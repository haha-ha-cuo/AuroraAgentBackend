"""统一日志配置。

封装 ``rich`` 的 ``RichHandler``，为所有模块提供一致的彩色分级日志：

    from aurora.logging import get_logger

    log = get_logger(__name__)
    log.warning("这是一条带颜色的警告")

默认级别为 ``INFO``，可用环境变量 ``AURORA_LOG_LEVEL`` 覆盖
（DEBUG / INFO / WARNING / ERROR / CRITICAL）。

配色由 ``RichHandler`` 内置：DEBUG 灰 / INFO 蓝 / WARNING 黄 /
ERROR 红 / CRITICAL 红底白字。
"""

from __future__ import annotations

import logging
import os

from rich.console import Console
from rich.logging import RichHandler

__all__ = ["get_logger", "setup_logging"]

_ENV_LEVEL = "AURORA_LOG_LEVEL"

# 常见吵闹的第三方库，统一压到 WARNING，避免刷屏
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "asyncio", "aiosqlite")

_configured = False
_console = Console(stderr=True)


def _resolve_level(level: int | str | None) -> int:
    """把字符串级别名转换为 logging 级别常量。"""
    if level is None:
        level = os.environ.get(_ENV_LEVEL, logging.INFO)
    if isinstance(level, str):
        return logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    return level


def setup_logging(level: int | str | None = None) -> None:
    """初始化根日志器并挂载 RichHandler（幂等，可重复调用）。"""
    global _configured
    if _configured:
        return

    handler = RichHandler(
        console=_console,
        rich_tracebacks=True,
        show_time=True,
        show_path=False,
    )

    root = logging.getLogger()
    root.setLevel(_resolve_level(level))
    root.addHandler(handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """获取统一风格的 logger；首次调用会自动完成初始化。"""
    setup_logging()
    return logging.getLogger(name)
