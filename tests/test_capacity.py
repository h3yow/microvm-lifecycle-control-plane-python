"""容量耗尽与失败回滚：拒绝超卖，失败后无实例/配额/进程残留。"""

import os

import pytest

from microvm.errors import CapacityExhausted, HypervisorError
from microvm.models import InstanceSpec
from microvm.states import InstanceState
from tests.conftest import TOTAL_CPUS, TOTAL_MEMORY_MB


def _alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_capacity_exhausted_rejects_and_no_oversell(service, tenant_a, admin):
    print(f"\n[容量-耗尽] 请求输入: 主机 {TOTAL_CPUS}C/{TOTAL_MEMORY_MB}MB，"
          f"连续申请 (3,800) 与 (2,300)")
    service.create_instance(tenant_a, InstanceSpec("big", 3, 800), None)

    with pytest.raises(CapacityExhausted) as exc:
        service.create_instance(tenant_a, InstanceSpec("small", 2, 300), None)

    cap = service.admin_capacity(admin)
    print(f"[容量-耗尽] 判定: code={exc.value.code}；剩余 cpus={cap['free_cpus']} "
          f"mem={cap['free_memory_mb']}MB 不足以容纳 (2,300) -> 拒绝，未超卖")
    assert cap["used_cpus"] == 3
    assert cap["used_memory_mb"] == 800
    assert len(service.list_instances(tenant_a)) == 1


def test_cpu_or_memory_any_dimension_exhaust(service, tenant_a, admin):
    print("\n[容量-单维度耗尽] 申请 (2,1000) 耗尽内存后再申请 (1,100)")
    service.create_instance(tenant_a, InstanceSpec("mem-hog", 2, 1000), None)

    with pytest.raises(CapacityExhausted):
        service.create_instance(tenant_a, InstanceSpec("mem-rest", 1, 100), None)
    cap = service.admin_capacity(admin)
    print(f"[容量-单维度耗尽] 判定: CPU 尚余 {cap['free_cpus']} 但内存余 "
          f"{cap['free_memory_mb']}MB -> 仍拒绝")
    assert cap["free_cpus"] == 2
    assert cap["free_memory_mb"] == 24


def test_create_failure_rolls_back_quota_and_instance(service, tenant_a, admin):
    spec = InstanceSpec("doomed", 2, 400, fault_inject=True)
    print(f"\n[回滚-创建失败] 请求输入: {spec}（占用配额后注入失败）")

    with pytest.raises(HypervisorError) as exc:
        service.create_instance(tenant_a, spec, None)

    cap = service.admin_capacity(admin)
    print(f"[回滚-创建失败] 判定: code={exc.value.code}；回滚后 used_cpus="
          f"{cap['used_cpus']} used_mem={cap['used_memory_mb']}，实例数=0")
    assert cap["used_cpus"] == 0
    assert cap["used_memory_mb"] == 0
    assert service.list_instances(tenant_a) == []


def test_start_failure_after_failed_retry_releases_quota(service, tenant_a, admin):
    # 先造成一个 FAILED 实例（创建回滚不留实例，故用 arm 启动失败构造）
    vm = service.create_instance(tenant_a, InstanceSpec("flaky", 1, 200), None)
    service._hypervisor.arm_failure(vm.instance_id)  # noqa: SLF001
    print(f"\n[回滚-启动失败] 请求: start id={vm.instance_id}（注入启动失败）")

    with pytest.raises(HypervisorError):
        service.start(tenant_a, vm.instance_id, None)

    refreshed = service.get_instance(tenant_a, vm.instance_id)
    cap = service.admin_capacity(admin)
    print(f"[回滚-启动失败] 判定: state={refreshed.state.value} pid={refreshed.pid}；"
          f"配额已释放 used_cpus={cap['used_cpus']}，无进程残留={not _alive(refreshed.pid)}")
    assert refreshed.state == InstanceState.FAILED
    assert refreshed.pid is None
    assert cap["used_cpus"] == 0
    assert cap["used_memory_mb"] == 0

    # FAILED 实例可成功重试 start 并重新占用配额
    recovered = service.start(tenant_a, vm.instance_id, None)
    print(f"[回滚-启动失败] 重试: state={recovered.state.value} pid={recovered.pid} 存活")
    assert recovered.state == InstanceState.RUNNING
    assert _alive(recovered.pid)
    service.stop(tenant_a, vm.instance_id, None)
    service.delete(tenant_a, vm.instance_id, None)


def test_delete_releases_quota_and_kills_process(service, tenant_a, admin):
    vm = service.create_instance(tenant_a, InstanceSpec("ephemeral", 1, 200), None)
    vm = service.start(tenant_a, vm.instance_id, None)
    pid = vm.pid
    print(f"\n[回滚-删除] 请求: delete id={vm.instance_id} pid={pid}")

    service.delete(tenant_a, vm.instance_id, None)
    cap = service.admin_capacity(admin)
    print(f"[回滚-删除] 判定: 进程存活={_alive(pid)}（期望False）；"
          f"used_cpus={cap['used_cpus']} 实例数={len(service.list_instances(tenant_a))}")
    assert not _alive(pid)
    assert cap["used_cpus"] == 0
    assert cap["used_memory_mb"] == 0
    assert service.list_instances(tenant_a) == []
