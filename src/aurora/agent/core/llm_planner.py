"""基于 LLM 的规划器实现：用结构化输出把目标拆成任务列表。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field, field_validator

from aurora.text import sanitize_text

from ...logging import get_logger
from ..tools.base import Tool, format_tools_for_llm
from .planner import ClarificationDecision
from .state import Clarification, Effort, Task

log = get_logger(__name__)


class PlannedTask(BaseModel):
    """LLM 规划出的单个任务。"""

    id: str
    description: str = ""
    effort: Literal["low", "medium", "high"]
    tool: str
    args: dict = Field(default_factory=dict)

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, value: object) -> str:
        """容忍模型把 id 输出成数字等情况，统一转字符串。"""
        return str(value)


class Plan(BaseModel):
    """LLM 规划出的任务列表。"""

    tasks: list[PlannedTask]


class ClarificationAssessment(BaseModel):
    """LLM 对当前计划完整性的判断。"""

    needed: bool
    question: str = ""
    reason: str = ""


PLANNER_SYSTEM = """
你是项目级 AI Agent 的规划器。把用户目标拆解为可直接执行的最小子任务。

可用工具（tool 字段只能从下面选）：
{tools}

推理强度 effort 三档：low（简单查询/读取）、medium（需要分析理解）、high（需要设计/实现）。

必须只输出一个 JSON 对象，不要输出任何解释文字。JSON 结构如下：

{{"tasks": [{{"id": "字符串", "description": "任务描述", "effort": "low",
"tool": "工具名", "args": {{"参数名": "参数值"}}}}]}}

字段要求：
- id：字符串，任务唯一标识
- description：字符串，必须写清楚任务做什么
- effort：low / medium / high
- tool：只能取上面列出的工具名
- args：传给工具的参数对象，字段名与类型必须和上面「参数」标注一致

示例：
{{"tasks": [{{"id": "t1", "description": "列出项目文件结构", "effort": "low",
"tool": "list_files", "args": {{"path": "."}}}}]}}

最多 8 个任务，任务之间尽量独立以便并行。
"""

CLARIFIER_SYSTEM = """
你负责判断执行计划是否缺少必须由用户决定的信息。

只有缺少的信息会显著改变执行目标、范围或结果，且无法安全采用合理默认值时，才需要澄清。
不要询问可从工具获取的信息，不要重复已经问过的问题，每轮最多提出一个简短问题。

必须只输出 JSON：
{{"needed": true, "question": "需要询问的问题", "reason": "判断理由"}}
不需要澄清时输出：
{{"needed": false, "question": "", "reason": "判断理由"}}
"""


class LLMPlanner:
    """基于 LLM 的规划器：用结构化输出把目标拆成任务列表。"""

    def __init__(self, llm, tools: Mapping[str, Tool]) -> None:
        tool_desc = format_tools_for_llm(tools)
        self._tool_names = list(tools.keys())
        # DeepSeek 不支持 json_schema 严格模式、思考模式又不支持 tool_choice，
        # 所以用 json_mode：要求模型输出 JSON 对象，再由 Pydantic 解析。
        # include_raw=True 以便拿到原始响应里的 token 用量。
        self._llm = llm.with_structured_output(Plan, method="json_mode", include_raw=True)
        self._prompt = ChatPromptTemplate.from_messages(
            [
                ("system", PLANNER_SYSTEM),
                ("human", "{goal}"),
            ]
        )
        self._tool_desc = tool_desc

    def plan(self, goal: str) -> list[Task]:
        goal = sanitize_text(goal)
        log.info("LLM 规划开始，目标：%s", goal)
        log.info("可用工具：%s", ", ".join(self._tool_names))

        chain = self._prompt | self._llm
        result = chain.invoke({"goal": goal, "tools": self._tool_desc})

        # include_raw=True 时返回 {"raw", "parsed", "parsing_error"}
        if isinstance(result, dict):
            plan: Plan = result["parsed"]
            raw = result.get("raw")
        else:
            plan = result
            raw = None

        # token 用量反馈
        usage = getattr(raw, "usage_metadata", None) or {}
        if usage:
            log.info(
                "token 用量：输入 %s / 输出 %s / 总计 %s",
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("total_tokens"),
            )

        log.info("LLM 规划出 %d 个子任务：", len(plan.tasks))
        for i, t in enumerate(plan.tasks, 1):
            log.info("  [%d] [%s] %s", i, t.effort, t.description)
            log.info("      tool=%s args=%s", t.tool, t.args)

        # Pydantic 模型 → 现有的 Task TypedDict，graph.py 无需改动
        return [
            {
                "id": t.id,
                "description": t.description or f"执行工具 {t.tool}",
                "effort": Effort(t.effort),
                "tool": t.tool,
                "args": t.args,
            }
            for t in plan.tasks
        ]


class LLMClarifier:
    """使用结构化输出判断计划是否需要用户澄清。"""

    def __init__(self, llm) -> None:
        self._llm = llm.with_structured_output(
            ClarificationAssessment,
            method="json_mode",
        )
        self._prompt = ChatPromptTemplate.from_messages(
            [
                ("system", CLARIFIER_SYSTEM),
                (
                    "human",
                    "用户目标：\n{goal}\n\n当前计划：\n{tasks}\n\n已有澄清：\n{clarifications}",
                ),
            ]
        )

    def assess(
        self,
        goal: str,
        tasks: Sequence[Task],
        clarifications: Sequence[Clarification],
    ) -> ClarificationDecision:
        goal = sanitize_text(goal)
        result = (self._prompt | self._llm).invoke(
            {
                "goal": goal,
                "tasks": tasks,
                "clarifications": clarifications,
            }
        )
        return ClarificationDecision(
            needed=result.needed,
            question=result.question.strip(),
            reason=result.reason.strip(),
        )
