"""Aurora 持续会话与斜杠指令。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Mapping

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from aurora.text import sanitize_text

from .core import Planner, Task, TraceEvent

SYSTEM_PROMPT = """你是 Aurora，一个运行在用户本机的项目级 AI Agent。
用清晰、直接的语言回答用户；不知道的信息明确说明，不虚构已经执行的操作。
只输出回答正文，不添加“Agent:”“Assistant:”或“Aurora:”等角色前缀。
先给结论，再给必要细节，不复述用户问题，不添加无关的客套话。
斜杠指令由宿主处理，你只负责 /say 对话。"""

_REPLY_PREFIX = re.compile(r"^\s*(?:agent|assistant|aurora)\s*(?::|：|>>?)\s*", re.IGNORECASE)

HELP_TEXT = """可用指令：
/say <内容>    与 Aurora 对话；普通文本等同于 /say
/plan <目标>   生成执行计划，但不调用工具
/run <目标>    规划并执行目标
/clear         清空 /say 对话上下文
/help          显示帮助
/exit          结束服务"""


@dataclass
class SessionReply:
    """一次会话指令的结构化结果。"""

    text: str = ""
    tasks: list[Task] | None = None
    trace: list[TraceEvent] | None = None
    exit_requested: bool = False


class ConversationSession:
    """维护对话历史并分发 Aurora 斜杠指令。"""

    def __init__(
        self,
        llm,
        planner: Planner,
        run_goal: Callable[[str], Mapping],
    ) -> None:
        self._llm = llm
        self._planner = planner
        self._run_goal = run_goal
        self._history: list[BaseMessage] = []

    @property
    def history_size(self) -> int:
        """返回已保存的对话消息数。"""
        return len(self._history)

    def handle(self, line: str) -> SessionReply:
        """解析并执行一行用户输入。"""
        value = sanitize_text(line).strip()
        if not value:
            return SessionReply()
        if not value.startswith("/"):
            return self._say(value)
        command, _, argument = value.partition(" ")
        handlers = {
            "/help": lambda _: SessionReply(text=HELP_TEXT),
            "/say": self._say,
            "/plan": self._plan,
            "/run": self._run,
            "/clear": self._clear,
            "/exit": lambda _: SessionReply(exit_requested=True),
            "/quit": lambda _: SessionReply(exit_requested=True),
        }
        handler = handlers.get(command.lower())
        if handler is None:
            return SessionReply(text=f"未知指令：{command}\n输入 /help 查看可用指令。")
        return handler(argument.strip())

    def _say(self, message: str) -> SessionReply:
        """调用模型回答并保存多轮上下文。"""
        if not message:
            return SessionReply(text="用法：/say <内容>")
        human = HumanMessage(content=message)
        response = self._llm.invoke([SystemMessage(content=SYSTEM_PROMPT), *self._history, human])
        ai = response if isinstance(response, BaseMessage) else AIMessage(content=str(response))
        self._history.extend([human, ai])
        return SessionReply(text=_message_text(ai))

    def _plan(self, goal: str) -> SessionReply:
        """生成目标计划但不执行工具。"""
        if not goal:
            return SessionReply(text="用法：/plan <目标>")
        return SessionReply(text=f"已生成 {len(tasks := self._planner.plan(goal))} 个任务。", tasks=tasks)

    def _run(self, goal: str) -> SessionReply:
        """规划并执行目标。"""
        if not goal:
            return SessionReply(text="用法：/run <目标>")
        state = self._run_goal(goal)
        return SessionReply(
            text=str(state.get("report", "")),
            tasks=state.get("tasks"),
            trace=state.get("trace"),
        )

    def _clear(self, _: str) -> SessionReply:
        """清空直接对话上下文。"""
        self._history.clear()
        return SessionReply(text="对话上下文已清空。")


def _message_text(message: BaseMessage) -> str:
    """把模型消息内容转换为终端文本。"""
    if isinstance(message.content, str):
        return _normalize_reply(message.content)
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return _normalize_reply("\n".join(parts) or str(message.content))


def _normalize_reply(text: str) -> str:
    """清理模型自行添加的角色前缀和外围空白。"""
    return _REPLY_PREFIX.sub("", sanitize_text(text), count=1).strip()
