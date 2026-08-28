"""从软件目录中的 YAML 加载 MCP 功能包。"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Mapping

import yaml
from mcp import types

from ...tools import RiskLevel
from .base import BaseMcpPackage, McpPackageManifest

SCHEMA_VERSION = 1


class YamlMcpPackage(BaseMcpPackage):
    """由 package.yaml 驱动的 MCP 功能包。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()
        document = _load_document(self.directory / "package.yaml")
        _validate_document(document, self.directory)
        package = document["package"]
        server = document.get("server", {})
        functions = document.get("functions", {})
        self.manifest = McpPackageManifest(
            id=package["id"],
            name=package["name"],
            version=str(package["version"]),
            description=package["description"],
            config_schema=document.get("configSchema", {"type": "object"}),
            functions=functions,
        )
        self.default_command = server.get("command")
        self.default_args = tuple(server.get("args", []))
        self.default_timeout = float(server.get("timeout", 30))
        self._functions = functions

    def tool_risk(self, tool: types.Tool, default: RiskLevel) -> RiskLevel:
        """按照 YAML 中的功能与工具匹配规则确定风险。"""
        selected = default
        for definition in self._functions.values():
            if any(fnmatchcase(tool.name, pattern) for pattern in definition.get("tools", [])):
                selected = RiskLevel(definition.get("risk", default.value))
        return selected


def load_package_directory(directory: str | Path) -> YamlMcpPackage:
    """加载一个包含 package.yaml 的软件功能目录。"""
    return YamlMcpPackage(directory)


def discover_package_directories(root: str | Path) -> list[YamlMcpPackage]:
    """扫描根目录下所有 YAML 功能包。"""
    base = Path(root)
    return [
        load_package_directory(directory)
        for directory in sorted(base.iterdir())
        if directory.is_dir() and (directory / "package.yaml").is_file()
    ]


def _load_document(path: Path) -> dict[str, Any]:
    """安全读取 YAML 文档。"""
    if not path.is_file():
        raise ValueError(f"MCP 功能包缺少 package.yaml: {path.parent}")
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"MCP 功能包 YAML 顶层必须是对象: {path}")
    return value


def _validate_document(document: Mapping[str, Any], directory: Path) -> None:
    """校验 YAML 功能包的必要字段和字段类型。"""
    if document.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError(f"MCP 功能包 schemaVersion 必须为 {SCHEMA_VERSION}: {directory}")
    package = document.get("package")
    if not isinstance(package, Mapping):
        raise ValueError(f"MCP 功能包缺少 package 对象: {directory}")
    for field in ("id", "name", "description"):
        if not isinstance(package.get(field), str) or not package[field].strip():
            raise ValueError(f"MCP 功能包 package.{field} 非法: {directory}")
    if not isinstance(package.get("version"), (str, int, float)):
        raise ValueError(f"MCP 功能包 package.version 非法: {directory}")
    server = document.get("server", {})
    if not isinstance(server, Mapping):
        raise ValueError(f"MCP 功能包 server 必须是对象: {directory}")
    if server.get("transport", "stdio") != "stdio":
        raise ValueError(f"MCP 功能包当前仅支持 stdio transport: {directory}")
    command = server.get("command")
    if command is not None and not isinstance(command, str):
        raise ValueError(f"MCP 功能包 server.command 必须是字符串: {directory}")
    args = server.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValueError(f"MCP 功能包 server.args 必须是字符串数组: {directory}")
    functions = document.get("functions", {})
    if not isinstance(functions, Mapping):
        raise ValueError(f"MCP 功能包 functions 必须是对象: {directory}")
    for name, definition in functions.items():
        if not isinstance(name, str) or not isinstance(definition, Mapping):
            raise ValueError(f"MCP 功能包 function 定义非法: {directory}")
        tools = definition.get("tools", [])
        if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
            raise ValueError(f"MCP 功能包 functions.{name}.tools 非法: {directory}")
        risk = definition.get("risk")
        if risk is not None:
            RiskLevel(risk)
    config_schema = document.get("configSchema", {"type": "object"})
    if not isinstance(config_schema, Mapping):
        raise ValueError(f"MCP 功能包 configSchema 必须是对象: {directory}")
