"""共享夹具：小容量主机（4 vCPU / 1024MB）与服务实例。"""

import logging

import pytest

from microvm.auth import Authenticator
from microvm.hypervisor import LocalHypervisor
from microvm.idempotency import IdempotencyStore
from microvm.models import Principal
from microvm.resources import ResourceManager
from microvm.service import LifecycleService
from microvm.store import InstanceStore

TOTAL_CPUS = 4
TOTAL_MEMORY_MB = 1024


def _make_service():
    return LifecycleService(
        resources=ResourceManager(total_cpus=TOTAL_CPUS, total_memory_mb=TOTAL_MEMORY_MB),
        store=InstanceStore(),
        hypervisor=LocalHypervisor(),
        idempotency=IdempotencyStore(),
        authenticator=Authenticator(),
    )


@pytest.fixture
def service():
    return _make_service()


@pytest.fixture
def service_factory():
    """需要独立服务实例的测试使用。"""
    return _make_service


@pytest.fixture
def tenant_a() -> Principal:
    return Authenticator().authenticate("token-tenant-a")


@pytest.fixture
def tenant_b() -> Principal:
    return Authenticator().authenticate("token-tenant-b")


@pytest.fixture
def admin() -> Principal:
    return Authenticator().authenticate("token-admin")


@pytest.fixture(autouse=True)
def _service_logs(caplog):
    """确保单测能捕获并打印"请求输入 + 判定依据"日志。"""
    caplog.set_level(logging.INFO, logger="microvm.service")
    yield
