"""规划器：把目标拆解为子任务并分推理强度档位。"""

from __future__ import annotations

from typing import Protocol

from .state import Effort, Task


class Planner(Protocol):
    """规划器协议：MockPlanner 与未来的 LLMPlanner 共用同一接口。"""

    def plan(self, goal: str) -> list[Task]: ...


_HIGH_KEYWORDS = ("重构", "实现", "开发", "编写", "创建", "设计", "修改", "新增")
_MEDIUM_KEYWORDS = ("分析", "对比", "优化", "调试", "测试", "评估")


def classify_effort(description: str) -> Effort:
    """确定性启发式分档（mock 阶段用）。"""
    if any(k in description for k in _HIGH_KEYWORDS):
        return Effort.HIGH
    if any(k in description for k in _MEDIUM_KEYWORDS):
        return Effort.MEDIUM
    return Effort.LOW


class MockPlanner:
    """确定性规划器：按 goal 关键词生成固定任务，不调用 LLM。"""

    def plan(self, goal: str) -> list[Task]:
        tasks: list[Task] = []

        if any(k in goal for k in ("列出", "文件", "结构", "目录")):
            tasks.append(
                {
                    "id": "list_files",
                    "description": "列出项目文件结构",
                    "effort": Effort.LOW,
                    "tool": "list_files",
                    "args": {"path": "."},
                }
            )

        if any(k in goal for k in ("阅读", "README", "总结", "定位")):
            tasks.append(
                {
                    "id": "read_readme",
                    "description": "阅读 README.md 并总结项目定位",
                    "effort": classify_effort("阅读 README.md 并总结项目定位"),
                    "tool": "read_file",
                    "args": {"path": "README.md"},
                }
            )

        # 兜底：无法从 goal 识别出任务时，默认列出项目结构
        if not tasks:
            tasks.append(
                {
                    "id": "list_files",
                    "description": "列出项目文件结构",
                    "effort": Effort.LOW,
                    "tool": "list_files",
                    "args": {"path": "."},
                }
            )

        return tasks
