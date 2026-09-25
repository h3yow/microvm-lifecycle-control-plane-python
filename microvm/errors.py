"""可区分的失败原因：所有业务错误都带有稳定 code 与 HTTP 状态。"""


class MicrovmError(Exception):
    code = "internal_error"
    http_status = 500


class Unauthorized(MicrovmError):
    """缺少调用方身份。"""

    code = "unauthorized"
    http_status = 401


class Forbidden(MicrovmError):
    """已认证但尝试管理级操作（非管理员）。"""

    code = "forbidden"
    http_status = 403


class InstanceNotFound(MicrovmError):
    """实例不存在，或存在但不属于当前租户（不泄露存在性）。"""

    code = "instance_not_found"
    http_status = 404


class InvalidRequest(MicrovmError):
    """请求参数非法（如配额申请为 0 或负数、名称为空）。"""

    code = "invalid_request"
    http_status = 400


class InvalidStateTransition(MicrovmError):
    """当前实例状态不允许该操作。"""

    code = "invalid_state_transition"
    http_status = 409


class CapacityExhausted(MicrovmError):
    """主机剩余容量不足以容纳申请，拒绝超卖。"""

    code = "capacity_exhausted"
    http_status = 507


class OperationConflict(MicrovmError):
    """同一实例上已有操作正在执行，冲突操作被拒绝。"""

    code = "operation_conflict"
    http_status = 409


class IdempotencyConflict(MicrovmError):
    """同一幂等键携带了不同的请求体。"""

    code = "idempotency_conflict"
    http_status = 409


class HypervisorError(MicrovmError):
    """本地进程模拟层启动/销毁失败。"""

    code = "hypervisor_error"
    http_status = 500
