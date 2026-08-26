"""规划器协议与推理强度启发式。"""

from __future__ import annotations

from typing import Protocol

from .state import Effort, Task


class Planner(Protocol):
    """规划器协议：LLMPlanner 等实现共用同一接口。"""

    def plan(self, goal: str) -> list[Task]: ...


_HIGH_KEYWORDS = ("重构", "实现", "开发", "编写", "创建", "设计", "修改", "新增")
_MEDIUM_KEYWORDS = ("分析", "对比", "优化", "调试", "测试", "评估")


def classify_effort(description: str) -> Effort:
    """确定性启发式分档（供确定性测试/回退场景使用）。"""
    if any(k in description for k in _HIGH_KEYWORDS):
        return Effort.HIGH
    if any(k in description for k in _MEDIUM_KEYWORDS):
        return Effort.MEDIUM
    return Effort.LOW
