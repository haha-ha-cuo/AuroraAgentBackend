"""委派图的状态与数据模型。"""

from __future__ import annotations

import operator
from enum import StrEnum
from typing import Annotated, NotRequired, TypedDict


class Effort(StrEnum):
    """推理强度（reasoning effort）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Task(TypedDict):
    """委派树中的一个叶子任务。"""

    id: str
    description: str
    effort: Effort
    tool: str
    args: dict[str, object]


class Result(TypedDict):
    """叶子任务的执行结果。"""

    task_id: str
    ok: bool
    output: str


class Clarification(TypedDict):
    """一轮用户澄清。"""

    question: str
    answer: str


class TraceEvent(TypedDict):
    """图中一次节点调用的轨迹。"""

    node: str
    detail: str


class Evaluation(TypedDict):
    """用户对本次执行结果的评价。"""

    score: int
    comment: str


class DelegationState(TypedDict):
    """委派图状态。

    results 用 ``operator.add`` reducer：多个并行分支各自返回一个结果，
    由框架自动累加。
    """

    goal: str
    tasks: list[Task]
    current_task: NotRequired[Task]
    results: Annotated[list[Result], operator.add]
    report: str
    clarification_needed: NotRequired[bool]
    clarification_question: NotRequired[str]
    clarifications: NotRequired[list[Clarification]]
    clarification_count: NotRequired[int]
    trace: Annotated[list[TraceEvent], operator.add]
    evaluation: NotRequired[Evaluation]
