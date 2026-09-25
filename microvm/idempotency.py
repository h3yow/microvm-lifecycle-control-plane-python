"""幂等键记录。

语义
----
* 每个幂等键配一把锁：相同键的并发提交被串行化，第一个执行，
  随后的请求重放首次结果（包括首次失败的错误码）。
* 记录成功后永久返回首次结果，因此重复提交不会重复创建实例、
  也不会重复扣减配额。
* 相同键但请求体指纹不同 -> IdempotencyConflict。
* key 为 None 表示调用方放弃幂等保证，每次都实际执行。
"""

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from microvm.errors import IdempotencyConflict


class IdempotencyStore:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def scope(self, key: str | None, request_hash: int) -> Iterator[dict[str, Any] | None]:
        """进入幂等作用域。

        yield 已有记录时调用方应重放；yield None 时调用方负责执行并回填。
        """
        if key is None:
            yield None
            return

        lock = self._lock_for(key)
        with lock:
            existing = self._records.get(key)
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict(
                        f"幂等键 {key} 已绑定不同的请求体"
                    )
                yield existing
                return
            yield None

    def save_success(self, key: str | None, request_hash: int,
                     result: dict[str, Any]) -> None:
        if key is None:
            return
        with self._guard:
            self._records[key] = {
                "status": "success",
                "request_hash": request_hash,
                "result": result,
            }

    def save_failure(self, key: str | None, request_hash: int,
                     error_code: str) -> None:
        if key is None:
            return
        with self._guard:
            self._records[key] = {
                "status": "error",
                "request_hash": request_hash,
                "error_code": error_code,
            }
