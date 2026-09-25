"""并发提交：容量不超卖、同实例操作串行化、同幂等键并发只生效一次。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from microvm.errors import (
    InvalidStateTransition,
    OperationConflict,
)
from microvm.models import InstanceSpec
from microvm.states import InstanceState
from tests.conftest import TOTAL_CPUS, TOTAL_MEMORY_MB


def test_concurrent_create_never_oversells(service, tenant_a, admin):
    print(f"\n[并发-创建] 请求输入: {TOTAL_CPUS * 3} 个线程各申请 (1,256)，"
          f"主机容量 {TOTAL_CPUS}C/{TOTAL_MEMORY_MB}MB")

    def one(_i):
        try:
            return service.create_instance(
                tenant_a, InstanceSpec(f"vm-{_i}", 1, 256), None
            )
        except Exception as exc:  # noqa: BLE001
            return exc

    with ThreadPoolExecutor(max_workers=TOTAL_CPUS * 3) as pool:
        results = list(pool.map(one, range(TOTAL_CPUS * 3)))

    created = [r for r in results if not isinstance(r, Exception)]
    rejected = [r for r in results if isinstance(r, Exception)]
    cap = service.admin_capacity(admin)
    print(f"[并发-创建] 判定: 成功 {len(created)}（期望{TOTAL_CPUS}），"
          f"拒绝 {len(rejected)} 全部 capacity_exhausted="
          f"{all(type(r).__name__ == 'CapacityExhausted' for r in rejected)}；"
          f"used_cpus={cap['used_cpus']}<={TOTAL_CPUS}")
    assert len(created) == TOTAL_CPUS
    assert all(type(r).__name__ == "CapacityExhausted" for r in rejected)
    assert cap["used_cpus"] == TOTAL_CPUS
    assert cap["used_memory_mb"] == TOTAL_CPUS * 256
    assert len(service.list_instances(tenant_a)) == TOTAL_CPUS


def test_conflicting_ops_on_same_instance_are_serialized(service, tenant_a):
    vm = service.create_instance(tenant_a, InstanceSpec("busy", 1, 128), None)
    vm = service.start(tenant_a, vm.instance_id, None)
    print(f"\n[并发-同实例冲突] 请求输入: 对 id={vm.instance_id} 同时提交"
          f" start/stop/restart/delete 各多次")

    actions = ["start", "stop", "restart", "delete"] * 3

    def one(action):
        try:
            if action == "start":
                service.start(tenant_a, vm.instance_id, None)
            elif action == "stop":
                service.stop(tenant_a, vm.instance_id, None)
            elif action == "restart":
                service.restart(tenant_a, vm.instance_id, None)
            else:
                service.delete(tenant_a, vm.instance_id, None)
            return ("ok", action)
        except Exception as exc:  # noqa: BLE001
            return (type(exc).__name__, action)

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(one, actions))

    conflict_names = {"OperationConflict", "InvalidStateTransition"}
    rejected = [o for o in outcomes if o[0] in conflict_names]
    remaining = service._store.get(vm.instance_id)  # noqa: SLF001 直接查注册表
    if remaining is None:
        print(f"[并发-同实例冲突] 判定: 实例已被唯一一次 delete 正常移除，"
              f"共 {len(outcomes)} 次提交中 {len(rejected)} 次被可区分原因拒绝")
    else:
        print(f"[并发-同实例冲突] 判定: 共 {len(outcomes)} 次提交，"
              f"{len(rejected)} 次被可区分原因拒绝；实例最终状态="
              f"{remaining.state.value}（合法稳定态）")

    # 不变量：要么实例还在且状态合法，要么被唯一一次 delete 移除；不允许"半死"状态
    if remaining is not None:
        assert remaining.state in {InstanceState.RUNNING, InstanceState.STOPPED}
    # 只有一次 delete 可能成功（成功的结果为 ('ok','delete')）
    successful_deletes = [o for o in outcomes if o == ("ok", "delete")]
    assert len(successful_deletes) <= 1


def test_concurrent_same_idempotency_key_creates_once(service, tenant_a, admin):
    spec = InstanceSpec("once", 2, 300)
    print(f"\n[并发-同幂等键] 请求输入: 8 线程用同一 key=ONCE 创建 (2,300)")

    def one(_i):
        return service.create_instance(tenant_a, spec, "ONCE")

    with ThreadPoolExecutor(max_workers=8) as pool:
        vms = list(pool.map(one, range(8)))

    ids = {v.instance_id for v in vms}
    cap = service.admin_capacity(admin)
    print(f"[并发-同幂等键] 判定: 返回实例 id 集合大小={len(ids)}（期望1）；"
          f"used_cpus={cap['used_cpus']}（期望2）, used_mem={cap['used_memory_mb']}")
    assert ids == {vms[0].instance_id}
    assert len(service.list_instances(tenant_a)) == 1
    assert cap["used_cpus"] == 2
    assert cap["used_memory_mb"] == 300
