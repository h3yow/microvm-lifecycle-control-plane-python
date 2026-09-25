"""生命周期与幂等性测试：日志打印请求输入与判定依据。"""

from __future__ import annotations

import logging

import pytest

from app.errors import ControlPlaneError, ErrorCode
from app.models import InstanceState
from app.service import InstanceService
from tests.conftest import TENANT_A

log = logging.getLogger("test.lifecycle")


class TestLifecycle:
    def test_create_then_start_stop_delete(
        self, service: InstanceService, quota
    ) -> None:
        log.info("input: create tenant-a vcpu=2 mem=512 key=k1")
        inst = service.create(TENANT_A, "web", 2, 512, "k1")
        assert inst.state is InstanceState.STOPPED
        log.info("decision: created %s state=%s", inst.instance_id, inst.state)

        log.info("input: start %s", inst.instance_id)
        inst = service.start(TENANT_A, inst.instance_id)
        assert inst.state is InstanceState.RUNNING
        assert inst.pid is not None
        log.info("decision: running pid=%s", inst.pid)

        log.info("input: stop %s", inst.instance_id)
        inst = service.stop(TENANT_A, inst.instance_id)
        assert inst.state is InstanceState.STOPPED
        assert inst.pid is None

        log.info("input: delete %s", inst.instance_id)
        inst = service.delete(TENANT_A, inst.instance_id)
        assert inst.state is InstanceState.DELETED
        usage = quota.usage_of("tenant-a")
        log.info("decision: usage after delete = %s", usage)
        assert usage.instances == 0 and usage.vcpu == 0 and usage.memory_mb == 0

    def test_restart_keeps_running(self, service: InstanceService) -> None:
        inst = service.create(TENANT_A, "web", 1, 256, "k-restart")
        service.start(TENANT_A, inst.instance_id)
        old_pid = service.get(TENANT_A, inst.instance_id).pid
        inst = service.restart(TENANT_A, inst.instance_id)
        log.info(
            "decision: restart old_pid=%s new_pid=%s state=%s",
            old_pid, inst.pid, inst.state,
        )
        assert inst.state is InstanceState.RUNNING
        assert inst.pid is not None and inst.pid != old_pid

    def test_illegal_transitions_rejected(self, service: InstanceService) -> None:
        inst = service.create(TENANT_A, "web", 1, 256, "k-illegal")
        for action in ("stop", "restart"):
            log.info("input: %s on STOPPED instance", action)
            with pytest.raises(ControlPlaneError) as exc:
                getattr(service, action)(TENANT_A, inst.instance_id)
            log.info("decision: rejected with %s", exc.value.code.value)
            assert exc.value.code is ErrorCode.INVALID_STATE_TRANSITION
        # 状态未被非法迁移污染
        assert service.get(TENANT_A, inst.instance_id).state is InstanceState.STOPPED

    def test_delete_running_rejected(self, service: InstanceService) -> None:
        inst = service.create(TENANT_A, "web", 1, 256, "k-delrun")
        service.start(TENANT_A, inst.instance_id)
        log.info("input: delete on RUNNING instance")
        with pytest.raises(ControlPlaneError) as exc:
            service.delete(TENANT_A, inst.instance_id)
        log.info("decision: rejected with %s", exc.value.code.value)
        assert exc.value.code is ErrorCode.INVALID_STATE_TRANSITION

    def test_get_deleted_returns_not_found(self, service: InstanceService) -> None:
        inst = service.create(TENANT_A, "web", 1, 256, "k-gone")
        service.delete(TENANT_A, inst.instance_id)
        with pytest.raises(ControlPlaneError) as exc:
            service.get(TENANT_A, inst.instance_id)
        assert exc.value.code is ErrorCode.NOT_FOUND


class TestIdempotency:
    def test_replay_same_key_returns_same_instance(
        self, service: InstanceService, quota
    ) -> None:
        log.info("input: create key=dup twice")
        i1 = service.create(TENANT_A, "web", 2, 512, "dup")
        i2 = service.create(TENANT_A, "web", 2, 512, "dup")
        log.info("decision: %s vs %s", i1.instance_id, i2.instance_id)
        assert i1.instance_id == i2.instance_id
        usage = quota.usage_of("tenant-a")
        log.info("decision: usage=%s (no double charge)", usage)
        assert usage.instances == 1 and usage.vcpu == 2

    def test_different_keys_create_different_instances(
        self, service: InstanceService
    ) -> None:
        i1 = service.create(TENANT_A, "a", 1, 256, "k1")
        i2 = service.create(TENANT_A, "b", 1, 256, "k2")
        assert i1.instance_id != i2.instance_id
