"""工具注册与沙箱工具风险分级的测试。"""

from __future__ import annotations

from aurora.agent.tools import RiskLevel, get_available_tools


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
