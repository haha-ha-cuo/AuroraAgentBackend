"""MCP 客户端、配置与 Aurora Tool 适配。"""

from .client import McpCallError, McpClientError, McpConnectionError, StdioMcpClient
from .config import McpServerConfig
from .packages import (
    ENTRY_POINT_GROUP,
    BaseMcpPackage,
    McpPackage,
    McpPackageManifest,
    McpPackageRegistry,
    YamlMcpPackage,
    discover_package_directories,
    load_package_directory,
)
from .tools import build_mcp_tools

__all__ = [
    "McpCallError",
    "McpClientError",
    "McpConnectionError",
    "McpServerConfig",
    "McpPackage",
    "McpPackageManifest",
    "McpPackageRegistry",
    "BaseMcpPackage",
    "YamlMcpPackage",
    "discover_package_directories",
    "load_package_directory",
    "ENTRY_POINT_GROUP",
    "StdioMcpClient",
    "build_mcp_tools",
]
