"""确认门接入委派图的集成测试。"""

from __future__ import annotations

from aurora.agent.core import Effort, build_delegation_graph
from aurora.agent.safety import ConfirmationGate, DenyApprover
from aurora.agent.sandbox import Sandbox, set_sandbox
from aurora.agent.tools import get_available_tools


class _StubPlanner:
    def __init__(self, tasks):
        self._tasks = tasks

    def plan(self, goal):
        return self._tasks


def _task(task_id, tool, args):
    return {
        "id": task_id,
        "description": "stub task",
        "effort": Effort.LOW,
        "tool": tool,
        "args": args,
    }


def test_read_task_passes_deny_gate(tmp_path):
    set_sandbox(Sandbox(root=tmp_path))
    planner = _StubPlanner([_task("1", "sandbox_list_files", {"path": "."})])
    graph = build_delegation_graph(planner, get_available_tools(), ConfirmationGate(DenyApprover()))
    state = graph.invoke({"goal": "g"})
    assert state["results"][0]["ok"] is True


def test_write_task_blocked_by_deny_gate(tmp_path):
    set_sandbox(Sandbox(root=tmp_path))
    planner = _StubPlanner([_task("1", "write_file", {"path": "a.py", "content": "print(1)"})])
    graph = build_delegation_graph(planner, get_available_tools(), ConfirmationGate(DenyApprover()))
    state = graph.invoke({"goal": "g"})
    assert state["results"][0]["ok"] is False
    assert "拒绝" in state["results"][0]["output"]
