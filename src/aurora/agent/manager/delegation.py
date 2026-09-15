"""在父运行内串行执行带独立检查点的子 Agent。"""

from __future__ import annotations

import base64
from contextvars import Context

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, interrupt
from sqlalchemy import delete

from ..conversation.session import _message_text
from ..core import build_delegation_graph
from ..safety import build_gate
from ..store.database import dumps, now, uid
from ..store.model import AgentRun, Artifact, Task
from ..tools.base import Tool


def execute_delegate(session, run_id, state, task, configs, progress):
    """桥接子图中断，并缓存已经完成的执行结果。"""
    records = session.records
    key = state["code_agent_run_id"] + ":" + task["id"]
    children = state.setdefault("children", {})
    args = task["args"]
    if set(args) - {"agent_id", "task", "artifact_ids"}:
        raise ValueError("未知 Agent 调用参数")
    config = next((c for c in configs if c["agent"]["id"] == args.get("agent_id")), None)
    if config is None:
        raise ValueError("Agent 未开放调用或不属于本轮可调用清单")
    goal = args.get("task")
    artifacts = args.get("artifact_ids") or []
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("子任务说明不能为空")
    if len(goal.encode("utf-8")) > config["model"]["context_budget_tokens"]:
        raise ValueError("子任务说明超过模型上下文预算")
    if not isinstance(artifacts, list) or any(not isinstance(item, str) for item in artifacts):
        raise ValueError("图片产物 ID 必须是字符串列表")
    if artifacts and config["model"]["model_type"] != "multimodal":
        raise ValueError("文本模型不支持图片输入")
    if key not in children:
        content: list[str | dict] = [{"type": "text", "text": goal}]
        for artifact_id in artifacts:
            artifact = records.get(Artifact, artifact_id)
            if artifact["run_id"] != run_id or artifact["media_type"] not in {
                "image/png",
                "image/jpeg",
                "image/webp",
            }:
                raise ValueError("图片必须是当前运行可访问的图片产物")
            _, data = records.artifact_bytes(artifact_id)
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{artifact['media_type']};base64,"
                        + base64.b64encode(data).decode()
                    },
                }
            )
        child_id = uid()
        records.insert(
            AgentRun,
            id=child_id,
            run_id=run_id,
            agent_id=config["agent"]["id"],
            parent_task_id=state["task_ids"][task["id"]],
            step_key="delegate_" + child_id,
            iteration=state["iteration"],
            status="running",
            input_json=dumps(args),
            config_snapshot_json=dumps(config),
            created_at=now(),
            updated_at=now(),
        )
        child = {"id": child_id, "tasks": {}, "next": {"goal": goal}}
        children[key] = child
        try:
            model = session._model(config)
            tools = {
                name: tool
                for name, tool in session._tools(config["agent"]).items()
                if name not in {"list_agents", "call_agent"}
            }

            def respond(prompt: str) -> str:
                """使用角色模型回答需要推理或图片理解的子任务。"""
                response = model.invoke(
                    [
                        SystemMessage(content=config["agent"]["instructions"]),
                        HumanMessage(content=content),
                        HumanMessage(content=prompt),
                    ]
                )
                return _message_text(response)

            tools["agent_respond"] = Tool(
                "agent_respond", "分析、生成文本或理解所附图片并返回答案", respond
            )
            planner = session.runtime._planner_factory(model, tools)
            if hasattr(planner, "set_instructions"):
                planner.set_instructions(config["agent"]["instructions"])
            if artifacts and hasattr(planner, "set_images"):
                planner.set_images(content[1:])
            child["planner"] = planner
            mode = state["snapshot"]["approval_mode"]
            child["graph"] = build_delegation_graph(
                planner,
                tools,
                build_gate("interrupt" if mode == "interactive" else mode),
                clarifier=session.runtime._clarifier_factory(model),
                checkpointer=InMemorySaver(),
                serial=True,
            )
        except Exception as exc:
            _failed(session, child, exc)
            raise
    child = children[key]
    if child.get("error"):
        raise ValueError(child["error"])
    if "result" in child:
        return child["result"]
    graph_config = {"configurable": {"thread_id": child["id"]}, "recursion_limit": 100}

    def drive():
        """在隔离上下文中推进子图并持久化叶子任务。"""
        records.update(AgentRun, child["id"], status="running", updated_at=now())
        value = child.pop("next", None)
        for chunk in child["graph"].stream(value, graph_config, stream_mode="updates"):
            for node, update in chunk.items():
                if not isinstance(update, dict):
                    continue
                if node == "plan":
                    records.db.execute(delete(Task).where(Task.agent_run_id == child["id"]))
                    child["tasks"] = {}
                    for item in update["tasks"]:
                        task_id = uid()
                        child["tasks"][item["id"]] = task_id
                        records.insert(
                            Task,
                            id=task_id,
                            agent_run_id=child["id"],
                            task_key=item["id"],
                            description=item["description"],
                            tool=item["tool"],
                            args_json=dumps(item["args"]),
                            effort=str(item["effort"]),
                            status="queued",
                            created_at=now(),
                            updated_at=now(),
                        )
                if node == "execute":
                    for result in update.get("results", []):
                        output = session.runtime.redact(result["output"])
                        records.update(
                            Task,
                            child["tasks"][result["task_id"]],
                            status="completed" if result["ok"] else "failed",
                            output=output,
                            error="" if result["ok"] else output,
                            updated_at=now(),
                        )
        return child["graph"].get_state(graph_config)

    try:
        for request in child.get("resolved", []):
            interrupt(request)
        while True:
            if child.get("pending"):
                pending = child["pending"]
                request = dict(pending.value, agentRunId=child["id"], agentId=config["agent"]["id"])
                request["delegationInterruptId"] = (
                    child["id"] + ":" + str(len(child.get("resolved", [])))
                )
                response = interrupt(request)
                child.setdefault("resolved", []).append(request)
                child["next"] = Command(resume={pending.id: response})
                child.pop("pending")
            snapshot = Context().run(drive)
            pending = [item for node in snapshot.tasks for item in node.interrupts]
            if pending:
                child["pending"] = pending[0]
                records.update(AgentRun, child["id"], status="waiting", updated_at=now())
                continue
            if any(not result["ok"] for result in snapshot.values.get("results", [])):
                raise ValueError(snapshot.values.get("report", "子 Agent 执行失败"))
            output = session.runtime.redact(snapshot.values.get("report", ""))
            records.update(
                AgentRun,
                child["id"],
                status="completed",
                output=output,
                usage_json=dumps(getattr(child["planner"], "usage", {})),
                updated_at=now(),
            )
            records.message(
                session.id,
                "assistant",
                output,
                run_id=run_id,
                agent_run_id=child["id"],
                visibility="internal",
            )
            child["result"] = dumps(
                {"agentRunId": child["id"], "status": "completed", "output": output}
            )
            return child["result"]
    except GraphInterrupt:
        raise
    except Exception as exc:
        _failed(session, child, exc)
        raise ValueError(
            dumps({"agentRunId": child["id"], "status": "failed", "error": child["error"]})
        ) from None


def _failed(session, child, exc):
    """保存脱敏后的子执行失败信息。"""
    child["error"] = session.runtime.redact(str(exc))
    session.records.update(
        AgentRun, child["id"], status="failed", error=child["error"], updated_at=now()
    )
