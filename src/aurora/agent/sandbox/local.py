"""跨平台本机文件写入沙箱。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .executor import (
    MAX_OUTPUT_BYTES,
    ExecutionResult,
    SandboxMode,
    SandboxUnavailableError,
    run_process,
)


class LocalSandboxExecutor:
    """按平台选择 Bubblewrap、Landlock、Seatbelt 或 Windows ACL runner。"""

    def __init__(
        self,
        *,
        mode: SandboxMode = "workspace-write",
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        probe_timeout: float = 5.0,
        platform_name: str | None = None,
    ) -> None:
        if mode not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError(f"未知沙箱模式: {mode}")
        self.mode = mode
        self._max_output_bytes = max_output_bytes
        self._probe_timeout = probe_timeout
        self._platform = platform_name or sys.platform
        self._runner: str | None = None

    @property
    def name(self) -> str:
        """返回实际选择的执行后端。"""
        if self.mode == "danger-full-access":
            return "danger-full-access"
        return self._runner or "local"

    def python_argv(self, script_name: str) -> list[str]:
        """返回宿主 Python 命令。"""
        return [sys.executable, script_name]

    def command_argv(self, command: str) -> list[str]:
        """返回平台对应的 shell 命令。"""
        if self._platform == "win32":
            shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
            return [shell, "-NoProfile", "-NonInteractive", "-Command", command]
        shell = shutil.which("bash") or "/bin/sh"
        return [shell, "-c", command]

    def prepare(self, cwd: Path) -> None:
        """功能探测并缓存当前平台可用的 runner。"""
        if self.mode == "danger-full-access":
            self._runner = "unsafe"
            return
        if self._runner is not None:
            return
        candidates = self._candidates()
        for candidate in candidates:
            if self._probe(candidate, cwd):
                self._runner = candidate
                return
        choices = ", ".join(candidates) or "无"
        raise SandboxUnavailableError(
            f"当前平台没有可用的本机沙箱（候选: {choices}），拒绝无隔离执行"
        )

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> ExecutionResult:
        """在选定的平台 runner 内执行命令。"""
        self.prepare(cwd)
        wrapped = argv if self.mode == "danger-full-access" else self._wrap(argv, cwd)
        result = run_process(
            wrapped,
            cwd=cwd,
            timeout=timeout,
            env=env,
            stdin=stdin,
            max_output_bytes=self._max_output_bytes,
        )
        self._raise_runner_failure(result)
        return result

    def _raise_runner_failure(self, result: ExecutionResult) -> None:
        """把 runner 自身故障与目标命令失败区分开。"""
        signatures = {
            "bwrap": (None, "bwrap:"),
            "landlock": (125, "aurora-landlock:"),
            "seatbelt": (None, "sandbox-exec:"),
            "windows-acl": (127, "aurora-windows-acl:"),
        }
        expected_exit, signature = signatures.get(self._runner, (None, ""))
        exit_matches = expected_exit is None or result.exit_code == expected_exit
        if signature and exit_matches and any(line.startswith(signature) for line in result.stderr.splitlines()):
            raise SandboxUnavailableError(result.stderr.strip())

    def _candidates(self) -> tuple[str, ...]:
        """返回当前平台的 runner 优先级。"""
        if self._platform.startswith("linux"):
            return ("bwrap", "landlock")
        if self._platform == "darwin":
            return ("seatbelt",)
        if self._platform == "win32":
            return ("windows-acl",)
        return ()

    def _probe(self, runner: str, cwd: Path) -> bool:
        """验证 runner 能实际施加限制。"""
        try:
            command = self._probe_argv(runner, cwd)
            result = subprocess.run(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self._probe_timeout,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _probe_argv(self, runner: str, cwd: Path) -> list[str]:
        """构造功能探测命令。"""
        if runner == "bwrap":
            return ["bwrap", *self._bwrap_args(cwd, "read-only"), "--", "true"]
        if runner == "landlock":
            return [sys.executable, "-m", "aurora.agent.sandbox.landlock_runner", "--probe"]
        if runner == "seatbelt":
            return ["sandbox-exec", *self._seatbelt_args(cwd, "read-only"), "--", "true"]
        if runner == "windows-acl":
            return [sys.executable, "-m", "aurora.agent.sandbox.windows_runner", "--probe"]
        raise AssertionError(f"未知 runner: {runner}")

    def _wrap(self, argv: list[str], cwd: Path) -> list[str]:
        """用已选择的 runner 包装目标命令。"""
        if self._runner == "bwrap":
            return ["bwrap", *self._bwrap_args(cwd, self.mode), "--", *argv]
        if self._runner == "landlock":
            return [
                sys.executable,
                "-m",
                "aurora.agent.sandbox.landlock_runner",
                "--mode",
                self.mode,
                "--workspace",
                str(cwd),
                "--",
                *argv,
            ]
        if self._runner == "seatbelt":
            return ["sandbox-exec", *self._seatbelt_args(cwd, self.mode), "--", *argv]
        if self._runner == "windows-acl":
            return [
                sys.executable,
                "-m",
                "aurora.agent.sandbox.windows_runner",
                "--mode",
                self.mode,
                "--workspace",
                str(cwd),
                "--",
                *argv,
            ]
        raise SandboxUnavailableError("本机沙箱尚未准备")

    @staticmethod
    def _bwrap_args(cwd: Path, mode: SandboxMode) -> list[str]:
        """构造 Bubblewrap 文件系统 profile。"""
        args = ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--die-with-parent"]
        if mode == "workspace-write":
            args.extend(["--tmpfs", "/tmp", "--bind", str(cwd), str(cwd)])
        return args

    @staticmethod
    def _seatbelt_args(cwd: Path, mode: SandboxMode) -> list[str]:
        """构造 Seatbelt 文件写入 profile。"""
        forms = ["(version 1)", "(allow default)", "(deny file-write*)", '(allow file-write* (literal "/dev/null"))']
        if mode == "workspace-write":
            roots = {str(cwd.resolve()), str(Path("/tmp").resolve()), str(Path(os.getenv("TMPDIR", "/tmp")).resolve())}
            grants = " ".join(f'(subpath "{_escape_sbpl(root)}")' for root in sorted(roots))
            forms.append(f"(allow file-write* {grants})")
        return ["-p", " ".join(forms)]


def _escape_sbpl(value: str) -> str:
    """转义 Seatbelt 字符串。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')
