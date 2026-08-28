"""面向 CLI 与桌面前端的会话级 Agent 运行时。"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from aurora.text import sanitize_text, sanitize_value

from .core import LLMClarifier, LLMPlanner, build_delegation_graph
from .model_access import build_llm
from .mcp import (
    McpPackage,
    McpPackageRegistry,
    McpServerConfig,
    StdioMcpClient,
    build_mcp_tools,
)
from .safety import build_gate
from .sandbox import Sandbox, SandboxMode, create_sandbox
from .tools import Tool, build_sandbox_tools, get_available_tools


@dataclass(frozen=True)
class WorkspaceInfo:
    """经过规范化和可访问性检查的工作区信息。"""

    path: str
    name: str
    is_directory: bool
    is_git_repository: bool
    readable: bool
    writable: bool

    def to_dict(self) -> dict[str, Any]:
        """转换为前端协议字段。"""
        return {
            "path": self.path,
            "name": self.name,
            "isDirectory": self.is_directory,
            "isGitRepository": self.is_git_repository,
            "readable": self.readable,
            "writable": self.writable,
        }


@dataclass(frozen=True)
class RuntimeInterrupt:
    """一次等待上层应用响应的运行中断。"""

    id: str
    value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """转换为协议可序列化结构。"""
        return {"interruptId": self.id, **dict(self.value)}


@dataclass(frozen=True)
class RunUpdate:
    """一次启动或恢复操作产生的运行快照。"""

    session_id: str
    run_id: str
    status: str
    state: Mapping[str, Any]
    interruptions: tuple[RuntimeInterrupt, ...]

    def to_dict(self) -> dict[str, Any]:
        """转换为协议响应结构。"""
        return {
            "sessionId": self.session_id,
            "runId": self.run_id,
            "status": self.status,
            "state": dict(self.state),
            "interruptions": [item.to_dict() for item in self.interruptions],
        }


def validate_workspace(path: str | Path) -> WorkspaceInfo:
    """规范化并校验前端选择的工作区目录。"""
    candidate = Path(path).expanduser().resolve()
    if not candidate.exists():
        raise ValueError(f"工作区不存在: {candidate}")
    if not candidate.is_dir():
        raise ValueError(f"工作区不是目录: {candidate}")
    readable = os.access(candidate, os.R_OK | os.X_OK)
    if not readable:
        raise ValueError(f"工作区不可读取: {candidate}")
    return WorkspaceInfo(
        path=str(candidate),
        name=candidate.name,
        is_directory=True,
        is_git_repository=(candidate / ".git").exists(),
        readable=readable,
        writable=os.access(candidate, os.W_OK),
    )


class AgentSession:
    """持有独立工作区、沙箱、工具和执行图的 Agent 会话。"""

    def __init__(
        self,
        session_id: str,
        workspace: WorkspaceInfo,
        sandbox: Sandbox,
        graph: Any,
        llm: Any,
        planner: Any,
    ) -> None:
        self.id = session_id
        self.workspace = workspace
        self.sandbox = sandbox
        self.llm = llm
        self.planner = planner
        self._graph = graph
        self._run_ids: set[str] = set()

    def start(
        self,
        goal: str,
        on_progress: Callable[[str, str, Mapping[str, Any]], None] | None = None,
    ) -> RunUpdate:
        """启动一个目标并返回完成或等待输入的快照。"""
        clean_goal = sanitize_text(goal).strip()
        if not clean_goal:
            raise ValueError("目标不能为空")
        run_id = uuid4().hex
        self._run_ids.add(run_id)
        if on_progress is None:
            state = self._graph.invoke({"goal": clean_goal}, self._config(run_id))
            return self._update(run_id, state)
        on_progress(run_id, "run.started", {"goal": clean_goal})
        return self._stream(run_id, {"goal": clean_goal}, on_progress)

    def resume(
        self,
        run_id: str,
        response: Any,
        interrupt_id: str | None = None,
        on_progress: Callable[[str, str, Mapping[str, Any]], None] | None = None,
    ) -> RunUpdate:
        """用前端响应恢复一个等待中的运行。"""
        if run_id not in self._run_ids:
            raise ValueError(f"运行不存在或不属于当前会话: {run_id}")
        clean_response = sanitize_value(response)
        resume_value = {interrupt_id: clean_response} if interrupt_id else clean_response
        command = Command(resume=resume_value)
        if on_progress is None:
            state = self._graph.invoke(command, self._config(run_id))
            return self._update(run_id, state)
        on_progress(run_id, "run.resumed", {})
        return self._stream(run_id, command, on_progress)

    def _stream(
        self,
        run_id: str,
        value: Any,
        on_progress: Callable[[str, str, Mapping[str, Any]], None],
    ) -> RunUpdate:
        """流式执行图节点并返回最终快照。"""
        config = self._config(run_id)
        for chunk in self._graph.stream(value, config, stream_mode="updates"):
            for node, update in chunk.items():
                if node != "__interrupt__" and isinstance(update, Mapping):
                    on_progress(run_id, str(node), update)
        snapshot = self._graph.get_state(config)
        state = dict(snapshot.values)
        interruptions = [item for task in snapshot.tasks for item in task.interrupts]
        if interruptions:
            state["__interrupt__"] = interruptions
        return self._update(run_id, state)

    def run_until_complete(
        self,
        goal: str,
        responder: Callable[[Mapping[str, Any]], Any],
    ) -> Mapping[str, Any]:
        """使用同步响应器持续恢复运行，供 CLI 复用。"""
        update = self.start(goal)
        while update.interruptions:
            pending = update.interruptions[0]
            update = self.resume(update.run_id, responder(pending.value), pending.id)
        return update.state

    def _config(self, run_id: str) -> dict[str, dict[str, str]]:
        """构造 LangGraph 运行配置。"""
        return {"configurable": {"thread_id": run_id}}

    def _update(self, run_id: str, state: Mapping[str, Any]) -> RunUpdate:
        """把图状态转换为稳定的运行时快照。"""
        interruptions = tuple(
            RuntimeInterrupt(item.id, item.value)
            for item in state.get("__interrupt__", [])
        )
        public_state = {
            key: value
            for key, value in state.items()
            if key not in {"__interrupt__", "current_task"}
        }
        return RunUpdate(
            session_id=self.id,
            run_id=run_id,
            status="waiting" if interruptions else "completed",
            state=public_state,
            interruptions=interruptions,
        )


class AgentRuntime:
    """管理多个互相隔离的 Agent 会话。"""

    def __init__(
        self,
        *,
        llm_factory: Callable[[], Any] = build_llm,
        sandbox_factory: Callable[..., Sandbox] = create_sandbox,
        planner_factory: Callable[[Any, Mapping[str, Tool]], Any] = LLMPlanner,
        clarifier_factory: Callable[[Any], Any] = LLMClarifier,
        mcp_packages: McpPackageRegistry | None = None,
    ) -> None:
        self._llm_factory = llm_factory
        self._sandbox_factory = sandbox_factory
        self._planner_factory = planner_factory
        self._clarifier_factory = clarifier_factory
        self._sessions: dict[str, AgentSession] = {}
        self._mcp_packages = mcp_packages if mcp_packages is not None else McpPackageRegistry()
        self._mcp_clients: dict[str, StdioMcpClient] = {}
        self._mcp_tools: dict[str, dict[str, Tool]] = {}
        self._mcp_package_ids: dict[str, str] = {}

    def create_session(
        self,
        workspace_path: str,
        *,
        sandbox_mode: SandboxMode = "workspace-write",
        approval_mode: str = "interactive",
        feedback_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> AgentSession:
        """为指定工作区创建独立 Agent 会话。"""
        workspace = validate_workspace(workspace_path)
        sandbox = self._sandbox_factory(root=workspace.path, mode=sandbox_mode)
        tools = get_available_tools()
        tools.update(build_sandbox_tools(sandbox))
        for server_tools in self._mcp_tools.values():
            tools.update(server_tools)
        llm = self._llm_factory()
        planner = self._planner_factory(llm, tools)
        gate_mode = "interrupt" if approval_mode == "interactive" else approval_mode
        graph = build_delegation_graph(
            planner,
            tools,
            build_gate(gate_mode),
            clarifier=self._clarifier_factory(llm),
            checkpointer=InMemorySaver(),
            collect_feedback=feedback_sink is not None,
            feedback_sink=feedback_sink,
        )
        session_id = uuid4().hex
        session = AgentSession(session_id, workspace, sandbox, graph, llm, planner)
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> AgentSession:
        """返回已创建的会话。"""
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise ValueError(f"会话不存在: {session_id}") from exc

    def close_session(self, session_id: str) -> None:
        """关闭并移除会话。"""
        if self._sessions.pop(session_id, None) is None:
            raise ValueError(f"会话不存在: {session_id}")

    def connect_mcp_server(self, config: McpServerConfig) -> dict[str, Any]:
        """连接 MCP Server 并缓存其工具定义。"""
        return self._connect_mcp(config)

    def catalog_mcp_packages(self) -> list[dict[str, Any]]:
        """列出内置和插件提供的 MCP 功能包。"""
        return [package.manifest.to_dict() for package in self._mcp_packages.list()]

    def mcp_package_plugin_errors(self) -> dict[str, str]:
        """返回外部 MCP 功能包加载错误。"""
        return self._mcp_packages.plugin_errors()

    def connect_mcp_package(
        self,
        package_id: str,
        instance_name: str,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        """通过统一功能包接口连接 MCP Server。"""
        package = self._mcp_packages.get(package_id)
        server_config = package.build_server_config(instance_name, config)
        self._connect_mcp(server_config, package)
        self._mcp_package_ids[instance_name] = package_id
        return self._mcp_status(instance_name)

    def _connect_mcp(
        self,
        config: McpServerConfig,
        package: McpPackage | None = None,
    ) -> dict[str, Any]:
        """连接底层 Server 并应用可选功能包策略。"""
        if config.name in self._mcp_clients:
            raise ValueError(f"MCP Server 已连接: {config.name}")
        client = StdioMcpClient(config)
        try:
            client.connect()
            tools = build_mcp_tools(client, package)
        except Exception:
            client.close()
            raise
        self._mcp_clients[config.name] = client
        self._mcp_tools[config.name] = tools
        return self._mcp_status(config.name)

    def list_mcp_servers(self) -> list[dict[str, Any]]:
        """列出已注册 MCP Server 及其工具。"""
        return [self._mcp_status(name) for name in sorted(self._mcp_clients)]

    def list_connected_mcp_packages(self) -> list[dict[str, Any]]:
        """列出通过功能包接口建立的连接。"""
        return [self._mcp_status(name) for name in sorted(self._mcp_package_ids)]

    def disconnect_mcp_package(self, instance_name: str) -> None:
        """断开一个通过功能包接口建立的连接。"""
        if instance_name not in self._mcp_package_ids:
            raise ValueError(f"MCP 功能包实例未连接: {instance_name}")
        self.disconnect_mcp_server(instance_name)

    def disconnect_mcp_server(self, name: str) -> None:
        """断开并移除一个 MCP Server。"""
        client = self._mcp_clients.pop(name, None)
        self._mcp_tools.pop(name, None)
        self._mcp_package_ids.pop(name, None)
        if client is None:
            raise ValueError(f"MCP Server 未连接: {name}")
        client.close()

    def close(self) -> None:
        """关闭所有会话与 MCP Server。"""
        self._sessions.clear()
        clients = list(self._mcp_clients.values())
        self._mcp_clients.clear()
        self._mcp_tools.clear()
        self._mcp_package_ids.clear()
        for client in clients:
            client.close()

    def _mcp_status(self, name: str) -> dict[str, Any]:
        """构造带工具摘要的 MCP Server 状态。"""
        client = self._mcp_clients[name]
        tools = self._mcp_tools[name]
        return {
            **client.status(),
            "packageId": self._mcp_package_ids.get(name),
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "risk": tool.risk.value,
                    "inputSchema": tool.params_schema,
                }
                for tool in tools.values()
            ],
        }
