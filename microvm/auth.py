"""鉴权：租户隔离 + 管理级操作区分。

模型
----
* 凭据为 Bearer Token，骨架阶段使用固定账号表（便于本地验证）。
* 普通租户只能操作自身 tenant_id 的资源；访问他人资源一律返回 404
  instance_not_found，不泄露其他租户的实例是否存在。
* 管理员可操作任意租户资源；管理级接口（如全局容量视图）要求 admin 角色，
  非管理员返回 403 forbidden。
"""

from microvm.errors import Forbidden, InstanceNotFound, Unauthorized
from microvm.models import Principal

_TENANT_ROLE = "tenant"
_ADMIN_ROLE = "admin"

# token -> (tenant_id, role)
ACCOUNTS: dict[str, tuple[str, str]] = {
    "token-tenant-a": ("tenant-a", _TENANT_ROLE),
    "token-tenant-b": ("tenant-b", _TENANT_ROLE),
    "token-admin": ("admin", _ADMIN_ROLE),
}


class Authenticator:
    def authenticate(self, credentials: str | None) -> Principal:
        if not credentials:
            raise Unauthorized("缺少 Authorization 凭据")
        token = credentials.removeprefix("Bearer ").strip()
        account = ACCOUNTS.get(token)
        if account is None:
            raise Unauthorized("凭据无效")
        return Principal(tenant_id=account[0], role=account[1])


def authorize_tenant(principal: Principal, tenant_id: str) -> None:
    """租户只能访问自己的资源；管理员可访问任意租户。

    越权访问统一报 not_found，避免存在性泄露。
    """
    if principal.role == _ADMIN_ROLE:
        return
    if principal.tenant_id != tenant_id:
        raise InstanceNotFound(f"实例不属于租户 {principal.tenant_id}")


def require_admin(principal: Principal) -> None:
    if principal.role != _ADMIN_ROLE:
        raise Forbidden("需要管理员角色")
