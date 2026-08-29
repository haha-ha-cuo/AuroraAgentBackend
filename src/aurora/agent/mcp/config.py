"""MCP Server 启动配置。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class McpServerConfig:
    """一个本地 stdio MCP Server 的声明。"""

    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 30.0

    def __post_init__(self) -> None:
        """校验配置并规范化可选路径。"""
        if not _SERVER_NAME.fullmatch(self.name):
            raise ValueError("MCP Server 名称只能包含字母、数字、下划线和连字符")
        if not self.command.strip():
            raise ValueError("MCP Server command 不能为空")
        if self.timeout <= 0:
            raise ValueError("MCP Server timeout 必须大于 0")
        if self.cwd is not None:
            path = Path(self.cwd).expanduser().resolve()
            if not path.is_dir():
                raise ValueError(f"MCP Server cwd 不是目录: {path}")
            object.__setattr__(self, "cwd", str(path))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> McpServerConfig:
        """从前端协议对象创建配置。"""
        args = value.get("args", [])
        env = value.get("env", {})
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("MCP Server args 必须是字符串数组")
        if not isinstance(env, Mapping) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in env.items()
        ):
            raise ValueError("MCP Server env 必须是字符串对象")
        name = value.get("name")
        command = value.get("command")
        if not isinstance(name, str) or not isinstance(command, str):
            raise ValueError("MCP Server name 和 command 必须是字符串")
        cwd = value.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("MCP Server cwd 必须是字符串")
        return cls(
            name=name,
            command=command,
            args=tuple(args),
            cwd=cwd,
            env=dict(env),
            timeout=float(value.get("timeout", 30.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为不包含环境变量值的公开配置。"""
        return {
            "name": self.name,
            "command": self.command,
            "args": list(self.args),
            "cwd": self.cwd,
            "envKeys": sorted(self.env),
            "timeout": self.timeout,
        }
