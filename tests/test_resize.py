"""实例规格调整（resize）：成功调整、容量不足整体拒绝、与生命周期操作
并发冲突、幂等重放、失败后恢复，以及任意时刻容量账与真实占用一致。

每条用例都 print 请求输入与判定依据（配合 -s 查看）。
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from microvm.api import create_app
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


def _ledger_matches_instances(service, admin) -> bool:
    """不变量：容量账本的已用量 == 所有持配额实例（STOPPED/RUNNING）规格之和。"""
    snap = service.admin_capacity(admin)
    held = [
        i for i in service._store.list_all()  # noqa: SLF001
        if i.state in {InstanceState.STOPPED, InstanceState.RUNNING}
    ]
    sum_cpus = sum(i.spec.cpus for i in held)
    sum_mem = sum(i.spec.memory_mb for i in held)
    return (
        snap["used_cpus"] == sum_cpus
        and snap["used_memory_mb"] == sum_mem
        and snap["used_cpus"] <= TOTAL_CPUS
        and snap["used_memory_mb"] <= TOTAL_MEMORY_MB
    )


# --------------------------------------------------------------------- #
# 1. 调整成功：身份/归属不变、状态仍 stopped、容量视图准确
# --------------------------------------------------------------------- #
def test_resize_up_and_down_keeps_identity(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 128), None)
    print(f"\n[调整-成功] 请求输入: id={vm.instance_id} (1C,128MB) -> (3C,600MB)")

    bigger = service.resize(tenant_a, vm.instance_id, 3, 600, None)
    cap = service.admin_capacity(admin)
    print(f"[调整-成功] 判定: 调大成功 id 不变={bigger.instance_id == vm.instance_id}，"
          f"tenant={bigger.tenant_id} state={bigger.state.value}，"
          f"账本 used={cap['used_cpus']}C/{cap['used_memory_mb']}MB")
    assert bigger.instance_id == vm.instance_id
    assert bigger.tenant_id == tenant_a.tenant_id
    assert bigger.state == InstanceState.STOPPED
    assert (bigger.spec.cpus, bigger.spec.memory_mb) == (3, 600)
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (3, 600)

    smaller = service.resize(tenant_a, vm.instance_id, 1, 64, None)
    cap = service.admin_capacity(admin)
    print(f"[调整-成功] 判定: 调小成功 (1C,64MB)，"
          f"账本 used={cap['used_cpus']}C/{cap['used_memory_mb']}MB free="
          f"{cap['free_cpus']}C/{cap['free_memory_mb']}MB")
    assert (smaller.spec.cpus, smaller.spec.memory_mb) == (1, 64)
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (1, 64)
    assert _ledger_matches_instances(service, admin)


def test_resized_spec_takes_effect_on_start_and_delete(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 128), None)
    service.resize(tenant_a, vm.instance_id, 2, 300, None)
    print(f"\n[调整-生效] 请求: resize 后 start id={vm.instance_id}，期望按 2C/300MB 计账")

    started = service.start(tenant_a, vm.instance_id, None)
    cap = service.admin_capacity(admin)
    assert started.state == InstanceState.RUNNING
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (2, 300)
    print(f"[调整-生效] 判定: 运行中按新规格占用 {cap['used_cpus']}C/"
          f"{cap['used_memory_mb']}MB")

    service.stop(tenant_a, vm.instance_id, None)
    service.delete(tenant_a, vm.instance_id, None)
    cap = service.admin_capacity(admin)
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (0, 0)
    print("[调整-生效] 判定: delete 按新规格归还，账本归零")


def test_resize_same_spec_is_noop(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 128), None)
    print(f"\n[调整-空变更] 请求: (1C,128MB) -> (1C,128MB)")
    out = service.resize(tenant_a, vm.instance_id, 1, 128, "NOOP")
    out2 = service.resize(tenant_a, vm.instance_id, 1, 128, "NOOP")
    cap = service.admin_capacity(admin)
    print(f"[调整-空变更] 判定: 两次幂等返回同一 id={out.instance_id == out2.instance_id}，"
          f"账本仍为 {cap['used_cpus']}C/{cap['used_memory_mb']}MB")
    assert out.instance_id == out2.instance_id == vm.instance_id
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (1, 128)


# --------------------------------------------------------------------- #
# 2. 容量不足：整体拒绝，无任何残留，错误码可区分
# --------------------------------------------------------------------- #
def test_resize_rejected_when_capacity_insufficient(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 128), None)
    other = service.create_instance(tenant_a, InstanceSpec("filler", 2, 500), None)
    print(f"\n[调整-容量拒绝] 请求输入: id={vm.instance_id} (1C,128MB) -> (4C,900MB)；"
          f"另有 filler (2C,500MB)，主机 {TOTAL_CPUS}C/{TOTAL_MEMORY_MB}MB")

    with pytest.raises(CapacityExhausted) as exc:
        service.resize(tenant_a, vm.instance_id, 4, 900, None)
    print(f"[调整-容量拒绝] 判定: code={exc.value.code}（区别于状态冲突等其他失败）")

    unchanged = service.get_instance(tenant_a, vm.instance_id)
    cap = service.admin_capacity(admin)
    print(f"[调整-容量拒绝] 判定: 拒绝后规格仍为 "
          f"{unchanged.spec.cpus}C/{unchanged.spec.memory_mb}MB state="
          f"{unchanged.state.value}；账本 {cap['used_cpus']}C/{cap['used_memory_mb']}MB"
          f"（=1+2 / 128+500）")
    assert exc.value.code == "capacity_exhausted"
    assert (unchanged.spec.cpus, unchanged.spec.memory_mb) == (1, 128)
    assert unchanged.state == InstanceState.STOPPED
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (3, 628)
    # 被整体拒绝后，实例仍可正常操作：调小可成功、也可直接删除
    ok = service.resize(tenant_a, vm.instance_id, 2, 400, None)
    assert (ok.spec.cpus, ok.spec.memory_mb) == (2, 400)
    service.delete(tenant_a, other.instance_id, None)
    service.delete(tenant_a, vm.instance_id, None)
    assert _ledger_matches_instances(service, admin)


def test_resize_invalid_args_rejected(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 128), None)
    print(f"\n[调整-非法参数] 请求输入: cpus=0 / memory_mb=-1")
    for cpus, mem in ((0, 128), (1, -1)):
        with pytest.raises(InvalidRequest) as exc:
            service.resize(tenant_a, vm.instance_id, cpus, mem, None)
        print(f"[调整-非法参数] 判定: ({cpus},{mem}) -> code={exc.value.code}")
        assert exc.value.code == "invalid_request"
    cap = service.admin_capacity(admin)
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (1, 128)


def test_resize_only_allowed_when_stopped(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 128), None)
    service.start(tenant_a, vm.instance_id, None)
    print(f"\n[调整-状态限制] 请求: 对 RUNNING 实例 id={vm.instance_id} resize")
    with pytest.raises(InvalidStateTransition) as exc:
        service.resize(tenant_a, vm.instance_id, 2, 200, None)
    print(f"[调整-状态限制] 判定: code={exc.value.code}，规格与运行进程不受影响")
    assert exc.value.code == "invalid_state_transition"
    running = service.get_instance(tenant_a, vm.instance_id)
    assert running.state == InstanceState.RUNNING
    assert (running.spec.cpus, running.spec.memory_mb) == (1, 128)
    service.stop(tenant_a, vm.instance_id, None)
    service.delete(tenant_a, vm.instance_id, None)


# --------------------------------------------------------------------- #
# 3. 并发：调整与同一实例上的生命周期操作排队/冲突，互不覆盖
# --------------------------------------------------------------------- #
def test_resize_conflicts_with_inflight_start(service, tenant_a, admin, monkeypatch):
    vm = service.create_instance(tenant_a, InstanceSpec("busy", 1, 128), None)
    entered = threading.Event()
    release = threading.Event()
    orig_launch = service._hypervisor.launch  # noqa: SLF001

    def blocked_launch(instance_id, spec):
        entered.set()
        assert release.wait(timeout=5)
        return orig_launch(instance_id, spec)

    monkeypatch.setattr(service._hypervisor, "launch", blocked_launch)  # noqa: SLF001
    print(f"\n[并发-调整撞启动] 请求输入: start 持锁进行中，对 id={vm.instance_id} "
          f"并发 resize (2C,300MB)")

    t = threading.Thread(target=service.start, args=(tenant_a, vm.instance_id, None))
    t.start()
    assert entered.wait(timeout=5)

    with pytest.raises(OperationConflict) as exc:
        service.resize(tenant_a, vm.instance_id, 2, 300, None)
    print(f"[并发-调整撞启动] 判定: resize 立即得到 code={exc.value.code}，不等待、不覆盖")

    release.set()
    t.join(timeout=5)
    final = service.get_instance(tenant_a, vm.instance_id)
    cap = service.admin_capacity(admin)
    print(f"[并发-调整撞启动] 判定: start 正常完成 state={final.state.value}，"
          f"规格未被覆盖={final.spec.cpus}C/{final.spec.memory_mb}MB，"
          f"账本={cap['used_cpus']}C/{cap['used_memory_mb']}MB")
    assert final.state == InstanceState.RUNNING
    assert (final.spec.cpus, final.spec.memory_mb) == (1, 128)
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (1, 128)
    assert _ledger_matches_instances(service, admin)
    service.stop(tenant_a, vm.instance_id, None)
    service.delete(tenant_a, vm.instance_id, None)


def test_concurrent_resizes_only_one_runs(service, tenant_a, admin, monkeypatch):
    vm = service.create_instance(tenant_a, InstanceSpec("busy", 1, 128), None)
    entered = threading.Event()
    release = threading.Event()
    orig = service._resources.try_resize  # noqa: SLF001

    def blocked_resize(old_spec, new_spec):
        entered.set()
        assert release.wait(timeout=5)
        return orig(old_spec, new_spec)

    monkeypatch.setattr(service._resources, "try_resize", blocked_resize)  # noqa: SLF001
    print(f"\n[并发-调整撞调整] 请求输入: 两次 resize 并发：(2C,300MB) 与 (3C,400MB)")

    t = threading.Thread(
        target=service.resize, args=(tenant_a, vm.instance_id, 2, 300, None)
    )
    t.start()
    assert entered.wait(timeout=5)

    with pytest.raises(OperationConflict) as exc:
        service.resize(tenant_a, vm.instance_id, 3, 400, None)
    print(f"[并发-调整撞调整] 判定: 第二个 resize code={exc.value.code}，未触碰账本")

    release.set()
    t.join(timeout=5)
    final = service.get_instance(tenant_a, vm.instance_id)
    cap = service.admin_capacity(admin)
    print(f"[并发-调整撞调整] 判定: 仅首个调整生效 "
          f"{final.spec.cpus}C/{final.spec.memory_mb}MB，账本="
          f"{cap['used_cpus']}C/{cap['used_memory_mb']}MB")
    assert (final.spec.cpus, final.spec.memory_mb) == (2, 300)
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (2, 300)
    assert _ledger_matches_instances(service, admin)
    service.delete(tenant_a, vm.instance_id, None)


def test_mixed_concurrent_ops_keep_ledger_consistent(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("mix", 1, 128), None)
    requests = [("resize", 2, 256), ("start", 0, 0), ("resize", 1, 64),
                ("stop", 0, 0), ("restart", 0, 0), ("delete", 0, 0)] * 2
    print(f"\n[并发-混合操作] 请求输入: 对同一实例同时提交 resize/start/stop/"
          f"restart/delete 共 {len(requests)} 次")
    gate = threading.Event()

    def one(req):
        gate.wait(timeout=5)
        kind, cpus, mem = req
        try:
            if kind == "resize":
                service.resize(tenant_a, vm.instance_id, cpus, mem, None)
            else:
                getattr(service, kind)(tenant_a, vm.instance_id, None)
            return ("ok", kind)
        except Exception as exc:  # noqa: BLE001
            return (type(exc).__name__, kind)

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        futs = [pool.submit(one, r) for r in requests]
        gate.set()
        outcomes = [f.result(timeout=10) for f in futs]

    known = {"ok", "OperationConflict", "InvalidStateTransition",
             "InstanceNotFound"}
    assert all(o[0] in known for o in outcomes), outcomes
    remaining = service._store.get(vm.instance_id)  # noqa: SLF001
    if remaining is None:
        print("[并发-混合操作] 判定: 实例被唯一一次 delete 移除")
    else:
        print(f"[并发-混合操作] 判定: 实例留存 state={remaining.state.value} "
              f"spec={remaining.spec.cpus}C/{remaining.spec.memory_mb}MB")
        assert remaining.state in {InstanceState.STOPPED, InstanceState.RUNNING}
    assert _ledger_matches_instances(service, admin)
    successful_deletes = [o for o in outcomes if o == ("ok", "delete")]
    assert len(successful_deletes) <= 1
    print("[并发-混合操作] 判定: 所有失败原因可区分，账本与持配额实例之和一致")


# --------------------------------------------------------------------- #
# 4. 幂等重放：不重复扣/还容量，结果与首次一致
# --------------------------------------------------------------------- #
def test_resize_idempotent_replay_charges_once(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("idem", 1, 128), None)
    print(f"\n[幂等-调整重放] 请求输入: 同 key=RK1 重放 (1C,128MB)->(3C,512MB) 共 6 次")

    first = service.resize(tenant_a, vm.instance_id, 3, 512, "RK1")
    for _ in range(5):
        service.resize(tenant_a, vm.instance_id, 3, 512, "RK1")

    final = service.get_instance(tenant_a, vm.instance_id)
    cap = service.admin_capacity(admin)
    print(f"[幂等-调整重放] 判定: id={first.instance_id} 规格 "
          f"{final.spec.cpus}C/{final.spec.memory_mb}MB；账本只按一次增量计="
          f"{cap['used_cpus']}C/{cap['used_memory_mb']}MB（期望 3/512）")
    assert (final.spec.cpus, final.spec.memory_mb) == (3, 512)
    assert (cap["used_cpus"], cap["used_memory_mb"]) == (3, 512)
    assert len(service.list_instances(tenant_a)) == 1
    assert _ledger_matches_instances(service, admin)
    service.delete(tenant_a, vm.instance_id, None)


def test_resize_same_key_different_body_conflicts(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("idem", 1, 128), None)
    service.resize(tenant_a, vm.instance_id, 2, 256, "RK2")
    print("\n[幂等-同键不同体] 请求输入: key=RK2 先 (2,256) 再 (3,512)")
    with pytest.raises(IdempotencyConflict) as exc:
        service.resize(tenant_a, vm.instance_id, 3, 512, "RK2")
    print(f"[幂等-同键不同体] 判定: code={exc.value.code}，规格维持首次结果 (2,256)")
    assert exc.value.code == "idempotency_conflict"
    final = service.get_instance(tenant_a, vm.instance_id)
    assert (final.spec.cpus, final.spec.memory_mb) == (2, 256)
    assert _ledger_matches_instances(service, admin)
    service.delete(tenant_a, vm.instance_id, None)


def test_failed_resize_replay_then_retry_after_capacity_freed(
    service, tenant_a, admin
):
    vm = service.create_instance(tenant_a, InstanceSpec("web", 1, 128), None)
    filler = service.create_instance(tenant_a, InstanceSpec("filler", 3, 800), None)
    print(f"\n[恢复-调整失败重试] 请求输入: resize (1,128)->(2,512) key=RK3；"
          f"filler 占 3C/800MB，内存维度不足（需增量384，余224）")

    # 首次失败被幂等记录
    with pytest.raises(CapacityExhausted):
        service.resize(tenant_a, vm.instance_id, 2, 512, "RK3")
    cap1 = service.admin_capacity(admin)
    # 同键重放：得到相同失败，账本无任何变化
    with pytest.raises(CapacityExhausted) as exc:
        service.resize(tenant_a, vm.instance_id, 2, 512, "RK3")
    cap2 = service.admin_capacity(admin)
    print(f"[恢复-调整失败重试] 判定: 同键重放 code={exc.value.code}，"
          f"账本不变 {cap1['used_cpus']}C/{cap1['used_memory_mb']}MB == "
          f"{cap2['used_cpus']}C/{cap2['used_memory_mb']}MB")
    assert cap1 == cap2 == {
        "total_cpus": TOTAL_CPUS, "total_memory_mb": TOTAL_MEMORY_MB,
        "used_cpus": 4, "used_memory_mb": 928,
        "free_cpus": 0, "free_memory_mb": 96,
    }
    unchanged = service.get_instance(tenant_a, vm.instance_id)
    assert (unchanged.spec.cpus, unchanged.spec.memory_mb) == (1, 128)
    assert unchanged.state == InstanceState.STOPPED

    # 别的实例释放容量后，新的调整请求（不带旧幂等键）重试成功
    service.delete(tenant_a, filler.instance_id, None)
    recovered = service.resize(tenant_a, vm.instance_id, 2, 512, "RK3-RETRY")
    cap3 = service.admin_capacity(admin)
    print(f"[恢复-调整失败重试] 判定: filler 释放后重试成功 -> "
          f"{recovered.spec.cpus}C/{recovered.spec.memory_mb}MB state="
          f"{recovered.state.value}；账本 {cap3['used_cpus']}C/{cap3['used_memory_mb']}MB")
    assert (recovered.spec.cpus, recovered.spec.memory_mb) == (2, 512)
    assert recovered.state == InstanceState.STOPPED
    assert (cap3["used_cpus"], cap3["used_memory_mb"]) == (2, 512)
    assert _ledger_matches_instances(service, admin)
    service.delete(tenant_a, vm.instance_id, None)
    assert _ledger_matches_instances(service, admin)


# --------------------------------------------------------------------- #
# 5. HTTP 端到端
# --------------------------------------------------------------------- #
def test_resize_over_http():
    client = TestClient(create_app())
    h = {"Authorization": "Bearer token-tenant-a"}
    print("\n[HTTP-调整] 输入: POST /instances 创建 (1,128)，再 POST /resize (2,300)")
    r = client.post("/v1/instances", headers=h,
                    json={"name": "web", "cpus": 1, "memory_mb": 128})
    assert r.status_code == 201
    iid = r.json()["instance_id"]

    r = client.post(f"/v1/instances/{iid}/resize", headers=h,
                    json={"cpus": 2, "memory_mb": 300})
    assert r.status_code == 200, r.text
    body = r.json()
    print(f"[HTTP-调整] 判定: 200 cpus={body['cpus']} mem={body['memory_mb']} "
          f"state={body['state']} id 保持={body['instance_id'] == iid}")
    assert body["cpus"] == 2 and body["memory_mb"] == 300
    assert body["state"] == "stopped" and body["instance_id"] == iid

    # 占满容量后再调大 -> 507 capacity_exhausted，响应结构与其他错误一致
    client.post("/v1/instances", headers=h,
                json={"name": "filler", "cpus": 2, "memory_mb": 700})
    r = client.post(f"/v1/instances/{iid}/resize", headers=h,
                    json={"cpus": 4, "memory_mb": 900})
    print(f"[HTTP-调整] 判定: 容量不足 http={r.status_code} "
          f"code={r.json()['error']['code']}")
    assert r.status_code == 507
    assert r.json()["error"]["code"] == "capacity_exhausted"

    # 幂等头：同键重放返回同一结果
    r1 = client.post(f"/v1/instances/{iid}/resize",
                     headers={**h, "X-Idempotency-Key": "http-resize"},
                     json={"cpus": 1, "memory_mb": 200})
    r2 = client.post(f"/v1/instances/{iid}/resize",
                     headers={**h, "X-Idempotency-Key": "http-resize"},
                     json={"cpus": 1, "memory_mb": 200})
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["cpus"] == r2.json()["cpus"] == 1
    print("[HTTP-调整] 判定: 幂等重放一致，未重复计账")
