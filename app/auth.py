"""鉴权模型：调用方身份与租户隔离。"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.errors import ControlPlaneError, ErrorCode


class Role(enum.Enum):
    """调用方角色。"""

    TENANT = "tenant"
    ADMIN = "admin"


@dataclass(frozen=True)
class Caller:
    """已认证的调用方。"""

    tenant_id: str
    role: Role


# 本地演示用 token 表：token -> Caller。
# 生产环境应替换为签发的凭证校验，这里保持确定性与可测试性。
_TOKENS: dict[str, Caller] = {
    "token-tenant-a": Caller(tenant_id="tenant-a", role=Role.TENANT),
    "token-tenant-b": Caller(tenant_id="tenant-b", role=Role.TENANT),
    "token-admin": Caller(tenant_id="admin", role=Role.ADMIN),
}


def authenticate(token: str | None) -> Caller:
    """根据 token 解析调用方身份，失败抛 UNAUTHENTICATED。

    失败信息不包含任何租户信息，避免泄露有效 token 的存在性。
    """
    if not token:
        raise ControlPlaneError(ErrorCode.UNAUTHENTICATED, "missing token")
    caller = _TOKENS.get(token)
    if caller is None:
        raise ControlPlaneError(ErrorCode.UNAUTHENTICATED, "invalid token")
    return caller
