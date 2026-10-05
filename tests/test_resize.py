"""规格调整（resize）：成功变更、容量不足整体拒绝且无残留、
与生命周期操作在同一实例上排队冲突、幂等重放、失败后恢复与容量账一致。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from microvm.errors import (
    CapacityExhausted,
    IdempotencyConflict,
    InvalidRequest,
    InvalidStateTransition,
    OperationConflict,
)
from microvm.models import InstanceSpec
from microvm.states import InstanceState
from tests.conftest import TOTAL_CPUS, TOTAL_MEMORY_MB


# --------------------------------------------------------------------------- #
# 一、调整成功：身份/归属不变，配额按新规格重算，容量视图随之变准
# --------------------------------------------------------------------------- #
def test_resize_success_keeps_identity_and_recharges_quota(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 200), None)
    print(f"\n[调整-成功] 请求输入: id={vm.instance_id} (1C,200MB) -> (3C,500MB)")

    resized = service.resize(tenant_a, vm.instance_id, 3, 500, None)

    cap = service.admin_capacity(admin)
    print(f"[调整-成功] 判定: id 不变={resized.instance_id == vm.instance_id}，"
          f"归属={resized.tenant_id}，新规格=({resized.spec.cpus}C,"
          f"{resized.spec.memory_mb}MB)，状态={resized.state.value}，"
          f"账本 used={cap['used_cpus']}C/{cap['used_memory_mb']}MB")
    assert resized.instance_id == vm.instance_id
    assert resized.tenant_id == "tenant-a"
    assert (resized.spec.cpus, resized.spec.memory_mb) == (3, 500)
    assert resized.state == InstanceState.STOPPED
    assert cap["used_cpus"] == 3
    assert cap["used_memory_mb"] == 500
    assert len(service.list_instances(tenant_a)) == 1


def test_resize_down_frees_capacity(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 3, 800), None)
    print(f"\n[调整-调小] 请求输入: id={vm.instance_id} (3C,800MB) -> (1C,100MB)")

    service.resize(tenant_a, vm.instance_id, 1, 100, None)

    cap = service.admin_capacity(admin)
    print(f"[调整-调小] 判定: 调小后账本 used={cap['used_cpus']}C/"
          f"{cap['used_memory_mb']}MB，free={cap['free_cpus']}C/{cap['free_memory_mb']}MB")
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (1, 100)
    assert (cap["free_cpus"], cap["free_memory_mb"]) == (3, 924)


def test_resized_instance_runs_with_new_spec(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 128), None)
    service.resize(tenant_a, vm.instance_id, 2, 256, None)
    started = service.start(tenant_a, vm.instance_id, None)
    print(f"\n[调整-后启动] 判定: 调整后启动 state={started.state.value} "
          f"规格=({started.spec.cpus}C,{started.spec.memory_mb}MB) pid={started.pid}")
    assert started.state == InstanceState.RUNNING
    assert (started.spec.cpus, started.spec.memory_mb) == (2, 256)
    cap = service.admin_capacity(admin)
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (2, 256)
    service.stop(tenant_a, vm.instance_id, None)
    service.delete(tenant_a, vm.instance_id, None)


# --------------------------------------------------------------------------- #
# 一(续)、容量不足：整体拒绝，不留半成品，实例状态/占用与提交前一模一样
# --------------------------------------------------------------------------- #
def test_resize_rejected_when_capacity_insufficient_no_residue(
    service, tenant_a, admin
):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 200), None)
    # 占住其余容量：再建一台 (2C,700MB)，已用 3C/900MB
    service.create_instance(tenant_a, InstanceSpec("db", 2, 700), None)
    print(f"\n[调整-容量拒绝] 请求输入: id={vm.instance_id} (1C,200MB) -> (3C,500MB)；"
          f"主机 {TOTAL_CPUS}C/{TOTAL_MEMORY_MB}MB 已用 3C/900MB，"
          f"调整后需 5C > {TOTAL_CPUS}C")

    with pytest.raises(CapacityExhausted) as exc:
        service.resize(tenant_a, vm.instance_id, 3, 500, None)

    refreshed = service.get_instance(tenant_a, vm.instance_id)
    cap = service.admin_capacity(admin)
    print(f"[调整-容量拒绝] 判定: code={exc.value.code}；实例规格仍为 "
          f"({refreshed.spec.cpus}C,{refreshed.spec.memory_mb}MB) "
          f"状态={refreshed.state.value}；账本未变 used={cap['used_cpus']}C/"
          f"{cap['used_memory_mb']}MB（期望 3C/900MB），实例数=2")
    assert (refreshed.spec.cpus, refreshed.spec.memory_mb) == (1, 200)
    assert refreshed.state == InstanceState.STOPPED
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (3, 900)
    assert len(service.list_instances(tenant_a)) == 2


def test_resize_rejected_even_though_memory_only_dimension_blocks(
    service, tenant_a, admin
):
    # 已用 3C/1000MB（web 1C/300 + db 2C/700）；把 web 调到 1C/400MB：
    # CPU 维度 1+2=3<=4 够，但内存 400+700=1100>1024，仅内存维度挡住
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 300), None)
    service.create_instance(tenant_a, InstanceSpec("db", 2, 700), None)
    print(f"\n[调整-单维度拒绝] 请求: web (1C,300MB)->(1C,400MB)，CPU 够"
          f"（3<={TOTAL_CPUS}）但内存 400+700=1100>{TOTAL_MEMORY_MB}")
    with pytest.raises(CapacityExhausted) as exc:
        service.resize(tenant_a, vm.instance_id, 1, 400, None)
    cap = service.admin_capacity(admin)
    refreshed = service.get_instance(tenant_a, vm.instance_id)
    print(f"[调整-单维度拒绝] 判定: code={exc.value.code}；账本保持 "
          f"used={cap['used_cpus']}C/{cap['used_memory_mb']}MB（期望3C/1000MB），"
          f"web 内存仍为 {refreshed.spec.memory_mb}MB")
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (3, 1000)
    assert refreshed.spec.memory_mb == 300

# --------------------------------------------------------------------------- #
# 状态限制：只允许 STOPPED；RUNNING/PENDING/FAILED 拒绝，且拒绝无副作用
# --------------------------------------------------------------------------- #
def test_resize_only_allowed_when_stopped(service, tenant_a):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 128), None)
    service.start(tenant_a, vm.instance_id, None)
    print(f"\n[调整-状态限制] 请求: 对 RUNNING 实例 id={vm.instance_id} resize")
    with pytest.raises(InvalidStateTransition) as exc:
        service.resize(tenant_a, vm.instance_id, 2, 256, None)
    print(f"[调整-状态限制] 判定: code={exc.value.code}（与容量原因可区分）")
    assert exc.value.code == "invalid_state_transition"
    running = service.get_instance(tenant_a, vm.instance_id)
    assert running.state == InstanceState.RUNNING
    assert (running.spec.cpus, running.spec.memory_mb) == (1, 128)
    service.stop(tenant_a, vm.instance_id, None)
    service.delete(tenant_a, vm.instance_id, None)


def test_resize_on_failed_instance_rejected(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("flaky", 1, 200), None)
    service._hypervisor.arm_failure(vm.instance_id)  # noqa: SLF001
    with pytest.raises(Exception):  # noqa: B017, PT011
        service.start(tenant_a, vm.instance_id, None)
    assert service.get_instance(tenant_a, vm.instance_id).state == InstanceState.FAILED
    print(f"\n[调整-FAILED 限制] 请求: 对 FAILED 实例 id={vm.instance_id} resize")
    with pytest.raises(InvalidStateTransition) as exc:
        service.resize(tenant_a, vm.instance_id, 2, 300, None)
    print(f"[调整-FAILED 限制] 判定: code={exc.value.code}；FAILED 不持配额，"
          f"需先 start 恢复或 delete 释放")
    assert exc.value.code == "invalid_state_transition"
    # FAILED 实例仍可 delete，账实一致
    service.delete(tenant_a, vm.instance_id, None)
    cap = service.admin_capacity(admin)
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (0, 0)


@pytest.mark.parametrize("cpus,mem", [(0, 128), (-1, 128), (1, 0), (2, -5)])
def test_resize_invalid_spec_rejected(service, tenant_a, cpus, mem):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 128), None)
    print(f"\n[调整-非法参数] 请求: resize id={vm.instance_id} -> ({cpus},{mem})")
    with pytest.raises(InvalidRequest) as exc:
        service.resize(tenant_a, vm.instance_id, cpus, mem, None)
    print(f"[调整-非法参数] 判定: code={exc.value.code}")
    assert exc.value.code == "invalid_request"
    untouched = service.get_instance(tenant_a, vm.instance_id)
    assert (untouched.spec.cpus, untouched.spec.memory_mb) == (1, 128)


# --------------------------------------------------------------------------- #
# 二、同一实例上 resize 与生命周期操作排队：抢锁失败者得到明确冲突反馈
# --------------------------------------------------------------------------- #
def test_resize_conflicts_with_concurrent_lifecycle_op(service, tenant_a, monkeypatch):
    vm = service.create_instance(tenant_a, InstanceSpec("busy", 1, 128), None)
    print(f"\n[调整-并发冲突] 请求输入: 对 id={vm.instance_id} 并发提交 resize 与 start；"
          f"在 resize 持锁窗口内注入延迟")

    real_try_resize = service._resources.try_resize  # noqa: SLF001

    def slow_try_resize(old, new):
        import time as _t
        _t.sleep(0.3)
        return real_try_resize(old, new)

    monkeypatch.setattr(service._resources, "try_resize", slow_try_resize)  # noqa: SLF001

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_resize = pool.submit(service.resize, tenant_a, vm.instance_id, 2, 256, None)
        import time
        time.sleep(0.05)  # 确保 resize 先拿到每实例锁
        f_start = pool.submit(service.start, tenant_a, vm.instance_id, None)

        with pytest.raises(OperationConflict) as exc:
            f_start.result()
        resized = f_resize.result()

    print(f"[调整-并发冲突] 判定: start 被拒 code={exc.value.code}（非排队超时）；"
          f"resize 正常完成 state={resized.state.value} "
          f"规格=({resized.spec.cpus},{resized.spec.memory_mb})")
    assert exc.value.code == "operation_conflict"
    assert resized.state == InstanceState.STOPPED
    assert (resized.spec.cpus, resized.spec.memory_mb) == (2, 256)
    # 被拒的 start 无副作用：实例仍关机；之后可正常 start
    assert service.get_instance(tenant_a, vm.instance_id).state == InstanceState.STOPPED
    started = service.start(tenant_a, vm.instance_id, None)
    assert started.state == InstanceState.RUNNING
    service.stop(tenant_a, vm.instance_id, None)
    service.delete(tenant_a, vm.instance_id, None)


def test_resize_serialized_against_burst_of_lifecycle_ops(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("burst", 1, 128), None)
    print(f"\n[调整-并发串行] 请求输入: resize 与 start/stop/delete 混合并发 12 次")

    calls = (["resize"] * 6) + (["start"] * 3) + (["delete"] * 3)

    def one(op):
        try:
            if op == "resize":
                service.resize(tenant_a, vm.instance_id, 2, 256, None)
            elif op == "start":
                service.start(tenant_a, vm.instance_id, None)
            else:
                service.delete(tenant_a, vm.instance_id, None)
            return ("ok", op)
        except Exception as exc:  # noqa: BLE001
            return (type(exc).__name__, op)

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(one, calls))

    rejected = [o for o in outcomes if o[0] in
                {"OperationConflict", "InvalidStateTransition"}]
    remaining = service._store.get(vm.instance_id)  # noqa: SLF001
    cap = service.admin_capacity(admin)
    print(f"[调整-并发串行] 判定: 12 次提交中 {len(rejected)} 次被可区分原因拒绝；"
          f"实例{'已移除(唯一delete成功)' if remaining is None else 'state=' + remaining.state.value}；"
          f"账本 {'0C/0MB' if remaining is None else f'{cap['used_cpus']}C/{cap['used_memory_mb']}MB'}")
    # 账本必须与真实残留实例对得上
    if remaining is None:
        assert (cap["used_cpus"], cap["used_memory_mb"]) == (0, 0)
    else:
        assert remaining.state in {InstanceState.STOPPED, InstanceState.RUNNING}
        assert (cap["used_cpus"], cap["used_memory_mb"]) == (
            remaining.spec.cpus, remaining.spec.memory_mb
        )
    successful_deletes = [o for o in outcomes if o == ("ok", "delete")]
    assert len(successful_deletes) <= 1
    if remaining is not None:
        service.delete(tenant_a, vm.instance_id, None)


# --------------------------------------------------------------------------- #
# 二(续)、幂等：同键重放不重复扣/还容量，结果与首次一致
# --------------------------------------------------------------------------- #
def test_resize_idempotent_replay_no_double_charge(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("idem", 1, 200), "CREATE")
    print(f"\n[调整-幂等成功] 请求输入: 同 key=RSZ 重放 (1C,200MB)->(3C,600MB) x3")

    first = service.resize(tenant_a, vm.instance_id, 3, 600, "RSZ")
    for _ in range(2):
        replay = service.resize(tenant_a, vm.instance_id, 3, 600, "RSZ")

    cap = service.admin_capacity(admin)
    print(f"[调整-幂等成功] 判定: 重放返回同实例={replay.instance_id == first.instance_id}，"
          f"规格=({replay.spec.cpus},{replay.spec.memory_mb})，"
          f"账本只按新规格计一次 used={cap['used_cpus']}C/{cap['used_memory_mb']}MB，"
          f"实例数={len(service.list_instances(tenant_a))}")
    assert replay.instance_id == first.instance_id
    assert (replay.spec.cpus, replay.spec.memory_mb) == (3, 600)
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (3, 600)
    assert len(service.list_instances(tenant_a)) == 1


def test_resize_same_key_different_body_conflict(service, tenant_a):
    vm = service.create_instance(tenant_a, InstanceSpec("idem2", 1, 128), None)
    service.resize(tenant_a, vm.instance_id, 2, 256, "RK")
    print("\n[调整-幂等冲突] 请求: 同 key=RK 携带不同新规格 (3,512)")
    with pytest.raises(IdempotencyConflict) as exc:
        service.resize(tenant_a, vm.instance_id, 3, 512, "RK")
    print(f"[调整-幂等冲突] 判定: code={exc.value.code}；首次规格不被覆盖")
    assert exc.value.code == "idempotency_conflict"
    untouched = service.get_instance(tenant_a, vm.instance_id)
    assert (untouched.spec.cpus, untouched.spec.memory_mb) == (2, 256)


def test_failed_resize_replay_repeats_error_without_charge(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("idem3", 1, 200), None)
    service.create_instance(tenant_a, InstanceSpec("neighbor", 3, 700), None)
    # 已用 4C/900MB，调到 2C 即 2+3=5>4 必然失败
    print(f"\n[调整-幂等失败重放] 请求: key=RF 重放必然容量不足的 resize x2")

    cap_before = service.admin_capacity(admin)
    for _ in range(2):
        with pytest.raises(CapacityExhausted) as exc:
            service.resize(tenant_a, vm.instance_id, 2, 200, "RF")
        assert exc.value.code == "capacity_exhausted"
    cap_after = service.admin_capacity(admin)

    print(f"[调整-幂等失败重放] 判定: 两次均 capacity_exhausted；"
          f"账本前后一致 {cap_before['used_cpus']}=={cap_after['used_cpus']}、"
          f"{cap_before['used_memory_mb']}=={cap_after['used_memory_mb']}；规格未变")
    assert cap_before == cap_after
    refreshed = service.get_instance(tenant_a, vm.instance_id)
    assert (refreshed.spec.cpus, refreshed.spec.memory_mb) == (1, 200)
    assert refreshed.state == InstanceState.STOPPED


def test_concurrent_same_idempotency_key_resize_applies_once(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("idem4", 1, 128), None)
    print(f"\n[调整-幂等并发] 请求输入: 8 线程同 key=RCC 提交相同 resize")

    def one(_i):
        return service.resize(tenant_a, vm.instance_id, 2, 256, "RCC")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, range(8)))

    cap = service.admin_capacity(admin)
    print(f"[调整-幂等并发] 判定: 8 次返回规格一致="
          f"{len({(r.spec.cpus, r.spec.memory_mb) for r in results}) == 1}；"
          f"账本 used={cap['used_cpus']}C/{cap['used_memory_mb']}MB（期望2C/256MB）")
    assert all((r.spec.cpus, r.spec.memory_mb) == (2, 256) for r in results)
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (2, 256)


# --------------------------------------------------------------------------- #
# 三、失败后恢复：容量释放后重试成功；任意时刻容量视图与真实占用对得上
# --------------------------------------------------------------------------- #
def test_resize_retry_succeeds_after_capacity_freed(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 200), None)
    neighbor = service.create_instance(tenant_a, InstanceSpec("db", 3, 700), None)
    print(f"\n[调整-恢复重试] 请求输入: id={vm.instance_id} 调大到 (2C,400MB)，"
          f"首次因 db 占容量失败")

    with pytest.raises(CapacityExhausted):
        service.resize(tenant_a, vm.instance_id, 2, 400, None)
    mid = service.admin_capacity(admin)
    print(f"[调整-恢复重试] 首次判定: 拒绝，账本 used={mid['used_cpus']}C/"
          f"{mid['used_memory_mb']}MB，实例仍 STOPPED 旧规格")

    # 邻居被删除释放容量后，原请求重试成功
    service.delete(tenant_a, neighbor.instance_id, None)
    resized = service.resize(tenant_a, vm.instance_id, 2, 400, None)
    cap = service.admin_capacity(admin)
    print(f"[调整-恢复重试] 重试判定: 邻居删除后重试成功 state={resized.state.value} "
          f"规格=({resized.spec.cpus},{resized.spec.memory_mb})；"
          f"账本 used={cap['used_cpus']}C/{cap['used_memory_mb']}MB，与实例占用一致")
    assert resized.state == InstanceState.STOPPED
    assert (resized.spec.cpus, resized.spec.memory_mb) == (2, 400)
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (2, 400)
    assert len(service.list_instances(tenant_a)) == 1


def test_resize_rejected_instance_still_deletable_and_releases_quota(
    service, tenant_a, admin
):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 2, 400), None)
    service.create_instance(tenant_a, InstanceSpec("db", 2, 500), None)
    with pytest.raises(CapacityExhausted):
        service.resize(tenant_a, vm.instance_id, 3, 400, None)  # 3+2=5>4
    print(f"\n[调整-拒绝后删除] 请求: 被拒实例 id={vm.instance_id} 执行 delete")

    service.delete(tenant_a, vm.instance_id, None)
    cap = service.admin_capacity(admin)
    print(f"[调整-拒绝后删除] 判定: 按旧规格(2C,400MB)归还，剩余 db 占用 "
          f"used={cap['used_cpus']}C/{cap['used_memory_mb']}MB（期望2C/500MB），"
          f"实例数={len(service.list_instances(tenant_a))}")
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (2, 500)
    assert len(service.list_instances(tenant_a)) == 1


def test_capacity_view_matches_real_occupancy_after_many_resizes(
    service, tenant_a, admin
):
    print(f"\n[调整-账实一致] 场景: 建2台 -> 多次成功/失败调整 -> 启停 -> 删除")
    a = service.create_instance(tenant_a, InstanceSpec("a", 1, 100), None)
    b = service.create_instance(tenant_a, InstanceSpec("b", 1, 100), None)

    service.resize(tenant_a, a.instance_id, 2, 300, None)   # 已用 3C/400MB
    service.resize(tenant_a, b.instance_id, 1, 200, None)   # 已用 3C/500MB
    with pytest.raises(CapacityExhausted):
        service.resize(tenant_a, a.instance_id, 4, 300, None)  # 4+1=5>4 拒绝
    service.resize(tenant_a, a.instance_id, 1, 100, None)   # 调小 已用 2C/300MB
    service.resize(tenant_a, b.instance_id, 2, 700, None)   # 已用 3C/800MB

    started = service.start(tenant_a, b.instance_id, None)
    assert started.state == InstanceState.RUNNING

    alive = [i for i in service.list_instances(tenant_a)
             if i.state in {InstanceState.STOPPED, InstanceState.RUNNING}]
    cap = service.admin_capacity(admin)
    sum_cpus = sum(i.spec.cpus for i in alive)
    sum_mem = sum(i.spec.memory_mb for i in alive)
    print(f"[调整-账实一致] 判定: 存活实例 {len(alive)} 台规格和={sum_cpus}C/{sum_mem}MB；"
          f"容量视图 used={cap['used_cpus']}C/{cap['used_memory_mb']}MB；"
          f"账实相符={cap['used_cpus'] == sum_cpus and cap['used_memory_mb'] == sum_mem}")
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (sum_cpus, sum_mem)

    service.stop(tenant_a, b.instance_id, None)
    service.delete(tenant_a, a.instance_id, None)
    service.delete(tenant_a, b.instance_id, None)
    final = service.admin_capacity(admin)
    assert (final["used_cpus"], final["used_memory_mb"]) == (0, 0)
