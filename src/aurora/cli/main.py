"""Aurora Agent 命令行入口。

薄 CLI 层：只负责参数解析与命令分发，不承载业务逻辑。
每个子命令对应 ``cli/commands`` 下的一个模块。
"""

from __future__ import annotations

import argparse

from .commands import demo, evaluate, runtime, sandbox, serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aurora",
        description="Aurora Agent —— 形如 CodeX 的全平台桌面 AI Agent（命令行入口）",
    )
    subparsers = parser.add_subparsers(dest="command", title="子命令")

    demo.register(subparsers)
    evaluate.register(subparsers)
    sandbox.register(subparsers)
    serve.register(subparsers)
    runtime.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
