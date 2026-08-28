"""stdio MCP 客户端与 Aurora Tool 适配测试。"""

from __future__ import annotations

import sys
from pathlib import Path

from mcp import types

from aurora.agent.core import Effort, NoClarifier
from aurora.agent.mcp import (
    McpPackageRegistry,
    McpServerConfig,
    StdioMcpClient,
    build_mcp_tools,
    load_package_directory,
)
from aurora.agent.runtime import AgentRuntime
from aurora.agent.sandbox import Sandbox, UnsafeSubprocessExecutor
from aurora.agent.tools import RiskLevel
from aurora.agent.transport import RuntimeApi

SERVER = Path(__file__).parent / "fixtures" / "mcp_server.py"


def server_config(name: str = "test") -> McpServerConfig:
    """创建测试 MCP Server 配置。"""
    return McpServerConfig(
        name=name,
        command=sys.executable,
        args=(str(SERVER),),
        timeout=10,
    )


class McpPlanner:
    """调用测试 MCP 加法工具的固定规划器。"""

    def __init__(self, llm, tools):
        self.tools = tools

    def plan(self, goal):
        return [
            {
                "id": "mcp-1",
                "description": "调用 MCP 加法",
                "effort": Effort.LOW,
                "tool": "mcp.test.add",
                "args": {"a": 2, "b": 3},
            }
        ]


def make_runtime():
    """创建不访问模型和系统沙箱的运行时。"""
    return AgentRuntime(
        llm_factory=lambda: object(),
        sandbox_factory=lambda root, mode: Sandbox(root, executor=UnsafeSubprocessExecutor()),
        planner_factory=McpPlanner,
        clarifier_factory=lambda llm: NoClarifier(),
        mcp_packages=McpPackageRegistry(discover_plugins=False),
    )


def test_stdio_client_discovers_and_calls_tools():
    client = StdioMcpClient(server_config())
    try:
        client.connect()
        tools = build_mcp_tools(client)
        assert set(tools) == {"mcp.test.add", "mcp.test.store"}
        assert tools["mcp.test.add"].risk == RiskLevel.READ
        assert tools["mcp.test.store"].risk == RiskLevel.EXECUTE
        assert tools["mcp.test.add"].params_schema["required"] == ["a", "b"]
        assert tools["mcp.test.add"].run(a=2, b=4) == "6"
    finally:
        client.close()
    assert not client.connected


def test_runtime_includes_mcp_tools_in_new_sessions(tmp_path):
    runtime = make_runtime()
    try:
        status = runtime.connect_mcp_server(server_config())
        assert status["connected"]
        assert {tool["name"] for tool in status["tools"]} == {
            "mcp.test.add",
            "mcp.test.store",
        }
        update = runtime.create_session(str(tmp_path), approval_mode="always").start("计算")
        assert update.state["results"][0]["output"] == "5"
    finally:
        runtime.close()


def test_runtime_api_manages_mcp_servers():
    api = RuntimeApi(make_runtime())
    try:
        connected = api.handle(
            {
                "id": "1",
                "method": "mcp.server.connect",
                "params": server_config().to_dict() | {"args": [str(SERVER)]},
            }
        )[0]
        assert connected["result"]["connected"]
        listed = api.handle({"id": "2", "method": "mcp.server.list"})[0]
        assert listed["result"]["servers"][0]["name"] == "test"
        disconnected = api.handle(
            {
                "id": "3",
                "method": "mcp.server.disconnect",
                "params": {"name": "test"},
            }
        )[0]
        assert disconnected["result"]["disconnected"]
    finally:
        api.close()


def test_builtin_packages_expose_plugin_ready_manifests():
    registry = McpPackageRegistry(discover_plugins=False)
    manifests = {package.manifest.id: package.manifest.to_dict() for package in registry.list()}
    assert set(manifests) == {"blender", "qq"}
    assert manifests["blender"]["configSchema"]["properties"]["command"]["default"] == "uvx"
    assert manifests["qq"]["configSchema"]["required"] == ["command"]
    assert manifests["qq"]["functions"]["message.send"]["tools"] == ["send_*"]


def test_runtime_api_connects_package_through_unified_interface():
    api = RuntimeApi(make_runtime())
    try:
        catalog = api.handle({"id": "1", "method": "mcp.package.catalog"})[0]
        assert {item["id"] for item in catalog["result"]["packages"]} == {"blender", "qq"}
        connected = api.handle(
            {
                "id": "2",
                "method": "mcp.package.connect",
                "params": {
                    "packageId": "qq",
                    "instanceName": "qq-test",
                    "config": {
                        "command": sys.executable,
                        "args": [str(SERVER)],
                        "timeout": 10,
                    },
                },
            }
        )[0]
        assert connected["result"]["packageId"] == "qq"
        assert {tool["name"] for tool in connected["result"]["tools"]} == {
            "mcp.qq-test.add",
            "mcp.qq-test.store",
        }
        listed = api.handle({"id": "3", "method": "mcp.package.list"})[0]
        assert listed["result"]["packages"][0]["name"] == "qq-test"
        disconnected = api.handle(
            {
                "id": "4",
                "method": "mcp.package.disconnect",
                "params": {"instanceName": "qq-test"},
            }
        )[0]
        assert disconnected["result"]["disconnected"]
    finally:
        api.close()


def test_qq_package_never_downgrades_send_tools():
    package = McpPackageRegistry(discover_plugins=False).get("qq")
    remote = types.Tool(
        name="send_group_message",
        inputSchema={"type": "object"},
        annotations=types.ToolAnnotations(readOnlyHint=True),
    )
    assert package.tool_risk(remote, RiskLevel.READ) == RiskLevel.EXECUTE


def test_yaml_directory_is_a_complete_package_unit(tmp_path):
    directory = tmp_path / "calendar"
    directory.mkdir()
    (directory / "package.yaml").write_text(
        """schemaVersion: 1
package:
  id: calendar
  name: Calendar
  version: 1.0.0
  description: Calendar test package
server:
  command: calendar-mcp
functions:
  event.create:
    tools: [create_*]
    risk: execute
configSchema:
  type: object
""",
        encoding="utf-8",
    )
    package = load_package_directory(directory)
    config = package.build_server_config("work-calendar", {})
    remote = types.Tool(name="create_event", inputSchema={"type": "object"})
    assert package.manifest.id == "calendar"
    assert config.command == "calendar-mcp"
    assert package.tool_risk(remote, RiskLevel.READ) == RiskLevel.EXECUTE
