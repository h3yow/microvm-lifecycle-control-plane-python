"""失败回滚测试：运行时失败不残留状态、不泄漏资源。"""

from __future__ import annotations

import logging

import pytest

from app.errors import ControlPlaneError, ErrorCode
from app.models import InstanceState
from app.runtime import LocalProcessRuntime
from app.service import InstanceService
from tests.conftest import TENANT_A

log = logging.getLogger("test.rollback")


class FailingStartRuntime(LocalProcessRuntime):
    """start 永远失败的运行时，用于验证回滚。"""

    def start(self, instance_id: str) -> int:
        log.info("input: runtime.start(%s) -> forced failure", instance_id)
        raise ControlPlaneError(ErrorCode.RUNTIME_ERROR, "simulated spawn failure")


class FlakyRestartRuntime(LocalProcessRuntime):
    """仅在第二次 start（restart 的重新拉起）失败一次的运行时。"""

    def __init__(self) -> None:
        super().__init__()
        self._starts = 0

    def start(self, instance_id: str) -> int:
        self._starts += 1
        if self._starts == 2:
            log.info("input: runtime.start#%d -> forced failure", self._starts)
            raise ControlPlaneError(ErrorCode.RUNTIME_ERROR, "simulated respawn failure")
        return super().start(instance_id)


class TestStartRollback:
    def test_failed_start_rolls_back_state(
        self, service: InstanceService, quota
    ) -> None:
        failing = InstanceService(quota, FailingStartRuntime())
        inst = failing.create(TENANT_A, "web", 1, 256, "rb1")
        log.info("input: start with failing runtime")
        with pytest.raises(ControlPlaneError) as exc:
            failing.start(TENANT_A, inst.instance_id)
        assert exc.value.code is ErrorCode.RUNTIME_ERROR
        final = failing.get(TENANT_A, inst.instance_id)
        log.info(
            "decision: state=%s pid=%s (rolled back, no half-started instance)",
            final.state.value, final.pid,
        )
        assert final.state is InstanceState.STOPPED
        assert final.pid is None

    def test_failed_restart_leaves_explainable_state(self, quota) -> None:
        svc = InstanceService(quota, FlakyRestartRuntime())
        inst = svc.create(TENANT_A, "web", 1, 256, "rb2")
        svc.start(TENANT_A, inst.instance_id)
        log.info("input: restart with runtime failing on respawn")
        with pytest.raises(ControlPlaneError) as exc:
            svc.restart(TENANT_A, inst.instance_id)
        assert exc.value.code is ErrorCode.RUNTIME_ERROR
        final = svc.get(TENANT_A, inst.instance_id)
        log.info(
            "decision: state=%s pid=%s (old process stopped, new one failed)",
            final.state.value, final.pid,
        )
        # 旧进程已停、新进程没起来：落回 STOPPED，可重试 start，状态可解释
        assert final.state is InstanceState.STOPPED
        assert final.pid is None
        recovered = svc.start(TENANT_A, inst.instance_id)
        log.info("decision: retry start recovered -> %s", recovered.state.value)
        assert recovered.state is InstanceState.RUNNING
