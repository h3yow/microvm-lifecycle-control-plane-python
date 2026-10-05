"""生命周期编排：状态机迁移、配额占用、失败回滚、幂等的唯一事务边界。

一致性约定
----------
* 对已有实例的每个操作都在"每实例锁"内完成：读状态 -> 校验迁移 ->
  改 PENDING -> 资源/进程动作 -> 落定终态，期间其他操作被串行化或拒绝。
* create 先在资源锁内原子扣减配额，再登记实例；任一步失败即反向归还并删除。
* start/restart 的进程动作失败时：归还配额、状态落 FAILED、不残留进程。
* delete 仅在实例当前持有配额（STOPPED/RUNNING）时归还一次，随后移除记录。
"""

import hashlib
import json
import logging
import time
import uuid
from typing import Any, Callable

from microvm.auth import Authenticator, authorize_tenant, require_admin
from microvm.errors import (
    CapacityExhausted,
    Forbidden,
    HypervisorError,
    IdempotencyConflict,
    InstanceNotFound,
    InvalidRequest,
    InvalidStateTransition,
    MicrovmError,
    OperationConflict,
    Unauthorized,
)
from microvm.hypervisor import LocalHypervisor
from microvm.idempotency import IdempotencyStore
from microvm.models import Instance, InstanceSpec, Principal
from microvm.resources import ResourceManager
from microvm.states import InstanceState, can_perform
from microvm.store import InstanceStore

log = logging.getLogger("microvm.service")

# 错误码 -> 异常类，用于幂等重放首次失败
CODE_MAP: dict[str, type[MicrovmError]] = {
    cls.code: cls
    for cls in (
        Unauthorized,
        Forbidden,
        InstanceNotFound,
        InvalidStateTransition,
        CapacityExhausted,
        OperationConflict,
        IdempotencyConflict,
        HypervisorError,
        InvalidRequest,
    )
}

_HOLDING_QUOTA = {InstanceState.STOPPED, InstanceState.RUNNING}


