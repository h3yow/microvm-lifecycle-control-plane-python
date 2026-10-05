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

    def try_resize(self, old_spec: InstanceSpec, new_spec: InstanceSpec) -> None:
        """原子地把占用从 old_spec 调整为 new_spec。

        在同一把账本锁内完成"减去旧占用 -> 容量检查 -> 计入新占用"：
        调整后任一维度超出总量即整体拒绝（CapacityExhausted），账本不做
        任何改动，不会出现只调一半的中间态。实例自身已持有的旧配额在
        检查时被视为可复用（即只对增量部分要求主机剩余容量）。
        """
        with self._lock:
            new_used_cpus = self._used_cpus - old_spec.cpus + new_spec.cpus
            new_used_memory = self._used_memory_mb - old_spec.memory_mb + new_spec.memory_mb
            if (
                new_used_cpus > self._total_cpus
                or new_used_memory > self._total_memory_mb
            ):
                free_cpus = self._total_cpus - self._used_cpus
                free_mem = self._total_memory_mb - self._used_memory_mb
                raise CapacityExhausted(
                    f"容量不足：调整 {old_spec.cpus}C/{old_spec.memory_mb}MB -> "
                    f"{new_spec.cpus}C/{new_spec.memory_mb}MB，"
                    f"需增量 cpus={new_spec.cpus - old_spec.cpus} "
                    f"mem={new_spec.memory_mb - old_spec.memory_mb}MB，"
                    f"主机剩余 cpus={free_cpus} mem={free_mem}MB"
                )
            self._used_cpus = new_used_cpus
            self._used_memory_mb = new_used_memory

    def release(self, spec: InstanceSpec) -> None:
        """归还配额。每个实例在失败回滚/删除时恰好归还一次。"""
        with self._lock:
            self._used_cpus = max(0, self._used_cpus - spec.cpus)
            self._used_memory_mb = max(0, self._used_memory_mb - spec.memory_mb)

    def snapshot(self) -> CapacitySnapshot:
        with self._lock:
            return CapacitySnapshot(
                total_cpus=self._total_cpus,
                total_memory_mb=self._total_memory_mb,
                used_cpus=self._used_cpus,
                used_memory_mb=self._used_memory_mb,
            )
