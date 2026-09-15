"""执行固定的多 Agent 流程并持久化阶段、审查和交接证据。"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Mapping
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy import update as orm_update

from aurora.agent.store.model import ConversationSession, Project, ReviewArtifact

from ..conversation.session import SYSTEM_PROMPT, _message_text
from ..core import build_delegation_graph
from ..model_access.config import build_configured_llm
from ..preview import validate_preview
from ..safety import build_gate
from ..store.database import dumps, now, uid
from ..store.model import (
    AgentRun,
    Interaction,
    Review,
    ReviewFinding,
    Run,
    Task,
)
from ..store.records import decode
from .runtime import AgentSession, RuntimeInterrupt, RunUpdate


class Finding(BaseModel):
    """结构化视觉问题与截图位置。"""

    severity: Literal["blocking", "suggestion"]
    description: str
    artifact_id: str
    location: str
    suggestion: str
    bbox: list[float] | None = None
    previous_finding_id: str | None = None


class ReviewResult(BaseModel):
    """视觉审查的可验证结论。"""

    verdict: Literal["passed", "changes_requested", "unable_to_review"]
    summary: str
    findings: list[Finding] = Field(default_factory=list)


class WorkflowSession(AgentSession):
    """在同一会话中串行驱动编写、截图、审查与返修。"""

    def __init__(self, runtime, record, workspace, repository):
        self.runtime = runtime
        self.records = runtime.records
        self.record = record
        self.id = record["id"]
        self.workspace = workspace
        self.repository = repository
        self._run_ids = set()
        self._active = {}
        self.feedback_sink: Any = None
        self._lock = threading.Lock()
        self.sandbox = runtime._sandbox_factory(root=workspace.path, mode=record["sandbox_mode"])

    @property
    def llm(self):
        """构建默认流程编写角色的模型。"""
        return self._model(self._snapshot()["steps"][0]["config"])

    @property
    def planner(self):
        """提供 CLI 兼容规划器。"""
        return self.runtime._planner_factory(self.llm, self._tools({"allowed_tools": ["*"]}))

    def _snapshot(self, workflow_id=None):
        """读取会话当前配置并冻结工作流。"""
        self.record = self.records.get(ConversationSession, self.id)
        result = self.records.snapshot(
            workflow_id or self.record["workflow_id"],
            self.record["sandbox_mode"],
            self.record["approval_mode"],
        )
        from .registry import AgentRegistry

        result["agents"] = AgentRegistry(self.runtime).snapshot()
        result["preview"] = self.records.get(Project, self.record["project_id"])["preview"]
        return result

    def _model(self, config):
        """为阶段创建独立的模型客户端。"""
        if self.runtime._configured_llm_factory:
            return self.runtime._configured_llm_factory(config)
        if self.runtime._custom_llm:
            return self.runtime._llm_factory()
        return build_configured_llm(config)

    def _tools(self, agent):
        """按角色权限过滤当前运行时工具。"""
        from ..store import build_git_tools
        from ..tools import build_sandbox_tools, get_available_tools

        tools = get_available_tools()
        tools.update(build_sandbox_tools(self.sandbox))
        tools.update(build_git_tools(self.repository))
        for server_tools in self.runtime._mcp_tools.values():
            tools.update(server_tools)
        allowed = agent["allowed_tools"]
        return {key: value for key, value in tools.items() if "*" in allowed or key in allowed}

    def start(self, goal, on_progress=None, *, workflow_id=None, request_key=None, mode="run"):
        """原子登记一轮请求并开始执行，重复请求返回原记录。"""
        from aurora.text import sanitize_text

        goal = sanitize_text(goal).strip()
        if not goal or mode not in {"run", "say", "plan"}:
            raise ValueError("目标不能为空且运行模式必须有效")
        key = request_key or uid()
        existing = self.records.db.first(
            select(Run.id).where(Run.session_id == self.id, Run.request_key == key)
        )
        if existing:
            return self._result(existing["id"])
        if not self._lock.acquire(blocking=False):
            raise ValueError("会话正在处理请求")
        try:
            with self.records.db.transaction():
                existing = self.records.db.first(
                    select(Run.id).where(Run.session_id == self.id, Run.request_key == key)
                )
                if existing:
                    return self._result(existing["id"])
                self.records.require_idle(self.id)
                snapshot = self._snapshot(workflow_id)
                config = snapshot["steps"][0]["config"]
                history = self.records.context(
                    self.id, config["model"]["context_budget_tokens"], goal
                )
                run_id = uid()
                self.records.insert(
                    Run,
                    id=run_id,
                    session_id=self.id,
                    request_key=key,
                    goal=goal,
                    mode=mode,
                    status="running",
                    config_snapshot_json=dumps(snapshot),
                    created_at=now(),
                    updated_at=now(),
                )
                self.records.message(self.id, "user", goal, run_id=run_id)
                self.records.update(ConversationSession, self.id, updated_at=now())
            self._run_ids.add(run_id)
            state = {
                "snapshot": snapshot,
                "history": history,
                "goal": goal,
                "mode": mode,
                "iteration": 0,
                "phase": "code",
                "handoff": "",
                "tasks": [],
                "results": [],
            }
            self._active[run_id] = state
            return self._execute(run_id, state, on_progress, started=True)
        finally:
            self._lock.release()

    def resume(self, run_id, response, interrupt_id=None, on_progress=None):
        """恢复本进程持有的阶段或审批，不重放已完成阶段。"""
        if not self._lock.acquire(blocking=False):
            raise ValueError("会话正在处理请求")
        try:
            run = self.records.get(Run, run_id)
            if run["session_id"] != self.id:
                raise ValueError("运行不属于当前会话")
            if run["status"] != "waiting" or run_id not in self._active:
                raise ValueError("运行已结束或中断，不能恢复旧审批")
            state = self._active[run_id]
            pending = self.records.db.rows(
                select(Interaction).where(
                    Interaction.run_id == run_id, Interaction.status == "pending"
                )
            )
            if interrupt_id is None and len(pending) != 1:
                raise ValueError("必须指定 interruptId")
            selected = next(
                (
                    item
                    for item in pending
                    if interrupt_id is None or item["interrupt_id"] == interrupt_id
                ),
                None,
            )
            if selected is None:
                raise ValueError("审批已处理或不存在")
            if state.get("manual") == "preview":
                if isinstance(response, str):
                    response = json.loads(response)
                config = validate_preview(response, self.sandbox)
                state["snapshot"]["preview"] = config
                self.records.update(
                    Project,
                    self.record["project_id"],
                    preview_json=dumps(config),
                    updated_at=now(),
                )
                self.records.update(Run, run_id, config_snapshot_json=dumps(state["snapshot"]))
            elif state.get("manual") == "capture_approval":
                if not isinstance(response, Mapping) or response.get("approved") is not True:
                    state["cancelled"] = True
                else:
                    state["capture_approved"] = True
            elif state.get("manual") in {"review_limit", "review_unavailable"}:
                action = response.get("action") if isinstance(response, Mapping) else response
                if action not in {"accept", "cancel", "retry"}:
                    raise ValueError("请选择 accept、cancel 或 retry")
                if action == "accept":
                    state["phase"] = "summary"
                    state["accepted_with_issues"] = True
                elif action == "cancel":
                    state["cancelled"] = True
                elif state["manual"] == "review_limit":
                    state["iteration"] += 1
                    state["phase"] = "code"
                else:
                    state["review_attempt"] = state.get("review_attempt", 0) + 1
                    state["phase"] = "capture"
            else:
                graph_id = state.get("interrupt_ids", {}).get(
                    selected["interrupt_id"], selected["interrupt_id"]
                )
                state["resume"] = Command(resume={graph_id: response})
            with self.records.db.transaction():
                self.records.update(
                    Interaction,
                    selected["id"],
                    response_json=dumps(response),
                    status="resolved",
                    updated_at=now(),
                )
                if selected["kind"] == "clarification":
                    self.records.message(
                        self.id, "user", "补充信息：" + dumps(response), run_id=run_id
                    )
                self.records.update(Run, run_id, status="running", updated_at=now())
            state.pop("manual", None)
            return self._execute(run_id, state, on_progress)
        finally:
            self._lock.release()

    def _execute(self, run_id, state, progress, started=False):
        """隔离工作区写入并在异常时提交失败状态。"""
        workspace_lock = self.runtime.workspace_lock(self.workspace.path)
        if not workspace_lock.acquire(blocking=False):
            self._finish(run_id, state, "failed", "工作区正在被其他会话使用")
            raise ValueError("工作区正在被其他会话使用")
        try:
            if started:
                self.repository.begin_run(run_id)
            self._emit(
                run_id,
                state,
                "run.started" if started else "run.resumed",
                {"goal": state["goal"]},
                progress,
            )
            if state.get("cancelled"):
                return self._finish(run_id, state, "cancelled", "用户取消了协作流程")
            return self._drive(run_id, state, progress)
        except Exception as exc:
            message = self.runtime.redact(str(exc))
            self._finish(run_id, state, "failed", message)
            raise RuntimeError(message) from None
        finally:
            workspace_lock.release()

    def _emit(self, run_id, state, name, value, progress):
        """先持久化事件再通知协议层。"""
        payload = json.loads(self.runtime.redact(dumps(dict(value))))
        if state.get("agent_run_id"):
            payload |= {
                "agentRunId": state["agent_run_id"],
                "stepKey": state["phase"],
                "iteration": state["iteration"],
            }
        self.records.event(run_id, name, payload)
        if progress and (
            not name.startswith("stage.") or state["snapshot"]["workflow"]["kind"] != "single"
        ):
            try:
                progress(run_id, name, payload)
            except (ConnectionError, OSError):
                pass

    def _drive(self, run_id, state, progress):
        """驱动预设流程直到完成或等待用户。"""
        while True:
            phase = state["phase"]
            if phase == "code":
                if not self._code(run_id, state, progress):
                    return self._result(run_id)
                if state["mode"] != "run" or state["snapshot"]["workflow"]["kind"] == "single":
                    result = self._finish(run_id, state, "completed", state["report"])
                    self._emit(run_id, state, "summarize", {"report": state["report"]}, progress)
                    return result
                state["phase"] = "capture"
            elif phase == "capture":
                preview = state["snapshot"]["preview"]
                if not preview.get("url"):
                    return self._wait(
                        run_id,
                        state,
                        "preview",
                        "clarification",
                        {
                            "question": "请设置本项目的预览地址、启动方式和页面。",
                            "previewRequired": True,
                        },
                    )
                if not state.get("capture_approved"):
                    if state["snapshot"]["approval_mode"] == "never":
                        return self._unable(
                            run_id, state, "当前审批策略不允许启动浏览器预览", progress
                        )
                    if state["snapshot"]["approval_mode"] == "interactive":
                        return self._wait(
                            run_id,
                            state,
                            "capture_approval",
                            "approval",
                            {"tool": "browser_preview", "risk": "execute", "args": preview},
                        )
                    state["capture_approved"] = True
                self._emit(run_id, state, "stage.started", {"stepKey": "capture"}, progress)
                try:
                    if self.repository.capture().tree != state["code_tree"]:
                        raise ValueError("工作区已发生变化，请重新运行编写阶段后审查")
                    shots, log = self.runtime.capture.capture(preview, self.sandbox)
                    if self.repository.capture().tree != state["code_tree"]:
                        raise ValueError("截图期间工作区发生变化，证据已失效")
                    if not shots:
                        raise ValueError("截图为空")
                    artifacts = []
                    status = self.repository.status("run", run_id)
                    diff = "\n".join(
                        self.repository.diff(item["path"], "run", run_id)["content"]
                        for item in status["files"]
                    )
                    code_diff = self.records.artifact(
                        run_id,
                        state["code_agent_run_id"],
                        "code_diff",
                        diff.encode(),
                        "text/plain",
                        {},
                    )
                    self.records.artifact(
                        run_id,
                        state["code_agent_run_id"],
                        "preview_log",
                        self.runtime.redact(log).encode(),
                        "text/plain",
                        {},
                    )
                    for data, metadata in shots:
                        artifact = self.records.artifact(
                            run_id,
                            state["code_agent_run_id"],
                            "screenshot",
                            data,
                            "image/png",
                            metadata
                            | {
                                "code_agent_run_id": state["code_agent_run_id"],
                                "diff_artifact_id": code_diff["id"],
                                "captured_at": now(),
                            },
                        )
                        artifacts.append(artifact["id"])
                        self._emit(
                            run_id,
                            state,
                            "artifact.created",
                            {"artifactId": artifact["id"]},
                            progress,
                        )
                    state["artifacts"] = artifacts
                    state["phase"] = "review"
                except Exception as exc:
                    return self._unable(run_id, state, self.runtime.redact(str(exc)), progress)
            elif phase == "review":
                try:
                    review = self._review(run_id, state, progress)
                except Exception as exc:
                    return self._unable(run_id, state, self.runtime.redact(str(exc)), progress)
                state["handoff"] = dumps(review)
                if review["verdict"] == "passed":
                    state["phase"] = "summary"
                elif review["verdict"] == "unable_to_review":
                    return self._wait(
                        run_id,
                        state,
                        "review_unavailable",
                        "decision",
                        {"question": review["summary"], "actions": ["retry", "accept", "cancel"]},
                    )
                elif state["iteration"] >= state["snapshot"]["workflow"]["max_revisions"]:
                    return self._wait(
                        run_id,
                        state,
                        "review_limit",
                        "decision",
                        {
                            "question": "已达到自动返修上限，是否接受、取消或再返修一轮？",
                            "actions": ["accept", "cancel", "retry"],
                        },
                    )
                else:
                    state["iteration"] += 1
                    state["phase"] = "code"
            elif phase == "summary":
                config = self._step(state, "summary")["config"]
                inputs = self._input(
                    state,
                    config,
                    "根据编写结果和最新审查记录汇总实际完成情况。\n"
                    + state["report"]
                    + "\n"
                    + state["handoff"],
                )
                agent_run = self._begin_agent(run_id, state, config, inputs, progress)
                response = self._model(config).invoke(self._messages(config, inputs))
                report = _message_text(response)
                if state.get("accepted_with_issues"):
                    report = "用户接受了尚未通过视觉审查的结果。\n\n" + report
                self._complete_agent(
                    run_id,
                    state,
                    agent_run,
                    report,
                    progress,
                    getattr(response, "usage_metadata", None),
                )
                result = self._finish(run_id, state, "completed", report)
                self._emit(run_id, state, "summarize", {"report": report}, progress)
                return result

    def _step(self, state, key):
        """定位冻结流程中的阶段。"""
        return next(item for item in state["snapshot"]["steps"] if item["step_key"] == key)

    def _input(self, state, config, current):
        """构造隔离的角色上下文和明确交接材料。"""
        budget = config["model"]["context_budget_tokens"]
        if len(current.encode("utf-8")) > budget:
            raise ValueError("本轮需求和交接材料超过上下文预算")
        history = list(state["history"])
        while (
            history and len(dumps(history).encode("utf-8")) + len(current.encode("utf-8")) > budget
        ):
            history.pop(0)
            while history and history[0]["role"] != "user":
                history.pop(0)
        return {"history": history, "current": current, "artifact_ids": state.get("artifacts", [])}

    def _messages(self, config, inputs):
        """构造模型收到的角色指令和消息内容。"""
        return [
            SystemMessage(content=SYSTEM_PROMPT + "\n" + config["agent"]["instructions"]),
            HumanMessage(
                content="历史会话（仅供参考）：\n"
                + dumps(inputs["history"])
                + "\n\n本轮输入：\n"
                + inputs["current"]
            ),
        ]

    def _begin_agent(self, run_id, state, config, inputs, progress):
        """登记唯一阶段执行及其输入。"""
        key = state["phase"]
        if key == "review" and state.get("review_attempt"):
            key += f"_retry_{state['review_attempt']}"
        row = self.records.insert(
            AgentRun,
            id=uid(),
            run_id=run_id,
            step_key=key,
            agent_id=config["agent"]["id"],
            iteration=state["iteration"],
            status="running",
            input_json=dumps(inputs),
            config_snapshot_json=dumps(config),
            created_at=now(),
            updated_at=now(),
        )
        state["agent_run_id"] = row["id"]
        self._emit(run_id, state, "stage.started", {"agentId": config["agent"]["id"]}, progress)
        return row["id"]

    def _complete_agent(self, run_id, state, agent_run, output, progress, usage=None):
        """保存阶段结果和内部交接消息后发送完成事件。"""
        with self.records.db.transaction():
            self.records.update(
                AgentRun,
                agent_run,
                status="completed",
                output=output,
                usage_json=dumps(usage or {}),
                updated_at=now(),
            )
            self.records.message(
                self.id,
                "assistant",
                output,
                run_id=run_id,
                agent_run_id=agent_run,
                visibility="internal",
            )
        self._emit(run_id, state, "stage.completed", {"output": output}, progress)

    def _code(self, run_id, state, progress):
        """运行编写角色的工具图并持久化暂停点。"""
        config = self._step(state, "code")["config"]
        if "graph" not in state:
            current = state["goal"] + (
                "\n请根据审查返修：\n" + state["handoff"] if state["handoff"] else ""
            )
            inputs = self._input(state, config, current)
            agent_run = self._begin_agent(run_id, state, config, inputs, progress)
            state["code_agent_run_id"] = agent_run
            llm = self._model(config)
            tools = self._tools(config["agent"])
            from .registry import registry_tools

            configs = [
                c for c in state["snapshot"]["agents"] if c["agent"]["id"] != config["agent"]["id"]
            ]
            allowed = config["agent"]["allowed_tools"]
            tools.update(
                {
                    key: value
                    for key, value in registry_tools(configs).items()
                    if "*" in allowed or key in allowed
                }
            )
            planner = self.runtime._planner_factory(llm, tools)
            state["planner"] = planner
            if hasattr(planner, "set_instructions"):
                from .registry import catalog

                instructions = config["agent"]["instructions"]
                if "call_agent" in tools:
                    instructions += "\n本轮可调用的 Agent：\n" + dumps(catalog(configs))
                planner.set_instructions(instructions)
            goal = (
                "历史会话（仅供参考）：\n" + dumps(inputs["history"]) + "\n本轮目标：\n" + current
            )
            if state["mode"] == "say":
                response = llm.invoke(self._messages(config, inputs))
                state["report"] = _message_text(response)
                self._complete_agent(
                    run_id,
                    state,
                    agent_run,
                    state["report"],
                    progress,
                    getattr(response, "usage_metadata", None),
                )
                return True
            if state["mode"] == "plan":
                state["tasks"] = planner.plan(goal)
                state["report"] = dumps(state["tasks"])
                self._complete_agent(run_id, state, agent_run, state["report"], progress)
                return True
            state["graph"] = build_delegation_graph(
                planner,
                tools,
                build_gate(
                    "interrupt"
                    if state["snapshot"]["approval_mode"] == "interactive"
                    else state["snapshot"]["approval_mode"]
                ),
                clarifier=self.runtime._clarifier_factory(llm),
                checkpointer=InMemorySaver(),
                collect_feedback=self.feedback_sink is not None,
                feedback_sink=self.feedback_sink,
                serial="call_agent" in tools,
                delegate=lambda task: self._delegate(run_id, state, task, configs, progress),
            )
            state["graph_input"] = {"goal": goal}
            state["task_ids"] = {}
        graph = state["graph"]
        agent_run = state["code_agent_run_id"]
        state["agent_run_id"] = agent_run
        self.records.update(AgentRun, agent_run, status="running", updated_at=now())
        graph_config = {"configurable": {"thread_id": agent_run}}
        value = state.pop("resume", state["graph_input"])
        for chunk in graph.stream(value, graph_config, stream_mode="updates"):
            for node, update in chunk.items():
                if not isinstance(update, Mapping):
                    continue
                if node == "plan":
                    with self.records.db.transaction():
                        self.records.db.execute(delete(Task).where(Task.agent_run_id == agent_run))
                        mapped = []
                        for task in update["tasks"]:
                            task_id = uid()
                            state["task_ids"][task["id"]] = task_id
                            self.records.insert(
                                Task,
                                id=task_id,
                                agent_run_id=agent_run,
                                task_key=task["id"],
                                description=task["description"],
                                tool=task["tool"],
                                args_json=dumps(task["args"]),
                                effort=str(task["effort"]),
                                status="queued",
                                created_at=now(),
                                updated_at=now(),
                            )
                            mapped.append(dict(task, id=task_id))
                    update = dict(update, tasks=mapped)
                    state["tasks"] = mapped
                if node == "dispatch":
                    with self.records.db.transaction() as connection:
                        connection.execute(
                            orm_update(Task)
                            .where(Task.agent_run_id == agent_run, Task.status == "queued")
                            .values(status="running", updated_at=now())
                        )
                if node == "execute":
                    results = []
                    for result in update.get("results", []):
                        task_id = state["task_ids"][result["task_id"]]
                        output = self.runtime.redact(result["output"])
                        self.records.update(
                            Task,
                            task_id,
                            status="completed" if result["ok"] else "failed",
                            output=output,
                            error="" if result["ok"] else output,
                            updated_at=now(),
                        )
                        results.append(dict(result, task_id=task_id, output=output))
                    state["results"].extend(results)
                    update = dict(update, results=results)
                if node != "summarize":
                    self._emit(run_id, state, node, update, progress)
        snapshot = graph.get_state(graph_config)
        interruptions = [item for task in snapshot.tasks for item in task.interrupts]
        if interruptions:
            with self.records.db.transaction():
                self.records.db.execute(
                    orm_update(Task)
                    .where(Task.agent_run_id == agent_run, Task.status.in_(("queued", "running")))
                    .values(status="waiting", updated_at=now())
                )
                for item in interruptions:
                    public_id = item.value.get("delegationInterruptId", item.id)
                    state.setdefault("interrupt_ids", {})[public_id] = item.id
                    if not self.records.db.first(
                        select(Interaction.id).where(
                            Interaction.run_id == run_id, Interaction.interrupt_id == public_id
                        )
                    ):
                        self.records.insert(
                            Interaction,
                            id=uid(),
                            run_id=run_id,
                            agent_run_id=item.value.get("agentRunId", agent_run),
                            interrupt_id=public_id,
                            kind=item.value.get("kind", "approval"),
                            request_json=dumps(item.value),
                            created_at=now(),
                            updated_at=now(),
                        )
                self.records.update(AgentRun, agent_run, status="waiting", updated_at=now())
                self.records.update(
                    Run,
                    run_id,
                    status="waiting",
                    updated_at=now(),
                    state_json=dumps(self._public_state(state)),
                )
            return False
        if any(not result["ok"] for result in snapshot.values.get("results", [])):
            raise ValueError("编写阶段存在失败的工具任务，请检查执行记录")
        state["report"] = self.runtime.redact(snapshot.values.get("report", ""))
        state.pop("graph")
        self.repository.finish_run(run_id, "running")
        state["code_tree"] = self.repository.capture().tree
        self._complete_agent(
            run_id,
            state,
            agent_run,
            state["report"],
            progress,
            getattr(state.get("planner"), "usage", {}),
        )
        return True

    def _delegate(self, run_id, state, task, configs, progress):
        """在父运行内恢复独立子执行，不重建顶层会话。"""
        from .delegation import execute_delegate

        return execute_delegate(self, run_id, state, task, configs, progress)

    def _review(self, run_id, state, progress):
        """向视觉模型传递真实图片并验证审查证据。"""
        config = self._step(state, "review")["config"]
        if "image" not in config["model"]["input_types"]:
            raise ValueError("审查模型未声明支持图片输入")
        inputs = self._input(
            state,
            config,
            state["goal"]
            + "\n编写结果：\n"
            + state["report"]
            + "\n上一轮审查：\n"
            + state["handoff"],
        )
        agent_run = self._begin_agent(run_id, state, config, inputs, progress)
        state["review_agent_run_id"] = agent_run
        content: list[str | dict[str, Any]] = [
            {
                "type": "text",
                "text": "历史会话（仅供参考）：\n"
                + dumps(inputs["history"])
                + "\n"
                + inputs["current"]
                + "\n检查比例、布局、溢出和响应式问题。"
                "仅有建议时 passed；存在阻断问题时 changes_requested；证据不足时 unable_to_review。"
                "问题必须引用所附截图 ID。",
            }
        ]
        for artifact_id in state["artifacts"]:
            artifact, data = self.records.artifact_bytes(artifact_id)
            content.append(
                {
                    "type": "text",
                    "text": "截图 ID：" + artifact_id + "\n" + dumps(artifact["metadata"]),
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(data).decode()
                    },
                }
            )
        model = self._model(config).with_structured_output(
            ReviewResult, method="json_mode", include_raw=True
        )
        response = model.invoke(
            [
                SystemMessage(
                    content=config["agent"]["instructions"]
                    + "\n你只审查证据，不能执行代码或修改文件。"
                    + "只输出符合以下 JSON Schema 的 JSON 对象：\n"
                    + dumps(ReviewResult.model_json_schema())
                ),
                HumanMessage(content=content),
            ]
        )
        usage = {}
        if isinstance(response, dict) and "parsed" in response:
            if response.get("parsing_error") or response["parsed"] is None:
                raise ValueError("视觉审查返回了不符合结构定义的结果")
            usage = getattr(response.get("raw"), "usage_metadata", None) or {}
            response = response["parsed"]
        result = (
            response
            if isinstance(response, ReviewResult)
            else ReviewResult.model_validate(response)
        )
        previous = {
            row["id"]
            for row in self.records.db.rows(
                select(ReviewFinding.id)
                .join(ReviewFinding.review)
                .join(Review.agent_run)
                .where(AgentRun.run_id == run_id)
            )
        }
        for finding in result.findings:
            if finding.artifact_id not in state["artifacts"]:
                raise ValueError("审查引用了非本轮截图")
            if finding.previous_finding_id and finding.previous_finding_id not in previous:
                raise ValueError("审查引用了未知的历史问题")
            if finding.bbox is not None and (
                len(finding.bbox) != 4 or any(not 0 <= v <= 1 for v in finding.bbox)
            ):
                raise ValueError("问题边界框必须是四个 0 到 1 的数值")
        if result.verdict == "changes_requested" and not any(
            finding.severity == "blocking" for finding in result.findings
        ):
            raise ValueError("审查要求返修但没有提供阻断问题及证据")
        if result.verdict != "unable_to_review":
            result.verdict = (
                "changes_requested"
                if any(finding.severity == "blocking" for finding in result.findings)
                else "passed"
            )
        with self.records.db.transaction():
            review = self.records.insert(
                Review,
                id=uid(),
                agent_run_id=agent_run,
                code_agent_run_id=state["code_agent_run_id"],
                iteration=state["iteration"],
                verdict=result.verdict,
                summary=result.summary,
                created_at=now(),
            )
            for artifact_id in state["artifacts"]:
                self.records.insert(ReviewArtifact, review_id=review["id"], artifact_id=artifact_id)
            findings = []
            for finding in result.findings:
                values = finding.model_dump(exclude={"bbox"})
                item = self.records.insert(
                    ReviewFinding,
                    id=uid(),
                    review_id=review["id"],
                    bbox_json=dumps(finding.bbox),
                    **values,
                )
                findings.append(item)
            output = self.runtime.redact(result.model_dump_json())
            self.records.update(
                AgentRun,
                agent_run,
                status="completed",
                output=output,
                usage_json=dumps(usage),
                updated_at=now(),
            )
            self.records.message(
                self.id,
                "assistant",
                output,
                run_id=run_id,
                agent_run_id=agent_run,
                visibility="internal",
            )
        self._emit(run_id, state, "stage.completed", {"output": output}, progress)
        self._emit(
            run_id,
            state,
            "review.completed",
            {"reviewId": review["id"], "verdict": result.verdict},
            progress,
        )
        state.pop("review_agent_run_id", None)
        return review | {"findings": findings}

    def _unable(self, run_id, state, reason, progress):
        """记录无法审查的证据缺口并等待用户处理。"""
        agent_run = state.pop("review_agent_run_id", None)
        if agent_run is None:
            previous_phase = state["phase"]
            state["phase"] = "review"
            agent_run = self._begin_agent(
                run_id,
                state,
                self._step(state, "review")["config"],
                {"error": reason, "artifact_ids": []},
                progress,
            )
            state["phase"] = previous_phase
        with self.records.db.transaction():
            self.records.update(
                AgentRun, agent_run, status="failed", error=reason, updated_at=now()
            )
            review = self.records.insert(
                Review,
                id=uid(),
                agent_run_id=agent_run,
                code_agent_run_id=state["code_agent_run_id"],
                iteration=state["iteration"],
                verdict="unable_to_review",
                summary=reason,
                created_at=now(),
            )
        state["handoff"] = dumps(review)
        self._emit(
            run_id,
            state,
            "review.completed",
            {"reviewId": review["id"], "verdict": "unable_to_review"},
            progress,
        )
        return self._wait(
            run_id,
            state,
            "review_unavailable",
            "decision",
            {"question": reason, "actions": ["retry", "accept", "cancel"]},
        )

    def _wait(self, run_id, state, manual, kind, request):
        """登记流程级人工输入点。"""
        state["manual"] = manual
        with self.records.db.transaction():
            self.records.insert(
                Interaction,
                id=uid(),
                run_id=run_id,
                interrupt_id=uid(),
                kind=kind,
                request_json=dumps({"kind": kind} | request),
                created_at=now(),
                updated_at=now(),
            )
            self.records.update(
                Run,
                run_id,
                status="waiting",
                updated_at=now(),
                state_json=dumps(self._public_state(state)),
            )
        return self._result(run_id)

    def _public_state(self, state):
        """生成协议与重载共用的公开快照。"""
        return {
            "goal": state["goal"],
            "report": state.get("report", ""),
            "tasks": state["tasks"],
            "results": state["results"],
            "phase": state["phase"],
            "iteration": state["iteration"],
        }

    def _finish(self, run_id, state, status, report):
        """原子结束运行并提交最终公开消息。"""
        state["report"] = report
        with self.records.db.transaction() as connection:
            self.records.update(
                Run,
                run_id,
                status=status,
                ended_at=now(),
                updated_at=now(),
                error=report if status == "failed" else "",
                state_json=dumps(self._public_state(state)),
            )
            self.records.update(ConversationSession, self.id, updated_at=now())
            self.records.event(run_id, "run." + status, self._public_state(state))
            self.records.message(
                self.id,
                "assistant",
                report,
                run_id=run_id,
                agent_run_id=state.get("agent_run_id"),
                kind="report" if status == "completed" else "error",
                status="completed" if status == "completed" else "failed",
            )
            connection.execute(
                orm_update(Interaction)
                .where(Interaction.run_id == run_id, Interaction.status == "pending")
                .values(status="expired", updated_at=now())
            )
            connection.execute(
                orm_update(Task)
                .where(
                    Task.agent_run_id.in_(select(AgentRun.id).where(AgentRun.run_id == run_id)),
                    Task.status.in_(("queued", "running", "waiting")),
                )
                .values(status=status, updated_at=now())
            )
            connection.execute(
                orm_update(AgentRun)
                .where(AgentRun.run_id == run_id, AgentRun.status.in_(("running", "waiting")))
                .values(status=status, error=report if status == "failed" else "", updated_at=now())
            )
        if run_id in self._run_ids:
            try:
                self.repository.finish_run(run_id, status)
            except ValueError:
                pass
        self._active.pop(run_id, None)
        return self._result(run_id)

    def _result(self, run_id):
        """读取已提交的运行状态及有效交互。"""
        run = self.records.get(Run, run_id)
        pending = [
            decode(row)
            for row in self.records.db.rows(
                select(Interaction).where(
                    Interaction.run_id == run_id, Interaction.status == "pending"
                )
            )
        ]
        return RunUpdate(
            self.id,
            run_id,
            run["status"],
            run["state"] | {"goal": run["goal"]},
            tuple(RuntimeInterrupt(row["interrupt_id"], row["request"]) for row in pending),
        )

    def close(self):
        """中断活动运行并释放临时 Git 资源。"""
        if not self._lock.acquire(blocking=False):
            raise ValueError("会话正在执行，请等待当前阶段结束后关闭")
        try:
            for run_id, state in list(self._active.items()):
                self._finish(run_id, state, "interrupted", "会话已关闭")
            self.repository.close()
        finally:
            self._lock.release()
