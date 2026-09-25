"""实例状态机与数据模型。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class InstanceState(enum.Enum):
    """microVM 实例状态机。"""

    CREATING = "CREATING"
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    DELETED = "DELETED"


# 合法状态迁移表：动作 -> (允许的源状态集合, 目标状态)
TRANSITIONS: dict[str, tuple[frozenset[InstanceState], InstanceState]] = {
    "start": (frozenset({InstanceState.STOPPED}), InstanceState.RUNNING),
    "stop": (frozenset({InstanceState.RUNNING}), InstanceState.STOPPED),
    "restart": (frozenset({InstanceState.RUNNING}), InstanceState.RUNNING),
    "delete": (
        frozenset({InstanceState.STOPPED, InstanceState.CREATING}),
        InstanceState.DELETED,
    ),
}


@dataclass
class Instance:
    """一个 microVM 实例（由本地进程模拟）。"""

    instance_id: str
    tenant_id: str
    name: str
    vcpu: int
    memory_mb: int
    state: InstanceState = InstanceState.CREATING
    pid: int | None = None
    generation: int = 0


@dataclass(frozen=True)
class Quota:
    """租户配额。"""

    max_instances: int
    max_vcpu: int
    max_memory_mb: int


@dataclass
class Usage:
    """租户当前资源占用。"""

    instances: int = 0
    vcpu: int = 0
    memory_mb: int = 0
