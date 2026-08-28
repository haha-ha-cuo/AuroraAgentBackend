"""跨平台本机沙箱与默认实例。"""

from __future__ import annotations

from .executor import (
    ExecutionResult,
    SandboxExecutor,
    SandboxMode,
    SandboxUnavailableError,
    UnsafeSubprocessExecutor,
)
from .local import LocalSandboxExecutor
from .sandbox import DEFAULT_ROOT, Sandbox

_current: Sandbox | None = None

__all__ = [
    "Sandbox",
    "LocalSandboxExecutor",
    "UnsafeSubprocessExecutor",
    "SandboxExecutor",
    "SandboxMode",
    "SandboxUnavailableError",
    "ExecutionResult",
    "DEFAULT_ROOT",
    "create_sandbox",
    "get_sandbox",
    "set_sandbox",
]


def create_sandbox(
    root: str | None = None,
    mode: SandboxMode = "workspace-write",
    default_timeout: float = 30.0,
) -> Sandbox:
    """按权限模式创建本机沙箱。"""
    executor: SandboxExecutor
    if mode == "danger-full-access":
        executor = UnsafeSubprocessExecutor()
    else:
        executor = LocalSandboxExecutor(mode=mode)
    return Sandbox(root=root, executor=executor, default_timeout=default_timeout, mode=mode)


def set_sandbox(sandbox: Sandbox) -> None:
    """注入当前进程使用的沙箱实例。"""
    global _current
    _current = sandbox


def get_sandbox() -> Sandbox:
    """返回当前沙箱实例，未注入时创建默认本机沙箱。"""
    global _current
    if _current is None:
        _current = create_sandbox()
    return _current
