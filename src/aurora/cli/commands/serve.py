"""serve 子命令：启动 Aurora 持续交互会话。"""

from __future__ import annotations

import argparse

from rich.console import Console
from rich.tree import Tree

from aurora.agent.conversation import ConversationSession, SessionReply
from aurora.agent.core import LLMPlanner, build_delegation_graph
from aurora.agent.model_access import build_llm
from aurora.agent.safety import build_gate
from aurora.agent.sandbox import create_sandbox, set_sandbox
from aurora.agent.tools import get_available_tools


def register(subparsers: argparse._SubParsersAction) -> None:
    """注册 serve 子命令。"""
    parser = subparsers.add_parser("serve", help="启动持续交互式 Aurora 会话")
    parser.add_argument("--sandbox-dir", default=None, help="沙箱工作目录（默认 ~/.aurora/sandbox）")
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
    parser.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    """构建会话依赖并运行输入循环。"""
    console = Console()
    sandbox = create_sandbox(root=args.sandbox_dir, mode=args.mode)
    set_sandbox(sandbox)
    tools = get_available_tools()
    llm = build_llm()
    planner = LLMPlanner(llm, tools)
    graph = build_delegation_graph(planner, tools, build_gate(args.approve))
    session = ConversationSession(llm, planner, lambda goal: graph.invoke({"goal": goal}))

    console.print("[bold cyan]Aurora 服务已启动[/bold cyan]")
    console.print(f"工作区: {sandbox.root}")
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
        console.print(f"[bold cyan]aurora>[/bold cyan] {reply.text}")
