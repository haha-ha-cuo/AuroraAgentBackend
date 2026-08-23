"""规划器：把目标拆解为子任务并分推理强度档位。"""

from __future__ import annotations

from typing import Literal, Protocol

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, field_validator

from ...logging import get_logger
from ..tools.base import Tool
from .state import Effort, Task

log = get_logger(__name__)


class Planner(Protocol):
    """规划器协议：LLMPlanner 与未来的 MockPlanner 共用同一接口。"""

    def plan(self, goal: str) -> list[Task]: ...


# ---------- 推理强度启发式（保留给确定性测试用） ----------

_HIGH_KEYWORDS = ("重构", "实现", "开发", "编写", "创建", "设计", "修改", "新增")
_MEDIUM_KEYWORDS = ("分析", "对比", "优化", "调试", "测试", "评估")


def classify_effort(description: str) -> Effort:
    """确定性启发式分档。"""
    if any(k in description for k in _HIGH_KEYWORDS):
        return Effort.HIGH
    if any(k in description for k in _MEDIUM_KEYWORDS):
        return Effort.MEDIUM
    return Effort.LOW


# ---------- LLM 规划器 ----------

class PlannedTask(BaseModel):
    id: str
    description: str = ""
    effort: Literal["low", "medium", "high"]
    tool: str
    args: dict = {}

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, value: object) -> str:
        """容忍模型把 id 输出成数字等情况，统一转字符串。"""
        return str(value)


class Plan(BaseModel):
    tasks: list[PlannedTask]


PLANNER_SYSTEM = """你是项目级 AI Agent 的规划器。把用户目标拆解为可直接执行的最小子任务。

可用工具（tool 字段只能从下面选）：
{tools}

推理强度 effort 三档：low（简单查询/读取）、medium（需要分析理解）、high（需要设计/实现）。

必须只输出一个 JSON 对象，不要输出任何解释文字。JSON 结构如下：

{{"tasks": [{{"id": "字符串", "description": "任务描述", "effort": "low", "tool": "工具名", "args": {{"参数名": "参数值"}}}}]}}

字段要求：
- id：字符串，任务唯一标识
- description：字符串，必须写清楚任务做什么
- effort：low / medium / high
- tool：只能取上面列出的工具名
- args：传给工具的参数对象；list_files 用 {{"path": "."}}，read_file 用 {{"path": "文件名"}}

示例：
{{"tasks": [{{"id": "t1", "description": "列出项目文件结构", "effort": "low", "tool": "list_files", "args": {{"path": "."}}}}]}}

最多 8 个任务，任务之间尽量独立以便并行。
"""


class LLMPlanner:
    """基于 LLM 的规划器：用结构化输出把目标拆成任务列表。"""

    def __init__(self, llm, tools: dict[str, Tool]) -> None:
        tool_desc = "\n".join(f"- {n}: {t.description}" for n, t in tools.items())
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
