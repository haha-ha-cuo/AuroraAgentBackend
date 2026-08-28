"""澄清循环、轨迹、评分和断点测试。"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from aurora.agent.core import ClarificationDecision, Effort, build_delegation_graph
from aurora.agent.tools.base import Tool
from aurora.eval import JsonlFeedbackStore


class RecordingPlanner:
    """记录每轮规划输入并返回固定任务。"""

    def __init__(self) -> None:
        self.goals = []

    def plan(self, goal):
        self.goals.append(goal)
        return [
            {
                "id": "t1",
                "description": "读取目标",
                "effort": Effort.LOW,
                "tool": "echo",
                "args": {"value": goal},
            }
        ]


class OnceClarifier:
    """首轮要求澄清，后续接受计划。"""

    def assess(self, goal, tasks, clarifications):
        if clarifications:
            return ClarificationDecision(False, reason="信息充分")
        return ClarificationDecision(True, "目标平台是什么？", "缺少平台")


class AlwaysClarifier:
    """每轮都要求继续澄清。"""

    def assess(self, goal, tasks, clarifications):
        return ClarificationDecision(True, "还需要什么？", "仍不充分")


def make_tools():
    """创建无副作用测试工具。"""
    return {"echo": Tool("echo", "回显", lambda value: value)}


def test_clarification_interrupt_resumes_into_replanning():
    planner = RecordingPlanner()
    graph = build_delegation_graph(
        planner,
        make_tools(),
        clarifier=OnceClarifier(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "clarification"}}

    paused = graph.invoke({"goal": "构建应用"}, config)
    request = paused["__interrupt__"][0].value
    assert request["kind"] == "clarification"
    assert request["question"] == "目标平台是什么？"

    state = graph.invoke(Command(resume="macOS"), config)
    assert len(planner.goals) == 2
    assert "回答：macOS" in planner.goals[1]
    assert state["clarifications"][0]["answer"] == "macOS"
    assert [event["node"] for event in state["trace"]] == [
        "plan",
        "clarify",
        "ask_user",
        "plan",
        "clarify",
        "execute",
        "summarize",
    ]


def test_clarification_loop_stops_at_limit():
    planner = RecordingPlanner()
    graph = build_delegation_graph(
        planner,
        make_tools(),
        clarifier=AlwaysClarifier(),
        max_clarification_rounds=1,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "limit"}}

    graph.invoke({"goal": "构建应用"}, config)
    state = graph.invoke(Command(resume="macOS"), config)
    assert state["clarification_count"] == 1
    assert state["report"]
    assert state["trace"][-3]["detail"] == "达到澄清轮数上限"


def test_feedback_interrupt_records_score_and_trace():
    graph = build_delegation_graph(
        RecordingPlanner(),
        make_tools(),
        checkpointer=InMemorySaver(),
        collect_feedback=True,
    )
    config = {"configurable": {"thread_id": "feedback"}}

    paused = graph.invoke({"goal": "回显"}, config)
    assert paused["__interrupt__"][0].value["kind"] == "evaluation"

    state = graph.invoke(Command(resume={"score": 5, "comment": "准确"}), config)
    assert state["evaluation"] == {"score": 5, "comment": "准确"}
    assert state["trace"][-1] == {"node": "feedback", "detail": "用户评分 5"}


def test_static_breakpoint_pauses_before_execute():
    graph = build_delegation_graph(
        RecordingPlanner(),
        make_tools(),
        checkpointer=InMemorySaver(),
        interrupt_before=["execute"],
    )
    config = {"configurable": {"thread_id": "breakpoint"}}

    state = graph.invoke({"goal": "回显"}, config)
    assert state["results"] == []
    assert graph.get_state(config).next == ("execute",)


def test_feedback_sink_appends_eval_record(tmp_path):
    path = tmp_path / "eval" / "feedback.jsonl"
    graph = build_delegation_graph(
        RecordingPlanner(),
        make_tools(),
        checkpointer=InMemorySaver(),
        collect_feedback=True,
        feedback_sink=JsonlFeedbackStore(path),
    )
    config = {"configurable": {"thread_id": "feedback-store"}}

    graph.invoke({"goal": "回显"}, config)
    graph.invoke(Command(resume={"score": 4, "comment": "可改进"}), config)

    content = path.read_text(encoding="utf-8")
    assert '"goal": "回显"' in content
    assert '"score": 4' in content
    assert '"node": "feedback"' in content


def test_empty_plan_reaches_empty_summary():
    planner = RecordingPlanner()
    planner.plan = lambda goal: []
    graph = build_delegation_graph(planner, make_tools())

    state = graph.invoke({"goal": "无需执行"})
    assert state["report"] == ""
    assert state["trace"][-1] == {"node": "summarize", "detail": "汇总 0 个结果"}
