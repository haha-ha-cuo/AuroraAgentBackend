"""沙箱工作区门面：路径圈定、文件读写与命令执行。"""

from __future__ import annotations

import os
from pathlib import Path

from .executor import ExecutionResult, SandboxExecutor, SandboxMode
from .local import LocalSandboxExecutor

DEFAULT_ROOT = Path(os.environ.get("AURORA_SANDBOX_DIR") or Path.home() / ".aurora" / "sandbox")
_SKIP_PARTS = {"__pycache__", "node_modules", ".git"}


class Sandbox:
    """一个隔离的沙箱工作区，所有读写与执行都圈定在 root 内。"""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        executor: SandboxExecutor | None = None,
        default_timeout: float = 30.0,
        mode: SandboxMode = "workspace-write",
    ) -> None:
        self.root = (Path(root) if root is not None else DEFAULT_ROOT).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._executor = executor or LocalSandboxExecutor(mode=mode)
        self._default_timeout = default_timeout
        self.mode = mode

    @property
    def backend_name(self) -> str:
        """返回当前执行后端名称。"""
        return self._executor.name

    def resolve(self, path: str) -> Path:
        """把相对路径解析到沙箱内，拒绝越界与绝对路径逃逸。"""
        raw = Path(path)
        if raw.is_absolute():
            raise ValueError(f"不允许绝对路径: {path}")
        target = (self.root / raw).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError(f"路径越界沙箱: {path}")
        return target

    def write_file(self, path: str, content: str) -> str:
        """在沙箱内写入 UTF-8 文本文件。"""
        self._ensure_writable()
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        rel = target.relative_to(self.root)
        return f"已写入 {rel}（{len(content)} 字符）"

    def read_file(self, path: str) -> str:
        """读取沙箱内的 UTF-8 文本文件。"""
        target = self.resolve(path)
        if not target.exists():
            return f"文件不存在: {target}"
        if target.is_dir():
            return f"是目录而非文件: {target}"
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"无法按 UTF-8 读取（可能是二进制文件）: {target}"
        except OSError as exc:
            return f"读取失败: {exc}"

    def list_files(self, path: str = ".") -> str:
        """递归列出沙箱内的目录结构。"""
        base = self.resolve(path)
        if not base.exists():
            return f"路径不存在: {base}"
        if not base.is_dir():
            return f"不是目录: {base}"

        lines: list[str] = []
        for item in sorted(base.rglob("*")):
            rel = item.relative_to(base)
            if _should_skip(rel):
                continue
            indent = "  " * (len(rel.parts) - 1)
            suffix = "/" if item.is_dir() else ""
            lines.append(f"{indent}{rel.name}{suffix}")
        if not lines:
            return f"（空目录）{base}"
        return "\n".join(lines)

    def prepare(self) -> None:
        """探测并准备执行后端。"""
        self._executor.prepare(self.root)

    def run(self, command: str, timeout: float | None = None) -> ExecutionResult:
        """在沙箱内运行一条 shell 命令。"""
        return self._executor.run(
            self._executor.command_argv(command),
            cwd=self.root,
            timeout=timeout or self._default_timeout,
        )

    def run_python(self, code: str, timeout: float | None = None) -> ExecutionResult:
        """通过标准输入运行 Python 代码且不创建临时脚本。"""
        return self._executor.run(
            self._executor.python_argv("-"),
            cwd=self.root,
            timeout=timeout or self._default_timeout,
            stdin=code,
        )

    def _ensure_writable(self) -> None:
        """拒绝只读模式下由门面直接发起的写入。"""
        if self.mode == "read-only":
            raise PermissionError("只读沙箱不允许写入文件")


def _should_skip(rel: Path) -> bool:
    """跳过隐藏文件与常见构建缓存。"""
    for part in rel.parts:
        if part.startswith("."):
            return True
        if part in _SKIP_PARTS:
            return True
    return False
