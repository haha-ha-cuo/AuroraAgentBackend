"""沙箱工具：在隔离工作区内写文件与执行代码。"""

from __future__ import annotations

from ..sandbox import get_sandbox
from .base import RiskLevel, tool


@tool(
    name="write_file",
    description="在沙箱目录内写入或覆盖 UTF-8 文本文件（相对沙箱根路径）",
    risk=RiskLevel.WRITE,
)
def write_file(path: str, content: str) -> str:
    """在沙箱内写入 UTF-8 文本文件。"""
    return get_sandbox().write_file(path, content)


@tool(
    name="run_command",
    description="在沙箱目录内运行一条 shell 命令，返回退出码与标准输出",
    risk=RiskLevel.EXECUTE,
)
def run_command(command: str, timeout: int = 30) -> str:
    """在沙箱内运行 shell 命令。"""
    return get_sandbox().run(command, timeout=timeout).render()


@tool(
    name="run_python",
    description="在沙箱目录内运行一段 Python 代码，返回退出码与标准输出",
    risk=RiskLevel.EXECUTE,
)
def run_python(code: str, timeout: int = 30) -> str:
    """在沙箱内运行 Python 代码。"""
    return get_sandbox().run_python(code, timeout=timeout).render()


@tool(
    name="sandbox_list_files",
    description="递归列出沙箱目录内的文件结构",
    risk=RiskLevel.READ,
)
def sandbox_list_files(path: str = ".") -> str:
    """列出沙箱目录结构。"""
    return get_sandbox().list_files(path)


@tool(
    name="sandbox_read_file",
    description="读取沙箱目录内的 UTF-8 文本文件",
    risk=RiskLevel.READ,
)
def sandbox_read_file(path: str) -> str:
    """读取沙箱内的 UTF-8 文本文件。"""
    return get_sandbox().read_file(path)
