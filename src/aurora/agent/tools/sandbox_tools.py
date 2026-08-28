"""沙箱工具：在隔离工作区内写文件与执行代码。"""

from __future__ import annotations

from ..sandbox import Sandbox, get_sandbox
from .base import RiskLevel, Tool, tool


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
    description="通过标准输入临时运行 Python 代码，不创建脚本文件，返回退出码与输出",
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


def build_sandbox_tools(sandbox: Sandbox) -> dict[str, Tool]:
    """创建绑定到指定沙箱实例的工具集合。"""

    def bound_write_file(path: str, content: str) -> str:
        """在绑定沙箱内写入 UTF-8 文本文件。"""
        return sandbox.write_file(path, content)

    def bound_run_command(command: str, timeout: int = 30) -> str:
        """在绑定沙箱内运行 shell 命令。"""
        return sandbox.run(command, timeout=timeout).render()

    def bound_run_python(code: str, timeout: int = 30) -> str:
        """在绑定沙箱内运行 Python 代码。"""
        return sandbox.run_python(code, timeout=timeout).render()

    def bound_list_files(path: str = ".") -> str:
        """列出绑定沙箱目录结构。"""
        return sandbox.list_files(path)

    def bound_read_file(path: str) -> str:
        """读取绑定沙箱内的 UTF-8 文本文件。"""
        return sandbox.read_file(path)

    definitions = (
        ("list_files", "递归列出当前工作区内的文件结构", bound_list_files, RiskLevel.READ),
        ("read_file", "读取当前工作区内的 UTF-8 文本文件", bound_read_file, RiskLevel.READ),
        ("write_file", "在沙箱目录内写入或覆盖 UTF-8 文本文件（相对沙箱根路径）", bound_write_file, RiskLevel.WRITE),
        ("run_command", "在沙箱目录内运行一条 shell 命令，返回退出码与标准输出", bound_run_command, RiskLevel.EXECUTE),
        (
            "run_python",
            "通过标准输入临时运行 Python 代码，不创建脚本文件，返回退出码与输出",
            bound_run_python,
            RiskLevel.EXECUTE,
        ),
        ("sandbox_list_files", "递归列出沙箱目录内的文件结构", bound_list_files, RiskLevel.READ),
        ("sandbox_read_file", "读取沙箱目录内的 UTF-8 文本文件", bound_read_file, RiskLevel.READ),
    )
    return {
        name: Tool(name, description, func, risk)
        for name, description, func, risk in definitions
    }
