"""本机平台沙箱的选择与权限 profile 测试。"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from aurora.agent.sandbox import (
    ExecutionResult,
    LocalSandboxExecutor,
    Sandbox,
    SandboxUnavailableError,
    create_sandbox,
)


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        LocalSandboxExecutor(mode="unknown")


def test_default_factory_uses_local_sandbox(tmp_path):
    sandbox = create_sandbox(root=str(tmp_path))
    assert sandbox.backend_name == "local"


def test_danger_mode_requires_explicit_factory_choice(tmp_path):
    sandbox = create_sandbox(root=str(tmp_path), mode="danger-full-access")
    assert sandbox.backend_name == "unsafe"


def test_linux_prefers_bwrap(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    executor = LocalSandboxExecutor(platform_name="linux")
    executor.prepare(tmp_path)
    assert executor.name == "bwrap"


def test_linux_falls_back_to_landlock(monkeypatch, tmp_path):
    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1 if command[0] == "bwrap" else 0)

    monkeypatch.setattr(subprocess, "run", probe)
    executor = LocalSandboxExecutor(platform_name="linux")
    executor.prepare(tmp_path)
    assert executor.name == "landlock"
    assert len(calls) == 2


def test_missing_runner_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    executor = LocalSandboxExecutor(platform_name="darwin")
    with pytest.raises(SandboxUnavailableError):
        executor.prepare(tmp_path)


def test_runner_failure_is_not_reported_as_command_result():
    executor = LocalSandboxExecutor(platform_name="linux")
    executor._runner = "landlock"
    result = ExecutionResult(125, "", "aurora-landlock: denied", False, False, 1)
    with pytest.raises(SandboxUnavailableError):
        executor._raise_runner_failure(result)


def test_bwrap_workspace_profile_only_rebinds_workspace(tmp_path):
    args = LocalSandboxExecutor._bwrap_args(tmp_path, "workspace-write")
    assert args[:4] == ["--ro-bind", "/", "/", "--dev"]
    assert ["--bind", str(tmp_path), str(tmp_path)] == args[-3:]
    assert "--tmpfs" in args


def test_bwrap_read_only_profile_has_no_write_mount(tmp_path):
    args = LocalSandboxExecutor._bwrap_args(tmp_path, "read-only")
    assert "--bind" not in args
    assert "--tmpfs" not in args


def test_seatbelt_profile_denies_writes_and_allows_workspace(tmp_path):
    args = LocalSandboxExecutor._seatbelt_args(tmp_path, "workspace-write")
    profile = args[1]
    assert "(deny file-write*)" in profile
    assert str(tmp_path) in profile


def test_output_limit_is_applied_while_process_runs(tmp_path):
    sandbox = Sandbox(
        root=tmp_path,
        executor=LocalSandboxExecutor(mode="danger-full-access", max_output_bytes=32),
    )
    result = sandbox.run_python("print('x' * 10000)")
    assert result.truncated
    assert len(result.stdout.encode()) == 32
