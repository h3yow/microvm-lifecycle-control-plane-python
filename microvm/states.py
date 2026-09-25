"""实例状态机定义。

状态说明
--------
PENDING  : 操作进行中的瞬时态（创建提交 / 启动 / 停止 / 重启 / 删除），
           持锁期间可见，正常情况下不会被外部请求稳定观测到。
STOPPED  : 已创建或已关机；实例仍持有主机配额，但没有存活进程。
RUNNING  : 本地模拟进程存活，实例持有配额。
FAILED   : 启动/重启失败后的稳定终态；不持有配额，可重试 start 或 delete。
DELETED  : 已销毁，配额已释放，记录从注册表移除。
"""

from enum import Enum


class InstanceState(str, Enum):
    PENDING = "pending"
    STOPPED = "stopped"
    RUNNING = "running"
    FAILED = "failed"
    DELETED = "deleted"


# 完整的合法状态迁移邻接表（含瞬时 PENDING）
ALLOWED_TRANSITIONS: dict[InstanceState, set[InstanceState]] = {
    InstanceState.PENDING: {
        InstanceState.STOPPED,
        InstanceState.RUNNING,
        InstanceState.FAILED,
        InstanceState.DELETED,
    },
    InstanceState.STOPPED: {
        InstanceState.PENDING,
        InstanceState.RUNNING,
        InstanceState.DELETED,
    },
    InstanceState.RUNNING: {
        InstanceState.PENDING,
        InstanceState.STOPPED,
        InstanceState.DELETED,
    },
    InstanceState.FAILED: {
        InstanceState.PENDING,
        InstanceState.RUNNING,
        InstanceState.DELETED,
    },
    InstanceState.DELETED: set(),
}


# 各生命周期操作允许的发起状态（非法迁移据此拒绝）
OPERATION_SOURCES: dict[str, set[InstanceState]] = {
    "start": {InstanceState.STOPPED, InstanceState.FAILED},
    "stop": {InstanceState.RUNNING},
    "restart": {InstanceState.RUNNING},
    "delete": {InstanceState.STOPPED, InstanceState.FAILED, InstanceState.RUNNING},
}


def can_perform(operation: str, state: InstanceState) -> bool:
    return state in OPERATION_SOURCES[operation]
