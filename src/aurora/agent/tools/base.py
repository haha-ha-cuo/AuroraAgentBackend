"""工具抽象、风险分级与声明式注册。

一个工具 = 一段元数据（name / description / risk / params_schema）+ 一个实现函数。
通过 ``@tool`` 装饰器声明并自动注册进模块级注册表，执行阶段用
:func:`get_available_tools` 一次性取回全部工具。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, get_args, get_origin

F = Callable[..., str]


class RiskLevel(str, Enum):
    """工具风险分级。"""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


@dataclass
class Tool:
    """一个已注册的工具。

    属性：
        name: 工具唯一名
        description: 供规划器/模型理解用途
        func: 底层实现函数
        risk: 风险分级（默认 READ）
        params_schema: 由函数签名生成的 JSON Schema 参数描述
    """

    name: str
    description: str
    func: F
    risk: RiskLevel = RiskLevel.READ
    params_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    def run(self, **kwargs: Any) -> str:
        """执行工具并返回文本结果。"""
        return self.func(**kwargs)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Tool] = {}


def tool(
    name: str | None = None,
    description: str | None = None,
    risk: RiskLevel = RiskLevel.READ,
) -> Callable[[F], F]:
    """声明式注册装饰器：把函数注册为一个 Tool。

    Args:
        name: 工具名，缺省取函数名。
        description: 用途描述，缺省取 docstring 首段。
        risk: 风险分级。
    """

    def decorator(func: F) -> F:
        tool_name = name or func.__name__
        tool_description = description or _first_paragraph(func.__doc__)
        if tool_name in _REGISTRY:
            raise ValueError(f"工具重复注册：{tool_name}")
        _REGISTRY[tool_name] = Tool(
            name=tool_name,
            description=tool_description,
            func=func,
            risk=risk,
            params_schema=_schema_from_signature(func),
        )
        return func

    return decorator


def get_available_tools() -> dict[str, Tool]:
    """返回当前已注册的全部工具（副本，供执行阶段消费）。"""
    return dict(_REGISTRY)


def clear_tools() -> None:
    """清空注册表（主要用于测试隔离）。"""
    _REGISTRY.clear()


def format_tools_for_llm(tools: Mapping[str, Tool]) -> str:
    """把注册表渲染成给 LLM 看的工具清单（含参数 schema）。"""
    lines: list[str] = []
    for name, t in tools.items():
        line = f"- {name}: {t.description}"
        params = _render_params_schema(t.params_schema)
        if params:
            line += f"；参数：{params}"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

_JSON_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _first_paragraph(docstring: str | None) -> str:
    if not docstring:
        return ""
    return docstring.strip().split("\n", 1)[0].strip()


def _annotation_to_json_type(annotation: Any) -> str:
    """把 Python 类型注解映射为 JSON Schema 基本类型。"""
    if annotation is inspect.Parameter.empty:
        return "string"
    origin = get_origin(annotation)
    if origin is not None:
        # Optional[str] -> Union[str, None]：取第一个非 None 分支
        args = [a for a in get_args(annotation) if a is not type(None)]
        if args:
            return _annotation_to_json_type(args[0])
        return "string"
    return _JSON_TYPE_MAP.get(annotation, "string")


def _schema_from_signature(func: Callable[..., str]) -> dict[str, Any]:
    """从函数签名（类型注解 + 默认值）生成参数 JSON Schema。"""
    sig = inspect.signature(func)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        prop: dict[str, Any] = {
            "type": _annotation_to_json_type(param.annotation),
        }
        if param.default is not inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(pname)
        properties[pname] = prop

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _render_params_schema(schema: dict[str, Any]) -> str:
    """把参数 schema 渲染成一行紧凑描述，供 LLM prompt 使用。"""
    props: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    parts: list[str] = []
    for pname, spec in props.items():
        ptype = spec.get("type", "string")
        if pname in required:
            parts.append(f"{pname}({ptype}, 必填)")
        elif "default" in spec:
            parts.append(f"{pname}({ptype}, 默认 {spec['default']!r})")
        else:
            parts.append(f"{pname}({ptype})")
    return ", ".join(parts)
