"""serve 子命令：启动 Aurora 持续交互会话。"""

from __future__ import annotations

import argparse

from rich.console import Console
from rich.tree import Tree

from aurora.agent.conversation import ConversationSession, SessionReply
from aurora.agent.runtime import AgentRuntime
from aurora.cli.workflow import respond_to_interrupt
from aurora.eval import JsonlFeedbackStore


def register(subparsers: argparse._SubParsersAction) -> None:
    """注册 serve 子命令。"""
    parser = subparsers.add_parser("serve", help="启动持续交互式 Aurora 会话")
    parser.add_argument("--sandbox-dir", default=".", help="工作目录（默认当前目录）")
    parser.add_argument(
        "--approve",
        choices=["interactive", "always", "never"],
        default="interactive",
        help="工具确认策略",
    )
    parser.add_argument(
        "--mode",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default="workspace-write",
        help="本机文件权限模式",
    )
    parser.add_argument("--feedback-file", default=None, help="将用户评分和轨迹追加到 JSONL 文件")
    parser.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    """构建会话依赖并运行输入循环。"""
    console = Console()
    feedback_sink = JsonlFeedbackStore(args.feedback_file) if args.feedback_file else None
    runtime = AgentRuntime()
    agent_session = runtime.create_session(
        args.sandbox_dir,
        sandbox_mode=args.mode,
        approval_mode=args.approve,
        feedback_sink=feedback_sink,
    )
    session = ConversationSession(
        agent_session.llm,
        agent_session.planner,
        lambda goal: agent_session.run_until_complete(
            goal,
            lambda request: respond_to_interrupt(console, request),
        ),
    )

    console.print("[bold cyan]Aurora 服务已启动[/bold cyan]")
    console.print(f"工作区: {agent_session.workspace.path}")
    console.print(f"沙箱模式: {args.mode} · 确认门: {args.approve}")
    console.print("输入 [bold]/help[/bold] 查看指令，输入 [bold]/exit[/bold] 退出。")
    console.print()

    while True:
        try:
            line = console.input("[bold green]you>[/bold green] ")
        except (EOFError, KeyboardInterrupt):
            console.print("\nAurora 服务已停止。")
            return 0
        try:
            reply = session.handle(line)
        except Exception as exc:
            console.print(f"[bold red]执行失败：[/bold red]{exc}")
            continue
        if reply.exit_requested:
            console.print("Aurora 服务已停止。")
            return 0
        _render_reply(console, reply)


def _render_reply(console: Console, reply: SessionReply) -> None:
    """渲染一次会话回复。"""
    if reply.tasks:
        tree = Tree("计划")
        for task in reply.tasks:
            tree.add(f"[{task['effort'].value}] {task['description']} → {task['tool']}")
        console.print(tree)
    if reply.text:
        console.print(f"[bold cyan]agent>>[/bold cyan] {reply.text}")
    if reply.trace:
        trace = Tree("调用轨迹")
        for event in reply.trace:
            trace.add(f"{event['node']} · {event['detail']}")
        console.print(trace)
