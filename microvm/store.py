"""实例注册表与每实例串行化锁。

* 注册表由一把短临界区的 guard 锁保护。
* 每个实例 id 对应一把 RLock；生命周期操作在该锁内完成状态迁移与资源变更，
  同一实例上的并发操作因此被串行化。锁以非阻塞方式获取，抢不到立即返回
  OperationConflict（明确的冲突失败原因，而非排队超时）。
"""

import threading
from contextlib import contextmanager
from typing import Iterator

from microvm.errors import OperationConflict
from microvm.models import Instance


class InstanceStore:
    def __init__(self) -> None:
        self._instances: dict[str, Instance] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def put(self, instance: Instance) -> None:
        with self._guard:
            self._instances[instance.instance_id] = instance

    def get(self, instance_id: str) -> Instance | None:
        with self._guard:
            return self._instances.get(instance_id)

    def remove(self, instance_id: str) -> None:
        with self._guard:
            self._instances.pop(instance_id, None)

    def list_for_tenant(self, tenant_id: str) -> list[Instance]:
        with self._guard:
            return [i for i in self._instances.values() if i.tenant_id == tenant_id]

    def list_all(self) -> list[Instance]:
        with self._guard:
            return list(self._instances.values())

    def _lock_for(self, instance_id: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(instance_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[instance_id] = lock
            return lock

    @contextmanager
    def lock_instance(self, instance_id: str, blocking: bool = True) -> Iterator[None]:
        lock = self._lock_for(instance_id)
        acquired = lock.acquire(blocking=blocking)
        if not acquired:
            raise OperationConflict(f"实例 {instance_id} 上已有操作在执行")
        try:
            yield
        finally:
            lock.release()
