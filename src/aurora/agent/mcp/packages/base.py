"""MCP 功能包的统一接口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from mcp import types

from ...tools import RiskLevel
from ..config import McpServerConfig


@dataclass(frozen=True)
class McpPackageManifest:
    """描述可安装和连接的 MCP 功能包。"""

    id: str
    name: str
    version: str
    description: str
    config_schema: Mapping[str, Any]
    functions: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """转换为前端可用的功能包清单。"""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "configSchema": dict(self.config_schema),
            "functions": dict(self.functions),
        }


@runtime_checkable
class McpPackage(Protocol):
    """定义 MCP 功能包需要实现的稳定边界。"""

    manifest: McpPackageManifest

    def build_server_config(
        self,
        instance_name: str,
        config: Mapping[str, Any],
    ) -> McpServerConfig:
        """把功能配置转换为底层 MCP Server 配置。"""
        ...

    def tool_risk(self, tool: types.Tool, default: RiskLevel) -> RiskLevel:
        """覆盖功能包内工具的风险等级。"""
        ...


class BaseMcpPackage:
    """提供保守风险策略和通用 stdio 配置解析。"""

    manifest: McpPackageManifest
    default_command: str | None = None
    default_args: tuple[str, ...] = ()
    default_timeout: float = 30.0

    def build_server_config(
        self,
        instance_name: str,
        config: Mapping[str, Any],
    ) -> McpServerConfig:
        """使用默认值和用户覆盖构造 stdio 配置。"""
        value = dict(config)
        value["name"] = instance_name
        if "command" not in value and self.default_command is not None:
            value["command"] = self.default_command
        if "args" not in value:
            value["args"] = list(self.default_args)
        if "timeout" not in value:
            value["timeout"] = self.default_timeout
        return McpServerConfig.from_mapping(value)

    def tool_risk(self, tool: types.Tool, default: RiskLevel) -> RiskLevel:
        """沿用 MCP annotations 推导出的保守等级。"""
        return default
