"""内置与外部 MCP 功能包注册表。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any

from .base import McpPackage
from .loader import discover_package_directories, load_package_directory

ENTRY_POINT_GROUP = "aurora.mcp_packages"
_PACKAGE_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
_BUILTIN_ROOT = Path(__file__).parent


class McpPackageRegistry:
    """发现、校验并索引 MCP 功能包。"""

    def __init__(
        self,
        packages: Iterable[McpPackage] | None = None,
        *,
        discover_plugins: bool = True,
    ) -> None:
        self._packages: dict[str, McpPackage] = {}
        self._plugin_errors: dict[str, str] = {}
        initial = discover_package_directories(_BUILTIN_ROOT) if packages is None else packages
        for package in initial:
            self.register(package)
        if discover_plugins:
            for point in entry_points(group=ENTRY_POINT_GROUP):
                try:
                    self.register(self._load(point))
                except Exception as exc:
                    self._plugin_errors[point.name] = str(exc)

    def register(self, package: McpPackage) -> None:
        """注册一个实现统一接口的功能包。"""
        if not isinstance(package, McpPackage):
            raise TypeError("MCP 功能包未实现统一接口")
        package_id = package.manifest.id
        if not _PACKAGE_ID.fullmatch(package_id) or package_id in self._packages:
            raise ValueError(f"MCP 功能包 ID 非法或重复: {package_id}")
        self._packages[package_id] = package

    def get(self, package_id: str) -> McpPackage:
        """按 ID 返回功能包。"""
        try:
            return self._packages[package_id]
        except KeyError as exc:
            raise ValueError(f"MCP 功能包不存在: {package_id}") from exc

    def list(self) -> list[McpPackage]:
        """按 ID 返回全部功能包。"""
        return [self._packages[key] for key in sorted(self._packages)]

    def plugin_errors(self) -> dict[str, str]:
        """返回未能加载的外部插件及原因。"""
        return dict(self._plugin_errors)

    @staticmethod
    def _load(point: EntryPoint) -> McpPackage:
        """加载 entry point 暴露的实例、类或工厂。"""
        loaded: Any = point.load()
        if isinstance(loaded, type):
            loaded = loaded()
        if isinstance(loaded, McpPackage):
            return loaded
        if callable(loaded):
            loaded = loaded()
        if isinstance(loaded, McpPackage):
            return loaded
        if isinstance(loaded, (str, Path)):
            return load_package_directory(loaded)
        raise TypeError(f"MCP 插件 {point.name} 未提供有效功能包")
