"""工具抽象与风险分级。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum


class RiskLevel(str, Enum):
    """工具风险分级。"""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


class Tool(ABC):
    """工具基类。

    属性：
        name: 工具唯一名
        description: 供规划器/模型理解用途
        risk: 风险分级（默认 READ）
    """

    def __init__(
        self,
        name: str,
        description: str,
        risk: RiskLevel = RiskLevel.READ,
    ) -> None:
        self.name = name
        self.description = description
        self.risk = risk

    @abstractmethod
    def run(self, **kwargs: object) -> str:
        """执行工具并返回文本结果。"""
