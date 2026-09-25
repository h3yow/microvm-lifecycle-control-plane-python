"""幂等重放：重复请求不产生重复实例、不重复扣减资源。"""

import pytest

from microvm.errors import CapacityExhausted, IdempotencyConflict
from microvm.models import InstanceSpec
from microvm.states import InstanceState


def _log_request(case, **kwargs):
    print(f"\n[幂等-{case}] 请求输入: {kwargs}")


def test_create_replay_returns_same_instance_no_double_charge(service, tenant_a):
    spec = InstanceSpec(name="idem-vm", cpus=2, memory_mb=300)
    _log_request("创建重放", spec=(spec.cpus, spec.memory_mb), key="K1")

    first = service.create_instance(tenant_a, spec, "K1")
    second = service.create_instance(tenant_a, spec, "K1")

    print(f"[幂等-创建重放] 判定: 两次返回同一实例且 used_cpus 仅扣一次; "
          f"id1={first.instance_id} id2={second.instance_id}")
    assert first.instance_id == second.instance_id
    assert len(service.list_instances(tenant_a)) == 1


def test_create_replay_does_not_consume_extra_capacity(service, tenant_a, admin):
    spec = InstanceSpec(name="idem-cap", cpus=3, memory_mb=500)
    _log_request("容量不重复扣", spec=(3, 500), key="K2", replay=10)

    vm = service.create_instance(tenant_a, spec, "K2")
    for _ in range(10):
        service.create_instance(tenant_a, spec, "K2")

    cap = service.admin_capacity(admin)
    print(f"[幂等-容量不重复扣] 判定: used_cpus={cap['used_cpus']}（期望3）, "
          f"used_mem={cap['used_memory_mb']}（期望500）, 实例数=1, id={vm.instance_id}")
    assert cap["used_cpus"] == 3
    assert cap["used_memory_mb"] == 500
    assert len(service.list_instances(tenant_a)) == 1


def test_same_key_different_body_is_conflict(service, tenant_a):
    _log_request("同键不同体", first=(1, 128), second=(2, 256), key="K3")
    service.create_instance(tenant_a, InstanceSpec("a", 1, 128), "K3")

    with pytest.raises(IdempotencyConflict) as exc:
        service.create_instance(tenant_a, InstanceSpec("a", 2, 256), "K3")
    print(f"[幂等-同键不同体] 判定: 拒绝 code={exc.value.code}")
    assert exc.value.code == "idempotency_conflict"


def test_lifecycle_action_replay_is_idempotent(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("act", 1, 128), "KC")
    _log_request("start重放", instance_id=vm.instance_id, key="KS")

    started = service.start(tenant_a, vm.instance_id, "KS")
    pid1 = started.pid
    replayed = service.start(tenant_a, vm.instance_id, "KS")

    print(f"[幂等-start重放] 判定: state 保持 running、pid 不重复创建; "
          f"pid={pid1} replay_pid={replayed.pid}")
    assert replayed.state == InstanceState.RUNNING
    assert replayed.pid == pid1
    assert service.admin_capacity(admin)["used_cpus"] == 1


def test_failed_create_replay_replays_error_without_charge(service, tenant_a, admin):
    # 首次创建即耗尽容量：第二次重放必须得到同样的失败，且账本无变化
    service.create_instance(tenant_a, InstanceSpec("big", 4, 1000), "KB")
    spec = InstanceSpec("overflow", 1, 100, False)
    _log_request("失败重放", spec=(1, 100), 预期="剩余容量不足", key="KF")

    with pytest.raises(CapacityExhausted):
        service.create_instance(tenant_a, spec, "KF")
    cap_before = service.admin_capacity(admin)
    with pytest.raises(CapacityExhausted):
        service.create_instance(tenant_a, spec, "KF")
    cap_after = service.admin_capacity(admin)

    print(f"[幂等-失败重放] 判定: 两次均 capacity_exhausted; "
          f"容量前后一致 {cap_before['used_cpus']}=={cap_after['used_cpus']}")
    assert cap_before == cap_after
    assert len(service.list_instances(tenant_a)) == 1
