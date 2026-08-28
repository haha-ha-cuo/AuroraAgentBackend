"""委派图人工中断恢复辅助。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from langgraph.types import Command


def invoke_with_responder(
    graph,
    goal: str,
    responder: Callable[[Mapping[str, Any]], Any],
    *,
    thread_id: str | None = None,
) -> Mapping[str, Any]:
    """持续恢复动态中断，直到图执行结束。"""
    config = {"configurable": {"thread_id": thread_id or uuid4().hex}}
    value: Any = {"goal": goal}
    while True:
        state = graph.invoke(value, config)
        interruptions = state.get("__interrupt__", [])
        if not interruptions:
            return state
        response = responder(interruptions[0].value)
        value = Command(resume=response)
