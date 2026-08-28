"""runtime 子命令：通过 stdio 提供桌面前端协议。"""

from __future__ import annotations

import argparse
import asyncio
import sys

from aurora.agent.transport import RuntimeApi, serve_ndjson, serve_websocket


def register(subparsers: argparse._SubParsersAction) -> None:
    """注册 runtime 子命令。"""
    parser = subparsers.add_parser("runtime", help="启动 stdio NDJSON 前端运行时")
    parser.add_argument("--host", default="127.0.0.1", help="WebSocket 监听地址")
    parser.add_argument("--port", type=int, default=None, help="启用浏览器开发用 WebSocket 服务")
    parser.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    """在标准输入输出上运行协议循环。"""
    api = RuntimeApi()
    try:
        if args.port is None:
            serve_ndjson(api, sys.stdin, sys.stdout)
        else:
            try:
                asyncio.run(serve_websocket(api, args.host, args.port))
            except KeyboardInterrupt:
                pass
    finally:
        api.close()
    return 0
