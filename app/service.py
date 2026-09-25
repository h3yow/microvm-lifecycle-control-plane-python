"""控制面核心：实例生命周期服务。"""

from __future__ import annotations

import itertools
import logging
import threading

from app.auth import Caller, Role
from app.errors import ControlPlaneError, ErrorCode
from app.models import TRANSITIONS, Instance, InstanceState
from app.quota import QuotaManager
from app.runtime import LocalProcessRuntime

logger = logging.getLogger("microvm.service")


class InstanceService:
    """实例创建/启动/停止/重启/删除。

    一致性保证：
    - 创建按 (tenant, idempotency_key) 幂等，重复提交不重复扣减配额；
    - 同一实例的操作由实例级锁串行化；
    - 任何操作失败都回滚状态并释放已占用资源，不残留半成品实例。
    """

    def __init__(self, quota: QuotaManager, runtime: LocalProcessRuntime) -> None:
        self._quota = quota
        self._runtime = runtime
        self._instances: dict[str, Instance] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._counter = itertools.count(1)

    # ---- 内部辅助 ----

    def _authorize(self, caller: Caller, instance: Instance) -> None:
        """租户只能操作自己的实例；越权一律按 NOT_FOUND 处理，不泄露存在性。"""
        if caller.role is not Role.ADMIN and instance.tenant_id != caller.tenant_id:
            raise ControlPlaneError(ErrorCode.NOT_FOUND, "instance not found")

    def _lock_for(self, instance_id: str) -> threading.Lock:
        with self._global_lock:
            return self._locks.setdefault(instance_id, threading.Lock())

    def _get_visible(self, caller: Caller, instance_id: str) -> Instance:
        with self._global_lock:
            instance = self._instances.get(instance_id)
        if instance is None or instance.state is InstanceState.DELETED:
            raise ControlPlaneError(ErrorCode.NOT_FOUND, "instance not found")
        self._authorize(caller, instance)
        return instance

    def _transition(self, instance: Instance, action: str) -> None:
        """校验状态迁移合法性，非法迁移给出可区分原因。"""
        allowed, _target = TRANSITIONS[action]
        if instance.state not in allowed:
            raise ControlPlaneError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"cannot {action} instance in state {instance.state.value}",
            )

    # ---- 生命周期操作 ----

    def create(
        self,
        caller: Caller,
        name: str,
        vcpu: int,
        memory_mb: int,
        idempotency_key: str,
    ) -> Instance:
        """创建实例（幂等）。重复 key 返回既有实例，不产生重复扣减。"""
        key = (caller.tenant_id, idempotency_key)
        logger.info(
            "create request: tenant=%s name=%s vcpu=%d memory_mb=%d key=%s",
            caller.tenant_id, name, vcpu, memory_mb, idempotency_key,
        )
        with self._global_lock:
            existing_id = self._idempotency.get(key)
            if existing_id is not None:
                existing = self._instances.get(existing_id)
                if existing is not None and existing.state is not InstanceState.DELETED:
                    logger.info(
                        "create idempotent replay: key=%s -> %s", idempotency_key, existing_id
                    )
                    return existing
            # 先占配额再落记录；配额不足直接拒绝，无残留。
            self._quota.try_acquire(caller.tenant_id, vcpu, memory_mb)
            instance_id = f"i-{next(self._counter):06d}"
            instance = Instance(
                instance_id=instance_id,
                tenant_id=caller.tenant_id,
                name=name,
                vcpu=vcpu,
                memory_mb=memory_mb,
                state=InstanceState.STOPPED,
            )
            self._instances[instance_id] = instance
            self._idempotency[key] = instance_id
            logger.info("create ok: %s state=%s", instance_id, instance.state.value)
            return instance

    def start(self, caller: Caller, instance_id: str) -> Instance:
        return self._mutate(caller, instance_id, "start")

    def stop(self, caller: Caller, instance_id: str) -> Instance:
        return self._mutate(caller, instance_id, "stop")

    def restart(self, caller: Caller, instance_id: str) -> Instance:
        return self._mutate(caller, instance_id, "restart")

    def delete(self, caller: Caller, instance_id: str) -> Instance:
        return self._mutate(caller, instance_id, "delete")

    def _mutate(self, caller: Caller, instance_id: str, action: str) -> Instance:
        """对单实例串行执行状态迁移，失败时回滚到迁移前状态。"""
        logger.info("%s request: tenant=%s instance=%s", action, caller.tenant_id, instance_id)
        lock = self._lock_for(instance_id)
        with lock:
            instance = self._get_visible(caller, instance_id)
            self._transition(instance, action)
            before = instance.state
            try:
                if action == "start":
                    instance.pid = self._runtime.start(instance_id)
                    instance.state = InstanceState.RUNNING
                elif action == "stop":
                    assert instance.pid is not None
                    self._runtime.stop(instance.pid)
                    instance.pid = None
                    instance.state = InstanceState.STOPPED
                elif action == "restart":
                    assert instance.pid is not None
                    old_pid = instance.pid
                    self._runtime.stop(old_pid)
                    try:
                        instance.pid = self._runtime.start(instance_id)
                    except ControlPlaneError:
                        # 新进程起不来：实例无进程在跑，落回 STOPPED，可解释。
                        instance.pid = None
                        instance.state = InstanceState.STOPPED
                        raise
                    instance.state = InstanceState.RUNNING
                elif action == "delete":
                    if instance.pid is not None:
                        self._runtime.stop(instance.pid)
                        instance.pid = None
                    instance.state = InstanceState.DELETED
                    self._quota.release(
                        instance.tenant_id, instance.vcpu, instance.memory_mb
                    )
                    with self._global_lock:
                        self._instances.pop(instance_id, None)
                        self._locks.pop(instance_id, None)
                        stale = [
                            k for k, v in self._idempotency.items() if v == instance_id
                        ]
                        for k in stale:
                            self._idempotency.pop(k, None)
            except ControlPlaneError:
                if action in ("start", "stop"):
                    # 运行时失败：状态回滚，不留中间态。
                    instance.state = before
                raise
            instance.generation += 1
            logger.info(
                "%s ok: %s %s -> %s (gen=%d)",
                action, instance_id, before.value, instance.state.value, instance.generation,
            )
            return instance

    # ---- 查询 ----

    def get(self, caller: Caller, instance_id: str) -> Instance:
        """查询实例（越权不泄露存在性）。"""
        return self._get_visible(caller, instance_id)

    def list(self, caller: Caller) -> list[Instance]:
        """列出当前租户可见的实例。"""
        with self._global_lock:
            instances = list(self._instances.values())
        if caller.role is Role.ADMIN:
            return instances
        return [i for i in instances if i.tenant_id == caller.tenant_id]

    def list_all(self, caller: Caller) -> list[Instance]:
        """管理级操作：仅管理员可用，租户调用明确拒绝 FORBIDDEN。"""
        if caller.role is not Role.ADMIN:
            raise ControlPlaneError(
                ErrorCode.FORBIDDEN, "admin role required for list_all"
            )
        with self._global_lock:
            return list(self._instances.values())
