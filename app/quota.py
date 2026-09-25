"""租户配额与主机容量管理。"""

from __future__ import annotations

import threading

from app.errors import ControlPlaneError, ErrorCode
from app.models import Quota, Usage


class QuotaManager:
    """跟踪每租户资源占用，保证不超卖。

    所有检查与扣减在同一把锁内完成，容量不足时拒绝且不产生任何占用。
    """

    def __init__(self, quotas: dict[str, Quota]) -> None:
        self._lock = threading.Lock()
        self._quotas = dict(quotas)
        self._usage: dict[str, Usage] = {}

    def try_acquire(self, tenant_id: str, vcpu: int, memory_mb: int) -> None:
        """尝试占用资源，不足时抛 QUOTA_EXCEEDED，且不产生部分占用。"""
        with self._lock:
            quota = self._quotas.get(tenant_id)
            if quota is None:
                raise ControlPlaneError(
                    ErrorCode.QUOTA_EXCEEDED,
                    f"tenant {tenant_id!r} has no quota configured",
                )
            usage = self._usage.setdefault(tenant_id, Usage())
            if usage.instances + 1 > quota.max_instances:
                raise ControlPlaneError(
                    ErrorCode.QUOTA_EXCEEDED,
                    f"instance quota exceeded: {usage.instances}/{quota.max_instances}",
                )
            if usage.vcpu + vcpu > quota.max_vcpu:
                raise ControlPlaneError(
                    ErrorCode.QUOTA_EXCEEDED,
                    f"vcpu quota exceeded: {usage.vcpu}+{vcpu}>{quota.max_vcpu}",
                )
            if usage.memory_mb + memory_mb > quota.max_memory_mb:
                raise ControlPlaneError(
                    ErrorCode.QUOTA_EXCEEDED,
                    f"memory quota exceeded: "
                    f"{usage.memory_mb}+{memory_mb}>{quota.max_memory_mb}",
                )
            usage.instances += 1
            usage.vcpu += vcpu
            usage.memory_mb += memory_mb

    def release(self, tenant_id: str, vcpu: int, memory_mb: int) -> None:
        """释放资源，不允许出现负占用。"""
        with self._lock:
            usage = self._usage.get(tenant_id)
            if usage is None:
                return
            usage.instances = max(0, usage.instances - 1)
            usage.vcpu = max(0, usage.vcpu - vcpu)
            usage.memory_mb = max(0, usage.memory_mb - memory_mb)

    def usage_of(self, tenant_id: str) -> Usage:
        """查询租户当前占用（用于测试与观测）。"""
        with self._lock:
            usage = self._usage.get(tenant_id)
            if usage is None:
                return Usage()
            return Usage(usage.instances, usage.vcpu, usage.memory_mb)
