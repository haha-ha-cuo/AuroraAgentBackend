"""CLI 子命令：一个命令对应一个模块。"""

from . import demo, evaluate, sandbox, serve

__all__ = ["demo", "evaluate", "sandbox", "serve"]
