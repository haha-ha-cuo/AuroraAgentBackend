"""runtime 子命令：通过 stdio 提供桌面前端协议。"""

from __future__ import annotations

import argparse
import sys

from aurora.agent.transport import RuntimeApi, serve_ndjson


def register(subparsers: argparse._SubParsersAction) -> None:
    """注册 runtime 子命令。"""
    parser = subparsers.add_parser("runtime", help="启动 stdio NDJSON 前端运行时")
    parser.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    """在标准输入输出上运行协议循环。"""
    api = RuntimeApi()
    try:
        serve_ndjson(api, sys.stdin, sys.stdout)
    finally:
        api.close()
    return 0
