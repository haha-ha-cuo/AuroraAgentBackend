"""委派图构建：plan → dispatch → execute → summarize。"""

from __future__ import annotations

from typing import Mapping

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ...logging import get_logger
from ..safety.gate import ConfirmationGate, DenyApprover
from ..tools.base import Tool
from .planner import Planner
from .state import DelegationState, Result

log = get_logger(__name__)


def build_delegation_graph(
    planner: Planner,
    tools: Mapping[str, Tool],
    gate: ConfirmationGate | None = None,
):
    """构建并编译委派图。

    Args:
        planner: 规划器（MockPlanner 或未来的 LLMPlanner）。
        tools: 工具注册表 ``{name: Tool}``。
        gate: 工具执行前的确认门，缺省只放行只读工具。

    Returns:
        编译后的 LangGraph 图。
    """
    gate = gate or ConfirmationGate(DenyApprover())

    def plan_node(state: DelegationState) -> dict:
        tasks = planner.plan(state["goal"])
        log.info("规划完成，共 %d 个子任务", len(tasks))
        for task in tasks:
            log.info(
                "  [%s] %s → %s",
                task["effort"].value,
                task["description"],
                task["tool"],
            )
        return {"tasks": tasks}

    def dispatch(state: DelegationState) -> list[Send]:
        """条件边：按 tasks 动态 fan-out 到 execute（并行）。"""
        return [Send("execute", {"current_task": task}) for task in state["tasks"]]

    def execute_node(state: DelegationState) -> dict:
        task = state["current_task"]
        tool = tools[task["tool"]]
        try:
            output = gate.invoke(tool, task["args"])
            ok = True
            log.info("[%s] ✓ %s", task["effort"].value, task["description"])
        except Exception as exc:  # noqa: BLE001
            output = f"执行失败: {exc}"
            ok = False
            log.warning("[%s] ✗ %s：%s", task["effort"].value, task["description"], exc)

        result: Result = {"task_id": task["id"], "ok": ok, "output": output}
        return {"results": [result]}

    def summarize_node(state: DelegationState) -> dict:
        # 并行完成顺序不保证与 tasks 一致，按 task_id 匹配
        by_id = {r["task_id"]: r for r in state["results"]}
        parts: list[str] = []
        for i, task in enumerate(state["tasks"], 1):
            result = by_id[task["id"]]
            mark = "✓" if result["ok"] else "✗"
            parts.append(
                f"{i}) [{task['effort'].value}] {task['description']} {mark}\n"
                f"   {result['output']}"
            )
        return {"report": "\n\n".join(parts)}

    builder = StateGraph(DelegationState)
    builder.add_node("plan", plan_node)
    builder.add_node("execute", execute_node)
    builder.add_node("summarize", summarize_node)

    builder.add_edge(START, "plan")
    builder.add_conditional_edges("plan", dispatch)
    builder.add_edge("execute", "summarize")
    builder.add_edge("summarize", END)

    return builder.compile()
