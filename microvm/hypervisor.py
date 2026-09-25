"""本地进程模拟层。

用一个长睡眠的真实子进程模拟 microVM 的运行态；terminate 用进程组
整体回收。故障注入（arm_failure 或 spec.fault_inject）使 launch 在
创建进程前即失败，用于验证失败回滚时无残留进程。
"""

import os
import signal
import subprocess
import sys
import threading

from microvm.errors import HypervisorError
from microvm.models import InstanceSpec

_SLEEPER = "import time; time.sleep(3600)"


class LocalHypervisor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 一次性的"下次 launch 失败"标记，按 instance_id 安装
        self._armed_failures: set[str] = set()
        # pid -> Popen，用于 terminate 后 wait 回收，避免僵尸进程
        self._procs: dict[int, subprocess.Popen] = {}

    def arm_failure(self, instance_id: str) -> None:
        with self._lock:
            self._armed_failures.add(instance_id)

    def launch(self, instance_id: str, spec: InstanceSpec) -> int:
        with self._lock:
            fail = spec.fault_inject or instance_id in self._armed_failures
            self._armed_failures.discard(instance_id)
        if fail:
            # 在生成进程之前失败：天然不残留进程
            raise HypervisorError(f"实例 {instance_id} 的 VM 进程启动失败（故障注入）")
        proc = subprocess.Popen(
            [sys.executable, "-c", _SLEEPER],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # 独立进程组，便于整组回收
        )
        if proc.poll() is not None:
            raise HypervisorError(f"实例 {instance_id} 的 VM 进程立即退出")
        with self._lock:
            self._procs[proc.pid] = proc
        return proc.pid

    def terminate(self, pid: int) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        with self._lock:
            proc = self._procs.pop(pid, None)
        if proc is not None:
            try:
                proc.wait(timeout=5)  # 回收，避免僵尸进程
            except subprocess.TimeoutExpired:
                pass
