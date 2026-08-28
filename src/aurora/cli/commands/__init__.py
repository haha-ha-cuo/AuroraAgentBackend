"""CLI 子命令：一个命令对应一个模块。"""

from . import demo, evaluate, runtime, sandbox, serve

__all__ = ["demo", "evaluate", "runtime", "sandbox", "serve"]
