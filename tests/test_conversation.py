"""持续会话和斜杠指令测试。"""

from __future__ import annotations

from langchain_core.messages import AIMessage
from rich.console import Console

from aurora.agent.conversation import ConversationSession, SessionReply
from aurora.agent.core import Effort
from aurora.cli.commands.serve import _render_reply
from aurora.cli.main import build_parser


class FakeLlm:
    """记录消息的确定性对话模型。"""

    def __init__(self) -> None:
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return AIMessage(content=f"回答:{messages[-1].content}")


class FakePlanner:
    """返回固定单任务计划。"""

    def __init__(self) -> None:
        self.goals = []

    def plan(self, goal):
        self.goals.append(goal)
        return [{"id": "1", "description": goal, "effort": Effort.LOW, "tool": "list_files", "args": {}}]


def make_session():
    """创建带调用记录的会话夹具。"""
    llm = FakeLlm()
    planner = FakePlanner()
    runs = []

    def run(goal):
        runs.append(goal)
        return {"tasks": planner.plan(goal), "report": f"完成:{goal}"}

    return ConversationSession(llm, planner, run), llm, planner, runs


def test_plain_text_and_say_keep_conversation_history():
    session, llm, _, _ = make_session()
    assert session.handle("你好").text == "回答:你好"
    assert session.handle("/say 继续").text == "回答:继续"
    assert session.history_size == 4
    assert len(llm.calls[0]) == 2
    assert len(llm.calls[1]) == 4


def test_model_role_prefix_is_removed():
    session, llm, _, _ = make_session()
    llm.invoke = lambda messages: AIMessage(content="Aurora: 这是正文")
    assert session.handle("你好").text == "这是正文"


def test_console_uses_agent_reply_label():
    console = Console(record=True, force_terminal=False)
    _render_reply(console, SessionReply(text="这是正文"))
    output = console.export_text()
    assert "agent>> 这是正文" in output
    assert "aurora>" not in output


def test_plan_only_builds_tasks():
    session, _, planner, runs = make_session()
    reply = session.handle("/plan 检查项目")
    assert reply.tasks[0]["description"] == "检查项目"
    assert planner.goals == ["检查项目"]
    assert runs == []


def test_run_executes_goal():
    session, _, _, runs = make_session()
    reply = session.handle("/run 修复测试")
    assert reply.text == "完成:修复测试"
    assert runs == ["修复测试"]


def test_run_sanitizes_surrogateescaped_terminal_input():
    session, _, _, runs = make_session()
    session.handle("/run 查看工作\udce5\udc8e区")
    assert runs == ["查看工作�区"]
    runs[0].encode("utf-8")


def test_help_unknown_clear_and_exit():
    session, _, _, _ = make_session()
    assert "/plan" in session.handle("/help").text
    assert "未知指令" in session.handle("/missing").text
    session.handle("one")
    assert session.handle("/clear").text == "对话上下文已清空。"
    assert session.history_size == 0
    assert session.handle("/exit").exit_requested
    assert session.handle("/quit").exit_requested


def test_missing_arguments_return_usage():
    session, _, _, _ = make_session()
    assert session.handle("/say").text.startswith("用法")
    assert session.handle("/plan").text.startswith("用法")
    assert session.handle("/run").text.startswith("用法")


def test_serve_command_is_registered():
    args = build_parser().parse_args(["serve", "--mode", "read-only", "--approve", "never"])
    assert args.command == "serve"
    assert args.sandbox_dir == "."
    assert args.mode == "read-only"
    assert args.approve == "never"
