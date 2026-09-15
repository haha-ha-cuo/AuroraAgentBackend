"""面向 CLI 与桌面前端的会话级 Agent 运行时。"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langgraph.types import Command

from aurora.agent.store.model import ConversationSession, Project
from aurora.text import sanitize_text, sanitize_value

from ..core import LLMClarifier, LLMPlanner
from ..core.state import DelegationState
from ..mcp import (
    McpPackage,
    McpPackageRegistry,
    McpServerConfig,
    StdioMcpClient,
    build_mcp_tools,
)
from ..model_access import build_llm
from ..sandbox import Sandbox, SandboxMode, create_sandbox
from ..store import GitRepository, GitView
from ..tools import Tool

if TYPE_CHECKING:
    from .workflows import WorkflowSession


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
        is_git_repository=GitRepository.is_repository(candidate),
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
        repository: GitRepository,
    ) -> None:
        self.id = session_id
        self.workspace = workspace
        self.sandbox = sandbox
        self.llm = llm
        self.planner = planner
        self.repository = repository
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
        self.repository.begin_run(run_id)
        try:
            if on_progress is None:
                state = self._graph.invoke({"goal": clean_goal}, self._config(run_id))
                update = self._update(run_id, state)
            else:
                on_progress(run_id, "run.started", {"goal": clean_goal})
                update = self._stream(run_id, {"goal": clean_goal}, on_progress)
        except Exception:
            self.repository.finish_run(run_id, "failed")
            raise
        self.repository.finish_run(run_id, update.status)
        return update

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
        try:
            if on_progress is None:
                state = self._graph.invoke(command, self._config(run_id))
                update = self._update(run_id, state)
            else:
                on_progress(run_id, "run.resumed", {})
                update = self._stream(run_id, command, on_progress)
        except Exception:
            self.repository.finish_run(run_id, "failed")
            raise
        self.repository.finish_run(run_id, update.status)
        return update

    def git_status(self, view: GitView, run_id: str | None = None) -> dict[str, Any]:
        """返回会话工作区的 Git 状态。"""
        return self.repository.status(view, run_id)

    def git_diff(self, path: str, view: GitView, run_id: str | None = None) -> dict[str, Any]:
        """返回会话工作区的单文件差异。"""
        return self.repository.diff(path, view, run_id)

    def git_rollback(self, run_id: str) -> dict[str, Any]:
        """回滚最近一轮 Agent 改动。"""
        if run_id not in self._run_ids:
            raise ValueError(f"运行不存在或不属于当前会话: {run_id}")
        return self.repository.rollback(run_id)

    def close(self) -> None:
        """释放会话临时资源。"""
        self.repository.close()

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
            RuntimeInterrupt(item.id, item.value) for item in state.get("__interrupt__", [])
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
        database=None,
        configured_llm_factory=None,
        capture=None,
    ) -> None:
        from ..preview import BrowserCapture
        from ..store.database import Database
        from ..store.records import Records

        self._custom_llm = llm_factory is not build_llm
        self._configured_llm_factory = configured_llm_factory
        self.records = Records(database or Database(":memory:" if self._custom_llm else None))
        try:
            self.records.acquire_runtime()
            self.records.bootstrap()
            self.records.recover()
        except BaseException:
            self.records.close()
            raise
        self.capture = capture or BrowserCapture()
        self._workspace_locks = {}
        self._workspace_lock_guard = threading.Lock()
        self._llm_factory = llm_factory
        self._sandbox_factory = sandbox_factory
        self._planner_factory = planner_factory
        self._clarifier_factory = clarifier_factory
        self._sessions: dict[str, WorkflowSession] = {}
        self._session_lock = threading.RLock()
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
        feedback_sink: Callable[[DelegationState], None] | None = None,
        workflow_id: str | None = None,
        title: str = "新对话",
    ) -> WorkflowSession:
        """为指定工作区创建独立 Agent 会话。"""
        from ..store.database import now, uid

        if approval_mode not in {"interactive", "always", "never"}:
            raise ValueError("未知审批策略")
        if sandbox_mode not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("未知沙箱模式")
        self.records.snapshot(
            workflow_id or self.records.setting("default_workflow_id"), sandbox_mode, approval_mode
        )
        workspace = validate_workspace(workspace_path)
        project = self.records.project(workspace.path)
        record = self.records.insert(
            ConversationSession,
            id=uid(),
            project_id=project["id"],
            workflow_id=workflow_id or self.records.setting("default_workflow_id"),
            title=title,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            created_at=now(),
            updated_at=now(),
        )
        session = self.get_session(record["id"])
        session.feedback_sink = feedback_sink
        return session

    def get_session(self, session_id: str) -> WorkflowSession:
        """按需重建持久会话的本地资源。"""
        with self._session_lock:
            if session_id not in self._sessions:
                from .workflows import WorkflowSession

                record = self.records.get(ConversationSession, session_id)
                project = self.records.get(Project, record["project_id"])
                workspace = validate_workspace(project["path"])
                repository = GitRepository.ensure(workspace.path)
                self._sessions[session_id] = WorkflowSession(self, record, workspace, repository)
            return self._sessions[session_id]

    def close_session(self, session_id: str) -> None:
        """释放会话资源并保留历史。"""
        with self._session_lock:
            self.records.get(ConversationSession, session_id)
            session = self._sessions.get(session_id)
            if session:
                session.close()
                self._sessions.pop(session_id, None)

    def workspace_lock(self, path):
        """同一工作区的阶段执行使用共享锁。"""
        with self._workspace_lock_guard:
            return self._workspace_locks.setdefault(path, threading.Lock())

    def redact(self, value):
        """从持久化错误和工具日志中移除配置凭据。"""
        for provider in self.records.list_configs("provider"):
            ref = provider["credential_ref"]
            if ref.startswith("env:"):
                secret = os.getenv(ref[4:])
            else:
                import keyring

                try:
                    secret = keyring.get_password("Aurora", ref[8:])
                except Exception:
                    secret = None
            if secret:
                value = value.replace(secret, "[凭据已隐藏]")
        return value

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
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            session.close()
        clients = list(self._mcp_clients.values())
        self._mcp_clients.clear()
        self._mcp_tools.clear()
        self._mcp_package_ids.clear()
        for client in clients:
            client.close()
        self.records.close()

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