class LifecycleService:
    def __init__(
        self,
        resources: ResourceManager,
        store: InstanceStore,
        hypervisor: LocalHypervisor,
        idempotency: IdempotencyStore,
        authenticator: Authenticator,
    ) -> None:
        self._resources = resources
        self._store = store
        self._hypervisor = hypervisor
        self._idem = idempotency
        self._auth = authenticator

    # ------------------------------------------------------------------ #
    # 对外操作
    # ------------------------------------------------------------------ #
    def create_instance(
        self, principal: Principal, spec: InstanceSpec, idempotency_key: str | None
    ) -> Instance:
        self._validate_spec(spec)
        req_hash = self._hash(
            "create",
            principal.tenant_id,
            {
                "name": spec.name,
                "cpus": spec.cpus,
                "memory_mb": spec.memory_mb,
                "fault_inject": spec.fault_inject,
            },
        )
        log.info(
            "请求 create tenant=%s name=%s cpus=%s mem=%sMB idem=%s",
            principal.tenant_id, spec.name, spec.cpus, spec.memory_mb,
            idempotency_key,
        )

        def do() -> Instance:
            # 1) 先原子占用配额（容量不足直接拒绝，尚未登记任何实例）
            self._resources.try_allocate(spec)
            instance_id = f"vm-{uuid.uuid4().hex[:12]}"
            instance = Instance(
                instance_id=instance_id,
                tenant_id=principal.tenant_id,
                spec=spec,
                state=InstanceState.PENDING,
                created_at=time.time(),
            )
            self._store.put(instance)
            log.info("判定 create 已分配配额并登记实例 id=%s", instance_id)
            try:
                if spec.fault_inject:
                    # 模拟"创建准备阶段"在配额占用之后失败，触发完整回滚
                    raise HypervisorError("创建准备阶段故障（注入）")
                instance.state = InstanceState.STOPPED
                instance.status_message = "实例已创建，处于关机态"
            except BaseException:
                # 2) 回滚：释放配额 + 移除半成品实例，不留任何残留
                self._resources.release(spec)
                self._store.remove(instance_id)
                log.warning(
                    "判定 create 失败，已回滚 id=%s：配额已释放、实例已移除",
                    instance_id,
                )
                raise
            log.info("判定 create 成功 id=%s -> STOPPED", instance_id)
            return instance

        return self._idem_call(idempotency_key, req_hash, do)  # type: ignore[return-value]

    def start(
        self, principal: Principal, instance_id: str, idempotency_key: str | None
    ) -> Instance:
        req_hash = self._hash("start", principal.tenant_id, {"instance_id": instance_id})
        log.info("请求 start tenant=%s id=%s idem=%s",
                 principal.tenant_id, instance_id, idempotency_key)

        def do() -> Instance:
            return self._lifecycle(principal, instance_id, "start")

        return self._idem_call(idempotency_key, req_hash, do)  # type: ignore[return-value]

    def stop(
        self, principal: Principal, instance_id: str, idempotency_key: str | None
    ) -> Instance:
        req_hash = self._hash("stop", principal.tenant_id, {"instance_id": instance_id})
        log.info("请求 stop tenant=%s id=%s idem=%s",
                 principal.tenant_id, instance_id, idempotency_key)

        def do() -> Instance:
            return self._lifecycle(principal, instance_id, "stop")

        return self._idem_call(idempotency_key, req_hash, do)  # type: ignore[return-value]

    def restart(
        self, principal: Principal, instance_id: str, idempotency_key: str | None
    ) -> Instance:
        req_hash = self._hash("restart", principal.tenant_id, {"instance_id": instance_id})
        log.info("请求 restart tenant=%s id=%s idem=%s",
                 principal.tenant_id, instance_id, idempotency_key)

        def do() -> Instance:
            return self._lifecycle(principal, instance_id, "restart")

        return self._idem_call(idempotency_key, req_hash, do)  # type: ignore[return-value]

    def resize(
        self,
        principal: Principal,
        instance_id: str,
        cpus: int,
        memory_mb: int,
        idempotency_key: str | None,
    ) -> Instance:
        """就地调整已关机实例的规格；实例 id 与归属不变，配额按新规格重算。"""
        if not isinstance(cpus, int) or cpus <= 0:
            raise InvalidRequest("cpus 必须为正整数")
        if not isinstance(memory_mb, int) or memory_mb <= 0:
            raise InvalidRequest("memory_mb 必须为正整数")
        req_hash = self._hash(
            "resize",
            principal.tenant_id,
            {"instance_id": instance_id, "cpus": cpus, "memory_mb": memory_mb},
        )
        log.info(
            "请求 resize tenant=%s id=%s 新规格 cpus=%s mem=%sMB idem=%s",
            principal.tenant_id, instance_id, cpus, memory_mb, idempotency_key,
        )

        def do() -> Instance:
            return self._lifecycle(
                principal, instance_id, "resize", new_cpus=cpus, new_memory_mb=memory_mb
            )

        return self._idem_call(idempotency_key, req_hash, do)  # type: ignore[return-value]

    def delete(
        self, principal: Principal, instance_id: str, idempotency_key: str | None
    ) -> dict[str, Any]:
        req_hash = self._hash("delete", principal.tenant_id, {"instance_id": instance_id})
        log.info("请求 delete tenant=%s id=%s idem=%s",
                 principal.tenant_id, instance_id, idempotency_key)

        def do() -> dict[str, Any]:
            self._lifecycle(principal, instance_id, "delete")
            return {"instance_id": instance_id, "state": InstanceState.DELETED.value}

        return self._idem_call(idempotency_key, req_hash, do)  # type: ignore[return-value]

    def get_instance(self, principal: Principal, instance_id: str) -> Instance:
        instance = self._store.get(instance_id)
        if instance is None:
            raise InstanceNotFound(f"实例 {instance_id} 不存在")
        authorize_tenant(principal, instance.tenant_id)
        log.info("请求 get tenant=%s id=%s -> 允许（归属校验通过）",
                 principal.tenant_id, instance_id)
        return instance

    def list_instances(self, principal: Principal) -> list[Instance]:
        return self._store.list_for_tenant(principal.tenant_id)

    def admin_capacity(self, principal: Principal) -> dict[str, Any]:
        require_admin(principal)
        snap = self._resources.snapshot()
        return {
            "total_cpus": snap.total_cpus,
            "total_memory_mb": snap.total_memory_mb,
            "used_cpus": snap.used_cpus,
            "used_memory_mb": snap.used_memory_mb,
            "free_cpus": snap.free_cpus,
            "free_memory_mb": snap.free_memory_mb,
        }

    # ------------------------------------------------------------------ #
    # 内部：统一的状态迁移事务
    # ------------------------------------------------------------------ #
    def _lifecycle(
        self,
        principal: Principal,
        instance_id: str,
        operation: str,
        new_cpus: int | None = None,
        new_memory_mb: int | None = None,
    ) -> Instance:
        instance = self._store.get(instance_id)
        if instance is None:
            raise InstanceNotFound(f"实例 {instance_id} 不存在")
        authorize_tenant(principal, instance.tenant_id)

        with self._store.lock_instance(instance_id, blocking=False):
            if not can_perform(operation, instance.state):
                reason = (
                    f"非法状态迁移：operation={operation} 当前状态={instance.state.value}"
                )
                log.warning("拒绝 %s id=%s 依据：%s", operation, instance_id, reason)
                raise InvalidStateTransition(reason)

            old_state = instance.state
            held_quota = old_state in _HOLDING_QUOTA
            log.info(
                "判定 %s 允许 id=%s 依据：%s 属于允许源状态，置 PENDING",
                operation, instance_id, old_state.value,
            )
            instance.state = InstanceState.PENDING

            if operation == "start":
                # FAILED 重试需要重新占用配额；STOPPED 仍持有配额
                if old_state == InstanceState.FAILED:
                    self._resources.try_allocate(instance.spec)
                try:
                    pid = self._hypervisor.launch(instance.instance_id, instance.spec)
                except BaseException:
                    instance.pid = None
                    instance.state = InstanceState.FAILED
                    instance.status_message = "启动失败：配额已释放，可重试 start 或 delete"
                    self._resources.release(instance.spec)
                    log.warning(
                        "判定 start 失败 id=%s：已释放配额并落 FAILED，无残留进程",
                        instance_id,
                    )
                    raise
                instance.pid = pid
                instance.state = InstanceState.RUNNING
                instance.status_message = "运行中"

            elif operation == "stop":
                if instance.pid is not None:
                    self._hypervisor.terminate(instance.pid)
                instance.pid = None
                instance.state = InstanceState.STOPPED
                instance.status_message = "已关机（配额仍占用）"

            elif operation == "restart":
                if instance.pid is not None:
                    self._hypervisor.terminate(instance.pid)
                    instance.pid = None
                try:
                    pid = self._hypervisor.launch(instance.instance_id, instance.spec)
                except BaseException:
                    instance.state = InstanceState.FAILED
                    instance.status_message = "重启失败：配额已释放，可重试 start 或 delete"
                    self._resources.release(instance.spec)
                    log.warning(
                        "判定 restart 失败 id=%s：旧进程已回收、配额已释放、落 FAILED",
                        instance_id,
                    )
                    raise
                instance.pid = pid
                instance.state = InstanceState.RUNNING
                instance.status_message = "已重启运行"

            elif operation == "resize":
                # 仅 STOPPED 可进入（由 can_perform 保证）：无存活进程、正持有旧规格配额
                old_spec = instance.spec
                new_spec = InstanceSpec(
                    name=old_spec.name,
                    cpus=new_cpus,  # type: ignore[arg-type]
                    memory_mb=new_memory_mb,  # type: ignore[arg-type]
                    fault_inject=old_spec.fault_inject,
                )
                log.info(
                    "判定 resize 进行中 id=%s：旧规格 cpus=%s mem=%sMB -> 新规格 cpus=%s mem=%sMB",
                    instance_id, old_spec.cpus, old_spec.memory_mb,
                    new_spec.cpus, new_spec.memory_mb,
                )
                try:
                    # 原子"旧占用换出新占用"：容量不足直接抛错，账本不动
                    self._resources.try_resize(old_spec, new_spec)
                except BaseException:
                    # 整体拒绝：状态回到 STOPPED，spec 保持旧值，配额与提交前一致
                    instance.state = InstanceState.STOPPED
                    instance.status_message = "调整规格被拒绝：保持原规格，可稍后重试"
                    log.warning(
                        "判定 resize 拒绝 id=%s：容量不足，实例仍为 STOPPED 且占用旧规格",
                        instance_id,
                    )
                    raise
                instance.spec = new_spec
                instance.state = InstanceState.STOPPED
                instance.status_message = "规格已调整，处于关机态"
                log.info(
                    "判定 resize 成功 id=%s：现规格 cpus=%s mem=%sMB，身份与归属不变",
                    instance_id, new_spec.cpus, new_spec.memory_mb,
                )

            elif operation == "delete":
                if instance.pid is not None:
                    self._hypervisor.terminate(instance.pid)
                    instance.pid = None
                if held_quota:
                    self._resources.release(instance.spec)
                self._store.remove(instance_id)
                log.info(
                    "判定 delete 成功 id=%s：进程已回收，配额归还=%s，记录已移除",
                    instance_id, held_quota,
                )
                return instance  # type: ignore[return-value]

            log.info("判定 %s 成功 id=%s -> %s",
                     operation, instance_id, instance.state.value)
            return instance

    # ------------------------------------------------------------------ #
    # 幂等与序列化辅助
    # ------------------------------------------------------------------ #
    def _idem_call(
        self,
        key: str | None,
        request_hash: str,
        do: Callable[[], Any],
    ) -> Any:
        with self._idem.scope(key, request_hash) as record:
            if record is None:
                try:
                    output = do()
                except MicrovmError as exc:
                    self._idem.save_failure(key, request_hash, exc.code)
                    log.info("幂等记录失败 idem=%s code=%s", key, exc.code)
                    raise
                self._idem.save_success(key, request_hash, self._encode(output))
                log.info("幂等记录成功 idem=%s", key)
                return output

            if record["status"] == "error":
                code = record["error_code"]
                log.info("幂等重放首次失败 idem=%s code=%s", key, code)
                raise CODE_MAP[code](f"幂等重放：首次请求失败（{code}）")
            log.info("幂等重放首次成功 idem=%s", key)
            return self._decode(record["result"])

    @staticmethod
    def _hash(action: str, tenant_id: str, payload: dict[str, Any]) -> str:
        body = json.dumps(
            {"action": action, "tenant_id": tenant_id, "payload": payload},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_spec(spec: InstanceSpec) -> None:
        if not spec.name or not spec.name.strip():
            raise InvalidRequest("name 不能为空")
        if spec.cpus <= 0:
            raise InvalidRequest("cpus 必须为正整数")
        if spec.memory_mb <= 0:
            raise InvalidRequest("memory_mb 必须为正整数")

    # 实例 <-> 可持久化字典（供幂等记录回放）
    @staticmethod
    def _encode(output: Any) -> dict[str, Any]:
        if isinstance(output, Instance):
            return {
                "kind": "instance",
                "data": {
                    "instance_id": output.instance_id,
                    "tenant_id": output.tenant_id,
                    "name": output.spec.name,
                    "cpus": output.spec.cpus,
                    "memory_mb": output.spec.memory_mb,
                    "fault_inject": output.spec.fault_inject,
                    "state": output.state.value,
                    "pid": output.pid,
                    "status_message": output.status_message,
                    "created_at": output.created_at,
                },
            }
        return {"kind": "result", "data": output}

    @staticmethod
    def _decode(encoded: dict[str, Any]) -> Any:
        if encoded["kind"] == "instance":
            d = encoded["data"]
            return Instance(
                instance_id=d["instance_id"],
                tenant_id=d["tenant_id"],
                spec=InstanceSpec(
                    name=d["name"],
                    cpus=d["cpus"],
                    memory_mb=d["memory_mb"],
                    fault_inject=d["fault_inject"],
                ),
                state=InstanceState(d["state"]),
                pid=d["pid"],
                status_message=d["status_message"],
                created_at=d["created_at"],
            )
        return encoded["data"]
