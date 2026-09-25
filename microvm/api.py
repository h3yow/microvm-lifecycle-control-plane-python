"""FastAPI 路由层：凭据解析、请求日志、错误码到 HTTP 响应的统一映射。"""

import logging
from typing import Any

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from microvm.auth import Authenticator
from microvm.errors import MicrovmError
from microvm.hypervisor import LocalHypervisor
from microvm.idempotency import IdempotencyStore
from microvm.models import InstanceSpec, Principal
from microvm.resources import ResourceManager
from microvm.service import LifecycleService
from microvm.store import InstanceStore

log = logging.getLogger("microvm.api")


class CreateRequest(BaseModel):
    name: str = Field(min_length=1)
    cpus: int = Field(gt=0)
    memory_mb: int = Field(gt=0)
    fault_inject: bool = False


def build_default_service() -> LifecycleService:
    # 本地默认：4 vCPU / 1024MB 小容量主机，便于快速验证容量耗尽
    return LifecycleService(
        resources=ResourceManager(total_cpus=4, total_memory_mb=1024),
        store=InstanceStore(),
        hypervisor=LocalHypervisor(),
        idempotency=IdempotencyStore(),
        authenticator=Authenticator(),
    )


def _instance_dict(inst: Any) -> dict[str, Any]:
    return {
        "instance_id": inst.instance_id,
        "tenant_id": inst.tenant_id,
        "name": inst.spec.name,
        "cpus": inst.spec.cpus,
        "memory_mb": inst.spec.memory_mb,
        "state": inst.state.value,
        "status_message": inst.status_message,
        "pid": inst.pid,
    }


def create_app(service: LifecycleService | None = None) -> FastAPI:
    service = service or build_default_service()
    app = FastAPI(title="microVM Lifecycle Control Plane")

    @app.exception_handler(MicrovmError)
    async def _handle_microvm_error(_request, exc: MicrovmError):
        log.warning("响应错误 code=%s http=%s detail=%s",
                    exc.code, exc.http_status, exc.args[0] if exc.args else "")
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    def _principal(authorization: str | None) -> Principal:
        return service._auth.authenticate(authorization)  # noqa: SLF001

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/v1/instances", status_code=201)
    def create_instance(
        body: CreateRequest,
        authorization: str | None = Header(default=None),
        x_idempotency_key: str | None = Header(default=None),
    ):
        log.info("HTTP POST /v1/instances 输入=%s idem=%s",
                 body.model_dump(), x_idempotency_key)
        principal = _principal(authorization)
        spec = InstanceSpec(
            name=body.name,
            cpus=body.cpus,
            memory_mb=body.memory_mb,
            fault_inject=body.fault_inject,
        )
        inst = service.create_instance(principal, spec, x_idempotency_key)
        return _instance_dict(inst)

    @app.post("/v1/instances/{instance_id}/{action}")
    def instance_action(
        instance_id: str,
        action: str,
        authorization: str | None = Header(default=None),
        x_idempotency_key: str | None = Header(default=None),
    ):
        if action not in {"start", "stop", "restart", "delete"}:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "message": f"未知操作 {action}"}},
            )
        log.info("HTTP POST /v1/instances/%s/%s idem=%s",
                 instance_id, action, x_idempotency_key)
        principal = _principal(authorization)
        if action == "start":
            return _instance_dict(service.start(principal, instance_id, x_idempotency_key))
        if action == "stop":
            return _instance_dict(service.stop(principal, instance_id, x_idempotency_key))
        if action == "restart":
            return _instance_dict(service.restart(principal, instance_id, x_idempotency_key))
        return service.delete(principal, instance_id, x_idempotency_key)

    @app.get("/v1/instances/{instance_id}")
    def get_instance(instance_id: str, authorization: str | None = Header(default=None)):
        log.info("HTTP GET /v1/instances/%s", instance_id)
        principal = _principal(authorization)
        return _instance_dict(service.get_instance(principal, instance_id))

    @app.get("/v1/instances")
    def list_instances(authorization: str | None = Header(default=None)):
        principal = _principal(authorization)
        items = service.list_instances(principal)
        log.info("HTTP GET /v1/instances tenant=%s 返回 %d 条",
                 principal.tenant_id, len(items))
        return {"instances": [_instance_dict(i) for i in items]}

    @app.get("/v1/admin/capacity")
    def admin_capacity(authorization: str | None = Header(default=None)):
        log.info("HTTP GET /v1/admin/capacity")
        principal = _principal(authorization)
        return service.admin_capacity(principal)

    return app
