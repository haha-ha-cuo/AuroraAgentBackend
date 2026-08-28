"""跨终端与 JSON 边界的 Unicode 文本清洗。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def sanitize_text(value: str) -> str:
    """修复 surrogateescape 字节并替换无法编码的代理字符。"""
    if not any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        return value
    surrogates = [char for char in value if 0xD800 <= ord(char) <= 0xDFFF]
    if all(0xDC80 <= ord(char) <= 0xDCFF for char in surrogates):
        return value.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
    return value.encode("utf-16", errors="surrogatepass").decode("utf-16", errors="replace")


def sanitize_value(value: Any) -> Any:
    """递归清洗协议对象中的字符串键和值。"""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        return {sanitize_text(str(key)): sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_value(item) for item in value)
    return value
