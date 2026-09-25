"""配额测试：容量耗尽拒绝、失败后无残留、释放后可复用。"""

from __future__ import annotations

import logging

import pytest

from app.errors import ControlPlaneError, ErrorCode
from app.service import InstanceService
from tests.conftest import TENANT_A

log = logging.getLogger("test.quota")


class TestQuotaExhaustion:
    def test_instance_quota_exhausted(self, service: InstanceService, quota) -> None:
        log.info("input: create 3 instances with max_instances=2")
        service.create(TENANT_A, "a", 1, 256, "q1")
        service.create(TENANT_A, "b", 1, 256, "q2")
        with pytest.raises(ControlPlaneError) as exc:
            service.create(TENANT_A, "c", 1, 256, "q3")
        log.info("decision: rejected %s: %s", exc.value.code.value, exc.value.message)
        assert exc.value.code is ErrorCode.QUOTA_EXCEEDED
        usage = quota.usage_of("tenant-a")
        log.info("decision: usage unchanged = %s", usage)
        assert usage.instances == 2

    def test_vcpu_quota_exhausted(self, service: InstanceService, quota) -> None:
        log.info("input: create vcpu=3 then vcpu=2 with max_vcpu=4")
        service.create(TENANT_A, "big", 3, 256, "v1")
        with pytest.raises(ControlPlaneError) as exc:
            service.create(TENANT_A, "too-big", 2, 256, "v2")
        log.info("decision: rejected %s: %s", exc.value.code.value, exc.value.message)
        assert exc.value.code is ErrorCode.QUOTA_EXCEEDED
        assert quota.usage_of("tenant-a").vcpu == 3

    def test_memory_quota_exhausted(self, service: InstanceService, quota) -> None:
        log.info("input: create mem=768 then mem=512 with max_memory_mb=1024")
        service.create(TENANT_A, "m1", 1, 768, "m1")
        with pytest.raises(ControlPlaneError) as exc:
            service.create(TENANT_A, "m2", 1, 512, "m2")
        log.info("decision: rejected %s", exc.value.code.value)
        assert exc.value.code is ErrorCode.QUOTA_EXCEEDED
        assert quota.usage_of("tenant-a").memory_mb == 768

    def test_rejected_create_leaves_no_instance(
        self, service: InstanceService, quota
    ) -> None:
        """配额拒绝后：无实例残留、无资源占用、幂等键不占用。"""
        first = service.create(TENANT_A, "a", 4, 1024, "full")
        with pytest.raises(ControlPlaneError):
            service.create(TENANT_A, "b", 1, 256, "rejected-key")
        log.info("input: retry with same rejected key after freeing quota")
        service.delete(TENANT_A, first.instance_id)
        inst = service.create(TENANT_A, "b", 1, 256, "rejected-key")
        log.info("decision: retry succeeded as %s", inst.instance_id)
        assert quota.usage_of("tenant-a").instances == 1

    def test_delete_frees_quota_for_reuse(
        self, service: InstanceService, quota
    ) -> None:
        i1 = service.create(TENANT_A, "a", 2, 512, "r1")
        service.create(TENANT_A, "b", 2, 512, "r2")
        service.delete(TENANT_A, i1.instance_id)
        log.info("input: create after delete freed quota")
        i3 = service.create(TENANT_A, "c", 2, 512, "r3")
        usage = quota.usage_of("tenant-a")
        log.info("decision: created %s usage=%s", i3.instance_id, usage)
        assert usage.instances == 2 and usage.vcpu == 4
