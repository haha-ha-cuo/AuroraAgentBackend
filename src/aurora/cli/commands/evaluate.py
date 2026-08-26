"""eval 子命令：运行评估与校准（阶段二）。"""

from __future__ import annotations

import argparse


def register(subparsers: argparse._SubParsersAction) -> None:
    subparsers.add_parser("eval", help="运行评估与校准（阶段二）").set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    print("eval 子命令尚未实现（阶段二：最小 eval 集）")
    return 0
