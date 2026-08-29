"""CLI 委派图交互辅助。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich.console import Console


def respond_to_interrupt(console: Console, request: Mapping[str, Any]) -> Any:
    """在终端收集澄清回答或评估分数。"""
    if request.get("kind") == "approval":
        tool = request.get("tool")
        risk = request.get("risk")
        console.print(f"[bold yellow]即将执行 {tool}（风险 {risk}）[/bold yellow]")
        console.print(f"参数: {request.get('args', {})}")
        answer = console.input("允许执行？(y/n) ").strip().lower()
        return {"approved": answer in {"y", "yes"}}
    if request.get("kind") == "evaluation":
        score = console.input("[bold yellow]请评分（1-5）>[/bold yellow] ").strip()
        comment = console.input("[bold yellow]补充评价（可留空）>[/bold yellow] ").strip()
        return {"score": int(score), "comment": comment}
    question = str(request.get("question", "请补充信息"))
    return console.input(f"[bold yellow]aurora 需要澄清：[/bold yellow]{question}\n> ").strip()
