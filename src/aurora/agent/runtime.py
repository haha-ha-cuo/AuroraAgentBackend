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

    def start(self, goal: str) -> RunUpdate:
        """启动一个目标并返回完成或等待输入的快照。"""
        clean_goal = sanitize_text(goal).strip()
        if not clean_goal:
            raise ValueError("目标不能为空")
        run_id = uuid4().hex
        self._run_ids.add(run_id)
        state = self._graph.invoke({"goal": clean_goal}, self._config(run_id))
        return self._update(run_id, state)

    def resume(self, run_id: str, response: Any, interrupt_id: str | None = None) -> RunUpdate:
        """用前端响应恢复一个等待中的运行。"""
        if run_id not in self._run_ids:
            raise ValueError(f"运行不存在或不属于当前会话: {run_id}")
        clean_response = sanitize_value(response)
        resume_value = {interrupt_id: clean_response} if interrupt_id else clean_response
        state = self._graph.invoke(Command(resume=resume_value), self._config(run_id))
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
    ) -> None:
        self._llm_factory = llm_factory
        self._sandbox_factory = sandbox_factory
        self._planner_factory = planner_factory
        self._clarifier_factory = clarifier_factory
        self._sessions: dict[str, AgentSession] = {}

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
