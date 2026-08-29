"""支持澄清循环、断点和轨迹的委派图。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, cast

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from ...logging import get_logger
from ..safety.gate import ConfirmationGate, DenyApprover
from ..tools.base import Tool
from .planner import Clarifier, NoClarifier, Planner
from .state import DelegationState, Evaluation, Result, TraceEvent

log = get_logger(__name__)


def build_delegation_graph(
    planner: Planner,
    tools: Mapping[str, Tool],
    gate: ConfirmationGate | None = None,
    *,
    clarifier: Clarifier | None = None,
    max_clarification_rounds: int = 3,
    checkpointer: Any = None,
    interrupt_before: Sequence[str] | None = None,
    interrupt_after: Sequence[str] | None = None,
    collect_feedback: bool = False,
    feedback_sink: Callable[[DelegationState], None] | None = None,
):
    """构建并编译支持人工介入的委派图。"""
    if max_clarification_rounds < 0:
        raise ValueError("max_clarification_rounds 不能小于 0")
    gate = gate or ConfirmationGate(DenyApprover())
    clarifier = clarifier or NoClarifier()

    def planning_goal(state: DelegationState) -> str:
        """把已有澄清合并为下一轮规划输入。"""
        clarifications = state.get("clarifications", [])
        if not clarifications:
            return state["goal"]
        context = "\n".join(
            f"问题：{item['question']}\n回答：{item['answer']}" for item in clarifications
        )
        return f"{state['goal']}\n\n用户补充信息：\n{context}"

    def plan_node(state: DelegationState) -> dict:
        """根据目标和已有澄清生成任务。"""
        tasks = planner.plan(planning_goal(state))
        log.info("规划完成，共 %d 个子任务", len(tasks))
        for task in tasks:
            log.info(
                "  [%s] %s → %s",
                task["effort"].value,
                task["description"],
                task["tool"],
            )
        return {
            "tasks": tasks,
            "trace": [{"node": "plan", "detail": f"生成 {len(tasks)} 个任务"}],
        }

    def clarify_node(state: DelegationState) -> dict:
        """判断是否需要暂停并询问用户。"""
        count = state.get("clarification_count", 0)
        if count >= max_clarification_rounds:
            return {
                "clarification_needed": False,
                "trace": [{"node": "clarify", "detail": "达到澄清轮数上限"}],
            }
        decision = clarifier.assess(
            state["goal"],
            state["tasks"],
            list(state.get("clarifications", [])),
        )
        needed = decision.needed and bool(decision.question)
        detail = decision.reason or ("需要用户澄清" if needed else "计划信息充分")
        return {
            "clarification_needed": needed,
            "clarification_question": decision.question,
            "trace": [{"node": "clarify", "detail": detail}],
        }

    def route_after_clarify(state: DelegationState) -> Literal["ask_user", "dispatch"]:
        """按澄清判断选择人工门或任务派发。"""
        return "ask_user" if state.get("clarification_needed", False) else "dispatch"

    def ask_user_node(state: DelegationState) -> dict:
        """暂停图执行并接收用户补充信息。"""
        question = state.get("clarification_question", "")
        if not question:
            raise ValueError("澄清问题不能为空")
        answer = interrupt(
            {
                "kind": "clarification",
                "question": question,
                "round": state.get("clarification_count", 0) + 1,
            }
        )
        history = [
            *state.get("clarifications", []),
            {"question": question, "answer": str(answer)},
        ]
        return {
            "clarifications": history,
            "clarification_count": state.get("clarification_count", 0) + 1,
            "trace": [{"node": "ask_user", "detail": question}],
        }

    def dispatch(state: DelegationState) -> list[Send] | str:
        """按任务动态并行派发。"""
        if not state["tasks"]:
            return "summarize"
        return [Send("execute", {"current_task": task}) for task in state["tasks"]]

    def dispatch_node(state: DelegationState) -> dict:
        """进入动态派发阶段。"""
        return {}

    def execute_node(state: DelegationState) -> dict:
        """通过确认门执行一个叶子任务。"""
        task = state.get("current_task")
        if task is None:
            raise ValueError("当前任务不能为空")
        try:
            tool = tools[task["tool"]]
            output = gate.invoke(tool, task["args"])
            ok = True
            log.info("[%s] ✓ %s", task["effort"].value, task["description"])
        except GraphInterrupt:
            raise
        except Exception as exc:
            output = f"执行失败: {exc}"
            ok = False
            log.warning("[%s] ✗ %s：%s", task["effort"].value, task["description"], exc)

        result: Result = {"task_id": task["id"], "ok": ok, "output": output}
        return {
            "results": [result],
            "trace": [{"node": "execute", "detail": f"{task['id']}:{'ok' if ok else 'failed'}"}],
        }

    def summarize_node(state: DelegationState) -> dict:
        """按原任务顺序汇总并行结果。"""
        by_id = {result["task_id"]: result for result in state.get("results", [])}
        parts: list[str] = []
        for index, task in enumerate(state["tasks"], 1):
            result = by_id.get(task["id"])
            if result is None:
                parts.append(
                    f"{index}) [{task['effort'].value}] {task['description']} ✗\n   未产生结果"
                )
                continue
            mark = "✓" if result["ok"] else "✗"
            parts.append(
                f"{index}) [{task['effort'].value}] {task['description']} {mark}\n"
                f"   {result['output']}"
            )
        return {
            "report": "\n\n".join(parts),
            "trace": [{"node": "summarize", "detail": f"汇总 {len(by_id)} 个结果"}],
        }

    def feedback_node(state: DelegationState) -> dict:
        """暂停图执行并收集用户评分。"""
        value = interrupt(
            {
                "kind": "evaluation",
                "question": "请为本次结果评分（1-5），可附带 comment。",
                "report": state["report"],
                "trace": state.get("trace", []),
            }
        )
        if isinstance(value, Mapping):
            score = int(value.get("score", 0))
            comment = str(value.get("comment", ""))
        else:
            score = int(value)
            comment = ""
        if score not in range(1, 6):
            raise ValueError("评分必须是 1 到 5")
        evaluation: Evaluation = {"score": score, "comment": comment}
        event: TraceEvent = {"node": "feedback", "detail": f"用户评分 {score}"}
        if feedback_sink is not None:
            updated = cast(
                DelegationState,
                {**state, "evaluation": evaluation, "trace": [*state.get("trace", []), event]},
            )
            feedback_sink(updated)
        return {
            "evaluation": evaluation,
            "trace": [event],
        }

    builder = StateGraph(DelegationState)
    builder.add_node("plan", plan_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("ask_user", ask_user_node)
    builder.add_node("dispatch", dispatch_node)
    builder.add_node("execute", execute_node)
    builder.add_node("summarize", summarize_node)
    if collect_feedback:
        builder.add_node("feedback", feedback_node)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "clarify")
    builder.add_conditional_edges("clarify", route_after_clarify)
    builder.add_edge("ask_user", "plan")
    builder.add_conditional_edges("dispatch", dispatch)
    builder.add_edge("execute", "summarize")
    builder.add_edge("summarize", "feedback" if collect_feedback else END)
    if collect_feedback:
        builder.add_edge("feedback", END)

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before) if interrupt_before else None,
        interrupt_after=list(interrupt_after) if interrupt_after else None,
    )
