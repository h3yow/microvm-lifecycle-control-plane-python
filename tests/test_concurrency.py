"""并发测试：并发提交、冲突操作串行化、失败后无残留。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app.errors import ControlPlaneError, ErrorCode
from app.models import InstanceState
from app.service import InstanceService
from tests.conftest import TENANT_A

log = logging.getLogger("test.concurrency")


class TestConcurrentCreate:
    def test_same_key_concurrent_create_single_instance(
        self, service: InstanceService, quota
    ) -> None:
        """并发提交同一幂等键：只产生一个实例、只扣一次配额。"""
        log.info("input: 16x concurrent create key=race")
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(
                pool.map(
                    lambda _: service.create(TENANT_A, "web", 1, 256, "race"),
                    range(16),
                )
            )
        ids = {r.instance_id for r in results}
        usage = quota.usage_of("tenant-a")
        log.info("decision: distinct ids=%s usage=%s", ids, usage)
        assert len(ids) == 1
        assert usage.instances == 1 and usage.vcpu == 1 and usage.memory_mb == 256

    def test_distinct_keys_concurrent_create_respects_quota(
        self, service: InstanceService, quota
    ) -> None:
        """并发创建不同实例：配额内成功，超出部分被拒绝，无超卖。"""
        log.info("input: 8x concurrent create distinct keys (quota=2 instances)")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda n: _try_create(service, n),
                    range(8),
                )
            )
        ok = [r for r in results if not isinstance(r, ControlPlaneError)]
        rejected = [r for r in results if isinstance(r, ControlPlaneError)]
        usage = quota.usage_of("tenant-a")
        log.info(
            "decision: ok=%d rejected=%d (all %s) usage=%s",
            len(ok),
            len(rejected),
            {r.code.value for r in rejected},
            usage,
        )
        assert len(ok) == 2  # max_instances=2
        assert all(r.code is ErrorCode.QUOTA_EXCEEDED for r in rejected)
        assert usage.instances == 2  # 不超卖


class TestConflictingOps:
    def test_concurrent_start_stop_serialized(self, service: InstanceService) -> None:
        """同一实例的冲突操作被串行化，终态合法且可解释。"""
        inst = service.create(TENANT_A, "web", 1, 256, "k-conflict")
        log.info("input: concurrent start/stop on %s", inst.instance_id)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda n: _try_mutate(
                        service, "start" if n % 2 == 0 else "stop", inst.instance_id
                    ),
                    range(16),
                )
            )
        final = service.get(TENANT_A, inst.instance_id)
        errors = [r.code.value for r in results if isinstance(r, ControlPlaneError)]
        log.info(
            "decision: final state=%s pid=%s rejected=%s",
            final.state.value, final.pid, errors,
        )
        # 终态必为合法状态，且状态与进程占用一致
        assert final.state in (InstanceState.RUNNING, InstanceState.STOPPED)
        if final.state is InstanceState.RUNNING:
            assert final.pid is not None
        else:
            assert final.pid is None
        # 所有失败都是可区分的非法迁移，而非内部错误
        assert all(
            isinstance(r, ControlPlaneError)
            and r.code is ErrorCode.INVALID_STATE_TRANSITION
            for r in results
            if isinstance(r, ControlPlaneError)
        )

    def test_concurrent_delete_and_start_no_residue(
        self, service: InstanceService, quota
    ) -> None:
        """删除与启动并发：要么删除成功要么启动成功，不留半成品。"""
        inst = service.create(TENANT_A, "web", 1, 256, "k-delrace")
        log.info("input: concurrent delete+start on %s", inst.instance_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda a: _try_mutate(service, a, inst.instance_id),
                    ["delete", "start"],
                )
            )
        usage = quota.usage_of("tenant-a")
        log.info("decision: results=%s usage=%s",
                 [getattr(r, "code", None) and r.code.value or "ok" for r in results],
                 usage)
        if usage.instances == 0:
            # delete 赢了：实例必须不可见
            try:
                service.get(TENANT_A, inst.instance_id)
                raise AssertionError("deleted instance still visible")
            except ControlPlaneError as e:
                assert e.code is ErrorCode.NOT_FOUND
        else:
            # start 赢了：实例处于合法状态
            final = service.get(TENANT_A, inst.instance_id)
            assert final.state in (InstanceState.RUNNING, InstanceState.STOPPED)


def _try_create(service: InstanceService, n: int):
    try:
        return service.create(TENANT_A, f"vm-{n}", 1, 256, f"key-{n}")
    except ControlPlaneError as e:
        return e


def _try_mutate(service: InstanceService, action: str, instance_id: str):
    try:
        return getattr(service, action)(TENANT_A, instance_id)
    except ControlPlaneError as e:
        return e
