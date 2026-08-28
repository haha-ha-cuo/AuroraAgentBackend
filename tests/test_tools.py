"""工具注册与沙箱工具风险分级的测试。"""

from __future__ import annotations

import pytest

from aurora.agent.tools import RiskLevel, get_available_tools
from aurora.agent.tools.base import Tool


def test_sandbox_tools_registered():
    tools = get_available_tools()
    for name in ("write_file", "run_command", "run_python", "sandbox_list_files", "sandbox_read_file"):
        assert name in tools


def test_sandbox_tool_risk_levels():
    tools = get_available_tools()
    assert tools["write_file"].risk == RiskLevel.WRITE
    assert tools["run_command"].risk == RiskLevel.EXECUTE
    assert tools["run_python"].risk == RiskLevel.EXECUTE
    assert tools["sandbox_read_file"].risk == RiskLevel.READ
    assert tools["sandbox_list_files"].risk == RiskLevel.READ


def test_llm_schema_resolves_postponed_annotations():
    tools = get_available_tools()
    properties = tools["run_command"].params_schema["properties"]
    assert properties["command"]["type"] == "string"
    assert properties["timeout"]["type"] == "integer"


def test_tool_converts_model_string_to_declared_type():
    received = {}

    def sample(timeout: int, enabled: bool = True) -> str:
        """记录转换后的工具参数。"""
        received.update(timeout=timeout, enabled=enabled)
        return "ok"

    tool = Tool("sample", "", sample)
    assert tool.run(timeout="30", enabled="false") == "ok"
    assert received == {"timeout": 30, "enabled": False}


def test_tool_rejects_invalid_typed_argument():
    def sample(timeout: int) -> str:
        """返回超时参数。"""
        return str(timeout)

    with pytest.raises(ValueError, match="timeout"):
        Tool("sample", "", sample).run(timeout="invalid")
