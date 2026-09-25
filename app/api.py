"""FastAPI HTTP 层。"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import Caller, authenticate
from app.errors import ControlPlaneError, ErrorCode
from app.models import Instance
from app.service import InstanceService

_STATUS_BY_CODE = {
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.ALREADY_EXISTS: 409,
    ErrorCode.CONFLICT: 409,
    ErrorCode.INVALID_STATE_TRANSITION: 409,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.RUNTIME_ERROR: 500,
}


class CreateInstanceBody(BaseModel):
    name: str
    vcpu: int = Field(gt=0)
    memory_mb: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1)


def _instance_view(i: Instance) -> dict[str, object]:
    return {
        "instance_id": i.instance_id,
        "tenant_id": i.tenant_id,
        "name": i.name,
        "vcpu": i.vcpu,
        "memory_mb": i.memory_mb,
        "state": i.state.value,
        "generation": i.generation,
    }


def create_app(service: InstanceService) -> FastAPI:
    """构建 HTTP 应用。"""
    app = FastAPI(title="microvm-control-plane")

    def caller_dep(authorization: str | None = Header(default=None)) -> Caller:
        token = None
        if authorization and authorization.startswith("Bearer "):
            token = authorization.removeprefix("Bearer ").strip()
        return authenticate(token)

    @app.exception_handler(ControlPlaneError)
    async def cp_error_handler(_req: Request, exc: ControlPlaneError) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS_BY_CODE[exc.code],
            content={"error": {"code": exc.code.value, "message": exc.message}},
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/instances", status_code=201)
    def create_instance(
        body: CreateInstanceBody, caller: Caller = Depends(caller_dep)
    ) -> dict[str, object]:
        inst = service.create(
            caller, body.name, body.vcpu, body.memory_mb, body.idempotency_key
        )
        return _instance_view(inst)

    @app.get("/instances")
    def list_instances(caller: Caller = Depends(caller_dep)) -> list[dict[str, object]]:
        return [_instance_view(i) for i in service.list(caller)]

    @app.get("/instances/{instance_id}")
    def get_instance(
        instance_id: str, caller: Caller = Depends(caller_dep)
    ) -> dict[str, object]:
        return _instance_view(service.get(caller, instance_id))

    @app.post("/instances/{instance_id}/start")
    def start_instance(
        instance_id: str, caller: Caller = Depends(caller_dep)
    ) -> dict[str, object]:
        return _instance_view(service.start(caller, instance_id))

    @app.post("/instances/{instance_id}/stop")
    def stop_instance(
        instance_id: str, caller: Caller = Depends(caller_dep)
    ) -> dict[str, object]:
        return _instance_view(service.stop(caller, instance_id))

    @app.post("/instances/{instance_id}/restart")
    def restart_instance(
        instance_id: str, caller: Caller = Depends(caller_dep)
    ) -> dict[str, object]:
        return _instance_view(service.restart(caller, instance_id))

    @app.delete("/instances/{instance_id}")
    def delete_instance(
        instance_id: str, caller: Caller = Depends(caller_dep)
    ) -> dict[str, object]:
        return _instance_view(service.delete(caller, instance_id))

    @app.get("/admin/instances")
    def admin_list(caller: Caller = Depends(caller_dep)) -> list[dict[str, object]]:
        return [_instance_view(i) for i in service.list_all(caller)]

    return app
