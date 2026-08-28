"""沙箱执行协议与共享进程管理。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

MAX_OUTPUT_BYTES = 64 * 1024
SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]


class SandboxUnavailableError(RuntimeError):
    """请求的本机沙箱无法安全启用。"""


@dataclass
class ExecutionResult:
    """一次执行的结果。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int

    def render(self) -> str:
        """渲染为给模型使用的文本。"""
        status = f"退出码 {self.exit_code}"
        if self.timed_out:
            status += "（超时，进程已终止）"
        if self.truncated:
            status += "（输出过长已截断）"
        parts = [status]
        if self.stdout.strip():
            parts.append(self.stdout.rstrip())
        if self.stderr.strip():
            parts.append("[stderr]\n" + self.stderr.rstrip())
        return "\n".join(parts)


class SandboxExecutor(Protocol):
    """沙箱执行后端协议。"""

    @property
    def name(self) -> str: ...

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> ExecutionResult: ...

    def python_argv(self, script_name: str) -> list[str]: ...

    def command_argv(self, command: str) -> list[str]: ...

    def prepare(self, cwd: Path) -> None: ...


class UnsafeSubprocessExecutor:
    """仅供用户显式选择的无隔离执行后端。"""

    @property
    def name(self) -> str:
        return "unsafe"

    def python_argv(self, script_name: str) -> list[str]:
        """返回宿主 Python 命令。"""
        return [sys.executable, script_name]

    def command_argv(self, command: str) -> list[str]:
        """返回当前平台的命令解释器参数。"""
        if os.name == "nt":
            return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        return ["/bin/sh", "-c", command]

    def prepare(self, cwd: Path) -> None:
        """无隔离后端无需准备。"""

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> ExecutionResult:
        """在宿主环境直接执行命令。"""
        return run_process(argv, cwd=cwd, timeout=timeout, env=env, stdin=stdin)


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    popen_kwargs: dict | None = None,
    on_started: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> ExecutionResult:
    """执行进程并以固定内存上限收集输出。"""
    started = time.monotonic()
    child_env = dict(os.environ)
    if env:
        child_env.update(env)
    kwargs = dict(popen_kwargs or {})
    if os.name == "posix":
        kwargs.setdefault("start_new_session", True)
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=child_env,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    if on_started:
        on_started(process)
    stdout = bytearray()
    stderr = bytearray()
    truncated = [False, False]
    threads = [
        threading.Thread(target=_drain, args=(process.stdout, stdout, max_output_bytes, truncated, 0), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, stderr, max_output_bytes, truncated, 1), daemon=True),
    ]
    if stdin is not None:
        threads.append(
            threading.Thread(
                target=_write_stdin,
                args=(process.stdin, stdin.encode("utf-8")),
                daemon=True,
            )
        )
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(process)
        process.wait()
    for thread in threads:
        thread.join()
    duration_ms = int((time.monotonic() - started) * 1000)
    return ExecutionResult(
        exit_code=process.returncode,
        stdout=bytes(stdout).decode("utf-8", errors="replace"),
        stderr=bytes(stderr).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        truncated=any(truncated),
        duration_ms=duration_ms,
    )


def _drain(pipe, output: bytearray, limit: int, flags: list[bool], index: int) -> None:
    """持续排空管道并只保留限定字节数。"""
    if pipe is None:
        return
    try:
        while chunk := pipe.read(8192):
            remaining = limit - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                flags[index] = True
    finally:
        pipe.close()


def _write_stdin(pipe, content: bytes) -> None:
    """写入进程标准输入并及时关闭管道。"""
    if pipe is None:
        return
    try:
        pipe.write(content)
        pipe.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        pipe.close()


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """终止执行进程及其派生进程。"""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except OSError:
        pass
