"""主机资源配额：全局容量账本，保证不超卖。

账本由一把锁保护；try_allocate 把"容量检查 + 扣减"做成原子操作：
任意时刻通过锁串行的分配都不会出现超卖。
"""

import threading

from microvm.errors import CapacityExhausted
from microvm.models import CapacitySnapshot, InstanceSpec


class ResourceManager:
    def __init__(self, total_cpus: int, total_memory_mb: int) -> None:
        self._lock = threading.Lock()
        self._total_cpus = total_cpus
        self._total_memory_mb = total_memory_mb
        self._used_cpus = 0
        self._used_memory_mb = 0

    def try_allocate(self, spec: InstanceSpec) -> None:
        """容量足够则立即扣减并返回；不足抛 CapacityExhausted，不改动账本。"""
        with self._lock:
            free_cpus = self._total_cpus - self._used_cpus
            free_mem = self._total_memory_mb - self._used_memory_mb
            if spec.cpus > free_cpus or spec.memory_mb > free_mem:
                raise CapacityExhausted(
                    f"容量不足：申请 cpus={spec.cpus} mem={spec.memory_mb}MB，"
                    f"剩余 cpus={free_cpus} mem={free_mem}MB"
                )
            self._used_cpus += spec.cpus
            self._used_memory_mb += spec.memory_mb

    def release(self, spec: InstanceSpec) -> None:
        """归还配额。每个实例在失败回滚/删除时恰好归还一次。"""
        with self._lock:
            self._used_cpus = max(0, self._used_cpus - spec.cpus)
            self._used_memory_mb = max(0, self._used_memory_mb - spec.memory_mb)

    def try_resize(self, old_spec: InstanceSpec, new_spec: InstanceSpec) -> None:
        """原子地把某实例的占用从 old_spec 调整为 new_spec。

        校验按"归还旧占用、计入新占用"后的账本进行，因此实例自身的旧配额
        不会挡住自己的调大申请；容量不足时账本原样不动（整体拒绝，不调一半）。
        """
        with self._lock:
            new_used_cpus = self._used_cpus - old_spec.cpus + new_spec.cpus
            new_used_mem = self._used_memory_mb - old_spec.memory_mb + new_spec.memory_mb
            free_cpus = self._total_cpus - self._used_cpus
            free_mem = self._total_memory_mb - self._used_memory_mb
            # 除该实例自身旧占用外，主机还能额外挤出的空间
            headroom_cpus = free_cpus + old_spec.cpus
            headroom_mem = free_mem + old_spec.memory_mb
            if new_used_cpus > self._total_cpus or new_used_mem > self._total_memory_mb:
                raise CapacityExhausted(
                    f"容量不足：调整后需 cpus={new_spec.cpus} mem={new_spec.memory_mb}MB，"
                    f"可用（含本实例旧占用）cpus={headroom_cpus} mem={headroom_mem}MB"
                )
            self._used_cpus = new_used_cpus
            self._used_memory_mb = new_used_mem

    def snapshot(self) -> CapacitySnapshot:
        with self._lock:
            return CapacitySnapshot(
                total_cpus=self._total_cpus,
                total_memory_mb=self._total_memory_mb,
                used_cpus=self._used_cpus,
                used_memory_mb=self._used_memory_mb,
            )
