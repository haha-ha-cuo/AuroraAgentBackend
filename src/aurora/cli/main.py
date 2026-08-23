"""Aurora Agent 命令行入口。

薄 CLI 层：只负责参数解析与模块组装，不承载业务逻辑。
可复用逻辑位于 ``aurora.agent.*``。
"""

from __future__ import annotations

import argparse

from rich.console import Console
from rich.tree import Tree

from aurora.agent.core import LLMPlanner, build_delegation_graph
from aurora.agent.model_access import build_llm
from aurora.agent.tools import ListFilesTool, ReadFileTool
from aurora.logging import get_logger

log = get_logger(__name__)

DEFAULT_GOAL = "找到main.py并且分析这份文件的代码在干什么"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aurora",
        description="Aurora Agent —— 形如 CodeX 的全平台桌面 AI Agent（命令行入口）",
    )
    subparsers = parser.add_subparsers(dest="command", title="子命令")

    demo_parser = subparsers.add_parser("demo", help="运行最小纵向切片 demo")
    demo_parser.add_argument("goal", nargs="?", default=DEFAULT_GOAL, help="项目目标")

    subparsers.add_parser("eval", help="运行评估与校准（阶段二）")

    return parser


def _run_demo(goal: str) -> int:
    console = Console()
    tools = {
        "list_files": ListFilesTool(),
        "read_file": ReadFileTool(),
    }

    llm = build_llm()  # 配置不齐会直接抛异常
    planner = LLMPlanner(llm, tools)
    graph = build_delegation_graph(planner, tools)

    console.print(f"[bold]目标:[/bold] {goal}")
    console.print()

    state = graph.invoke({"goal": goal})

    tree = Tree("委派树")
    for task in state["tasks"]:
        tree.add(f"[{task['effort'].value}] {task['description']} → {task['tool']}")
    console.print(tree)
    console.print()

    console.print("[bold]汇总报告:[/bold]")
    console.print(state["report"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "demo":
        return _run_demo(args.goal)

    if args.command == "eval":
        print("eval 子命令尚未实现（阶段二：最小 eval 集）")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
