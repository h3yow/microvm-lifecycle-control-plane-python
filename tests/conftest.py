"""测试公共夹具：每个用例独立的服务实例与配额。"""

from __future__ import annotations

import logging

import pytest

from app.auth import Caller, Role
from app.models import Quota
from app.quota import QuotaManager
from app.runtime import LocalProcessRuntime
from app.service import InstanceService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

TENANT_A = Caller(tenant_id="tenant-a", role=Role.TENANT)
TENANT_B = Caller(tenant_id="tenant-b", role=Role.TENANT)
ADMIN = Caller(tenant_id="admin", role=Role.ADMIN)


@pytest.fixture()
def quota() -> QuotaManager:
    return QuotaManager(
        {
            "tenant-a": Quota(max_instances=2, max_vcpu=4, max_memory_mb=1024),
            "tenant-b": Quota(max_instances=1, max_vcpu=2, max_memory_mb=512),
        }
    )


@pytest.fixture()
def runtime() -> LocalProcessRuntime:
    rt = LocalProcessRuntime()
    yield rt
    # 清理残留模拟进程，避免测试间泄漏。
    for pid in list(rt._procs):
        rt.stop(pid)


@pytest.fixture()
def service(quota: QuotaManager, runtime: LocalProcessRuntime) -> InstanceService:
    return InstanceService(quota, runtime)
