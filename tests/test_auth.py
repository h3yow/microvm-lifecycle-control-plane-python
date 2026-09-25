"""鉴权测试：租户隔离、越权拒绝、管理级操作区分、不泄露信息。"""

from __future__ import annotations

import logging

import pytest

from app.auth import authenticate
from app.errors import ControlPlaneError, ErrorCode
from app.service import InstanceService
from tests.conftest import ADMIN, TENANT_A, TENANT_B

log = logging.getLogger("test.auth")


class TestAuthentication:
    def test_missing_token_rejected(self) -> None:
        log.info("input: authenticate(None)")
        with pytest.raises(ControlPlaneError) as exc:
            authenticate(None)
        log.info("decision: %s: %s", exc.value.code.value, exc.value.message)
        assert exc.value.code is ErrorCode.UNAUTHENTICATED

    def test_invalid_token_rejected(self) -> None:
        log.info("input: authenticate('forged-token')")
        with pytest.raises(ControlPlaneError) as exc:
            authenticate("forged-token")
        log.info("decision: %s: %s", exc.value.code.value, exc.value.message)
        assert exc.value.code is ErrorCode.UNAUTHENTICATED
        # 错误信息不得暗示有效 token 的存在性
        assert "tenant" not in exc.value.message.lower()


class TestTenantIsolation:
    def test_cross_tenant_get_not_found(self, service: InstanceService) -> None:
        inst = service.create(TENANT_A, "web", 1, 256, "a1")
        log.info("input: tenant-b get %s (owned by tenant-a)", inst.instance_id)
        with pytest.raises(ControlPlaneError) as exc:
            service.get(TENANT_B, inst.instance_id)
        log.info("decision: %s (与不存在一致，不泄露存在性)", exc.value.code.value)
        assert exc.value.code is ErrorCode.NOT_FOUND

    def test_cross_tenant_mutations_rejected(self, service: InstanceService) -> None:
        inst = service.create(TENANT_A, "web", 1, 256, "a2")
        for action in ("start", "stop", "restart", "delete"):
            log.info("input: tenant-b %s on tenant-a instance", action)
            with pytest.raises(ControlPlaneError) as exc:
                getattr(service, action)(TENANT_B, inst.instance_id)
            log.info("decision: %s", exc.value.code.value)
            assert exc.value.code is ErrorCode.NOT_FOUND
        # 实例状态未被越权操作影响
        assert service.get(TENANT_A, inst.instance_id).state.value == "STOPPED"

    def test_list_scoped_to_tenant(self, service: InstanceService) -> None:
        service.create(TENANT_A, "a", 1, 256, "la")
        service.create(TENANT_B, "b", 1, 256, "lb")
        a_list = service.list(TENANT_A)
        b_list = service.list(TENANT_B)
        log.info(
            "decision: tenant-a sees %s, tenant-b sees %s",
            [i.name for i in a_list], [i.name for i in b_list],
        )
        assert [i.name for i in a_list] == ["a"]
        assert [i.name for i in b_list] == ["b"]

    def test_tenant_cannot_use_admin_operation(self, service: InstanceService) -> None:
        log.info("input: tenant-a calls admin-only list_all")
        with pytest.raises(ControlPlaneError) as exc:
            service.list_all(TENANT_A)
        log.info("decision: %s (与越权 NOT_FOUND 明确区分)", exc.value.code.value)
        assert exc.value.code is ErrorCode.FORBIDDEN

    def test_admin_can_list_all(self, service: InstanceService) -> None:
        service.create(TENANT_A, "a", 1, 256, "adm-a")
        service.create(TENANT_B, "b", 1, 256, "adm-b")
        all_instances = service.list_all(ADMIN)
        log.info("decision: admin sees %s", [i.name for i in all_instances])
        assert {i.name for i in all_instances} == {"a", "b"}
