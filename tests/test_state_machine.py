"""非法状态迁移必须被拒绝，并给出可区分的失败原因。"""

import pytest

from microvm.errors import InvalidStateTransition
from microvm.models import InstanceSpec
from microvm.states import InstanceState


def test_start_twice_and_stop_without_start_rejected(service, tenant_a):
    vm = service.create_instance(tenant_a, InstanceSpec("fsm", 1, 128), None)

    print(f"\n[状态机] 请求: 对新实例 id={vm.instance_id}（STOPPED）先 stop")
    with pytest.raises(InvalidStateTransition) as exc:
        service.stop(tenant_a, vm.instance_id, None)
    print(f"[状态机] 判定: stop on STOPPED -> code={exc.value.code}")
    assert exc.value.code == "invalid_state_transition"

    started = service.start(tenant_a, vm.instance_id, None)
    print(f"[状态机] 请求: start 已运行的 id={started.instance_id}")
    with pytest.raises(InvalidStateTransition) as exc:
        service.start(tenant_a, vm.instance_id, None)
    print(f"[状态机] 判定: start on RUNNING -> code={exc.value.code}")
    assert exc.value.code == "invalid_state_transition"

    # 被拒绝后实例仍为 RUNNING，配额与进程完好
    assert service.get_instance(tenant_a, vm.instance_id).state == InstanceState.RUNNING
    print("[状态机] 判定: 拒绝后实例仍 RUNNING，无副作用")
    service.stop(tenant_a, vm.instance_id, None)
    service.delete(tenant_a, vm.instance_id, None)


def test_restart_only_allowed_when_running(service, tenant_a):
    vm = service.create_instance(tenant_a, InstanceSpec("fsm2", 1, 128), None)
    print(f"\n[状态机] 请求: 对 STOPPED 实例 id={vm.instance_id} 执行 restart")
    with pytest.raises(InvalidStateTransition) as exc:
        service.restart(tenant_a, vm.instance_id, None)
    print(f"[状态机] 判定: restart on STOPPED -> code={exc.value.code}")
    assert exc.value.code == "invalid_state_transition"


def test_invalid_create_spec_rejected(service, tenant_a):
    print("\n[状态机] 请求: cpus=0 / memory_mb=-1 / 空名称")
    from microvm.errors import InvalidRequest

    for bad in (InstanceSpec("", 1, 128), InstanceSpec("x", 0, 128),
                InstanceSpec("x", 1, -5)):
        with pytest.raises(InvalidRequest) as exc:
            service.create_instance(tenant_a, bad, None)
        print(f"[状态机] 判定: spec={bad} -> code={exc.value.code}")
        assert exc.value.code == "invalid_request"
