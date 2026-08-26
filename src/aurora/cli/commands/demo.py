"""demo 子命令：运行最小纵向切片。"""

from __future__ import annotations

import argparse

from rich.console import Console
from rich.tree import Tree

from aurora.agent.core import LLMPlanner, build_delegation_graph
from aurora.agent.model_access import build_llm
from aurora.agent.tools import get_available_tools

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
    graph = build_delegation_graph(planner, tools)

    console.print(f"[bold]目标:[/bold] {args.goal}")
    console.print()

    state = graph.invoke({"goal": args.goal})

    tree = Tree("委派树")
    for task in state["tasks"]:
        tree.add(f"[{task['effort'].value}] {task['description']} → {task['tool']}")
    console.print(tree)
    console.print()

    console.print("[bold]汇总报告:[/bold]")
    console.print(state["report"])
    return 0
