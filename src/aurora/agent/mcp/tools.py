"""MCP Tool 到 Aurora Tool 的适配。"""

from __future__ import annotations

from mcp import types

from ..tools import RiskLevel, Tool
from .client import StdioMcpClient
from .packages import McpPackage


def build_mcp_tools(
    client: StdioMcpClient,
    package: McpPackage | None = None,
) -> dict[str, Tool]:
    """发现远端工具并转换成带命名空间的 Aurora Tool。"""
    tools: dict[str, Tool] = {}
    for remote in client.list_tools():
        name = f"mcp.{client.config.name}.{remote.name}"

        def invoke(_remote_name: str = remote.name, **kwargs) -> str:
            """调用绑定的远端 MCP Tool。"""
            return client.call_tool(_remote_name, kwargs)

        tools[name] = Tool(
            name=name,
            description=remote.description or remote.title or f"调用 {remote.name}",
            func=invoke,
            risk=_risk_for(remote, package),
            params_schema=remote.input_schema,
        )
    return tools


def _risk_for(tool: types.Tool, package: McpPackage | None) -> RiskLevel:
    """根据 MCP Tool annotations 采用保守风险分级。"""
    risk = RiskLevel.EXECUTE
    if tool.annotations is not None and tool.annotations.read_only_hint is True:
        risk = RiskLevel.READ
    return package.tool_risk(tool, risk) if package is not None else risk
