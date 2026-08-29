"""sandbox 子命令：让 Agent 在沙箱内写代码并运行。"""

from __future__ import annotations

import argparse

from langgraph.checkpoint.memory import InMemorySaver
from rich.console import Console
from rich.tree import Tree

from aurora.agent.core import (
    LLMClarifier,
    LLMPlanner,
    build_delegation_graph,
    invoke_with_responder,
)
from aurora.agent.model_access import build_llm
from aurora.agent.safety import build_gate
from aurora.agent.sandbox import create_sandbox, set_sandbox
from aurora.agent.tools import get_available_tools
from aurora.cli.workflow import respond_to_interrupt

DEFAULT_GOAL = "在沙箱里写一个 Python 脚本，输出 1 到 10 的平方，并运行它"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("sandbox", help="在沙箱中让 Agent 写代码并运行")
    parser.add_argument("goal", nargs="?", default=DEFAULT_GOAL, help="任务目标")
    parser.add_argument(
        "--sandbox-dir", default=None, help="沙箱工作目录（默认 ~/.aurora/sandbox）"
    )
    parser.add_argument(
        "--approve",
        choices=["interactive", "always", "never"],
        default="interactive",
        help="确认门策略：interactive 交互确认 / always 全放行 / never 只读",
    )
    parser.add_argument(
        "--mode",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default="workspace-write",
        help="文件权限：只读 / 仅工作区可写 / 无隔离",
    )
    parser.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    console = Console()
    sandbox = create_sandbox(root=args.sandbox_dir, mode=args.mode)
    set_sandbox(sandbox)
    console.print(f"[bold]沙箱目录:[/bold] {sandbox.root}")
    console.print(f"[bold]沙箱模式:[/bold] {args.mode}")
    console.print(f"[bold]确认门:[/bold] {args.approve}")
    sandbox.prepare()
    console.print(f"[bold]执行后端:[/bold] {sandbox.backend_name}")
    console.print()

    tools = get_available_tools()
    llm = build_llm()
    planner = LLMPlanner(llm, tools)
    gate = build_gate(args.approve)
    graph = build_delegation_graph(
        planner,
        tools,
        gate,
        clarifier=LLMClarifier(llm),
        checkpointer=InMemorySaver(),
    )

    console.print(f"[bold]目标:[/bold] {args.goal}")
    console.print()

    state = invoke_with_responder(
        graph, args.goal, lambda request: respond_to_interrupt(console, request)
    )

    tree = Tree("委派树")
    for task in state["tasks"]:
        tree.add(f"[{task['effort'].value}] {task['description']} → {task['tool']}")
    console.print(tree)
    console.print()

    console.print("[bold]汇总报告:[/bold]")
    console.print(state["report"])
    console.print()

    trace = Tree("调用轨迹")
    for event in state["trace"]:
        trace.add(f"{event['node']} · {event['detail']}")
    console.print(trace)
    console.print()

    console.print("[bold]沙箱内文件:[/bold]")
    console.print(sandbox.list_files())
    return 0
