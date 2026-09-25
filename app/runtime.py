"""本地运行时：用本地进程模拟 microVM。"""

from __future__ import annotations

import subprocess
import sys

from app.errors import ControlPlaneError, ErrorCode


class LocalProcessRuntime:
    """以本地子进程模拟 microVM 的启动与销毁。

    每个“虚拟机”是一个 `sleep` 式的占位子进程；启动失败抛 RUNTIME_ERROR，
    由上层负责回滚已占用的资源。
    """

    def __init__(self) -> None:
        self._procs: dict[int, subprocess.Popen[bytes]] = {}

    def start(self, instance_id: str) -> int:
        """启动模拟进程，返回 pid。失败抛 RUNTIME_ERROR。"""
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(3600)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise ControlPlaneError(
                ErrorCode.RUNTIME_ERROR, f"failed to spawn microvm process: {exc}"
            ) from exc
        self._procs[proc.pid] = proc
        return proc.pid

    def stop(self, pid: int) -> None:
        """终止模拟进程；进程已退出视为成功（幂等）。"""
        proc = self._procs.pop(pid, None)
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def is_alive(self, pid: int) -> bool:
        """检查模拟进程是否存活。"""
        proc = self._procs.get(pid)
        return proc is not None and proc.poll() is None
