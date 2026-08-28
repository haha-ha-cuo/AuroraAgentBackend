"""把用户评分与调用轨迹写入 JSONL 评估集。"""

from __future__ import annotations

import json
from pathlib import Path

from ..agent.core.state import DelegationState


class JsonlFeedbackStore:
    """追加保存可用于后续回归评估的执行记录。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def __call__(self, state: DelegationState) -> None:
        """将一次已评分的图状态追加到评估集。"""
        evaluation = state["evaluation"]
        record = {
            "goal": state["goal"],
            "tasks": [
                {
                    "id": task["id"],
                    "description": task["description"],
                    "effort": task["effort"].value,
                    "tool": task["tool"],
                    "args": task["args"],
                }
                for task in state["tasks"]
            ],
            "report": state["report"],
            "trace": state.get("trace", []),
            "score": evaluation["score"],
            "comment": evaluation["comment"],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
