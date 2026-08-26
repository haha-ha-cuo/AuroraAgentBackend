"""内置工具：list_files / read_file（声明式注册）。"""

from __future__ import annotations

from pathlib import Path

from .base import RiskLevel, tool


@tool(
    name="list_files",
    description="递归列出目录下的文件与子目录结构（跳过隐藏文件与构建缓存）",
    risk=RiskLevel.READ,
)
def list_files(path: str = ".") -> str:
    """递归列出目录结构（跳过隐藏文件与构建缓存）。"""
    root = Path(path).resolve()
    if not root.exists():
        return f"路径不存在: {root}"
    if not root.is_dir():
        return f"不是目录: {root}"

    lines: list[str] = []
    for item in sorted(root.rglob("*")):
        rel = item.relative_to(root)
        if _should_skip(rel):
            continue
        indent = "  " * (len(rel.parts) - 1)
        suffix = "/" if item.is_dir() else ""
        lines.append(f"{indent}{rel.name}{suffix}")

    if not lines:
        return f"（空目录）{root}"
    return "\n".join(lines)


@tool(
    name="read_file",
    description="读取 UTF-8 文本文件内容",
    risk=RiskLevel.READ,
)
def read_file(path: str) -> str:
    """读取 UTF-8 文本文件内容。"""
    target = Path(path)
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


def _should_skip(rel: Path) -> bool:
    """跳过隐藏文件/目录与常见构建缓存。"""
    for part in rel.parts:
        if part.startswith("."):
            return True
        if part in {"__pycache__", "node_modules"}:
            return True
        if part.endswith(".egg-info"):
            return True
    return False
