"""可区分的失败原因定义。"""

from __future__ import annotations

import enum


class ErrorCode(enum.Enum):
    """所有对外可区分的失败原因。"""

    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    CONFLICT = "CONFLICT"
    RUNTIME_ERROR = "RUNTIME_ERROR"


class ControlPlaneError(Exception):
    """控制面统一异常，携带可区分的错误码。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
