"""领域模型：仅类型定义。"""

from dataclasses import dataclass, field
from microvm.states import InstanceState


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    role: str  # "tenant" | "admin"


@dataclass(frozen=True)
class InstanceSpec:
    name: str
    cpus: int
    memory_mb: int
    fault_inject: bool = False  # 测试用：让进程模拟层注入失败


@dataclass
class Instance:
    instance_id: str
    tenant_id: str
    spec: InstanceSpec
    state: InstanceState = InstanceState.PENDING
    pid: int | None = None
    status_message: str = ""
    created_at: float = 0.0


@dataclass(frozen=True)
class CapacitySnapshot:
    total_cpus: int
    total_memory_mb: int
    used_cpus: int
    used_memory_mb: int

    @property
    def free_cpus(self) -> int:
        return self.total_cpus - self.used_cpus

    @property
    def free_memory_mb(self) -> int:
        return self.total_memory_mb - self.used_memory_mb
