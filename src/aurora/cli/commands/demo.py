"""demo 子命令：运行最小纵向切片。"""

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
from aurora.agent.tools import get_available_tools
from aurora.cli.workflow import respond_to_interrupt

DEFAULT_GOAL = "找到main.py并且分析这份文件的代码在干什么"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("demo", help="运行最小纵向切片 demo")
    parser.add_argument("goal", nargs="?", default=DEFAULT_GOAL, help="项目目标")
    parser.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    console = Console()
    tools = get_available_tools()

    llm = build_llm()
    planner = LLMPlanner(llm, tools)
    graph = build_delegation_graph(
        planner,
        tools,
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
    return 0
