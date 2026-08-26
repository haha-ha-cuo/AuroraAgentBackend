"""确认门与放行策略的测试。"""

from __future__ import annotations

import pytest

from aurora.agent.safety import (
    AutoApprover,
    ConfirmationGate,
    DenyApprover,
    InteractiveApprover,
    ToolDeniedError,
    build_gate,
)
from aurora.agent.tools.base import RiskLevel, Tool


def _tool(risk: RiskLevel) -> Tool:
    return Tool(name="t", description="d", func=lambda **kwargs: "ok", risk=risk)


def test_deny_approver_allows_read():
    gate = ConfirmationGate(DenyApprover())
    assert gate.invoke(_tool(RiskLevel.READ), {}) == "ok"


def test_deny_approver_blocks_write():
    gate = ConfirmationGate(DenyApprover())
    with pytest.raises(ToolDeniedError):
        gate.invoke(_tool(RiskLevel.WRITE), {})


def test_deny_approver_blocks_execute():
    gate = ConfirmationGate(DenyApprover())
    with pytest.raises(ToolDeniedError):
        gate.invoke(_tool(RiskLevel.EXECUTE), {})


def test_auto_approver_allows_execute():
    gate = ConfirmationGate(AutoApprover())
    assert gate.invoke(_tool(RiskLevel.EXECUTE), {}) == "ok"


def test_interactive_approver_logs_info_and_prompts(caplog):
    class FakeConsole:
        """记录交互提示并返回允许。"""

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def input(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "y"

    console = FakeConsole()
    approver = InteractiveApprover(console=console)

    with caplog.at_level("INFO"):
        assert approver.approve(_tool(RiskLevel.EXECUTE), {"command": "echo ok"})

    assert console.prompts == ["允许执行？(y/n) "]
    assert "确认门 即将执行 t（风险 execute）" in caplog.text
    assert "参数: {'command': 'echo ok'}" in caplog.text


def test_build_gate_modes():
    assert isinstance(build_gate("interactive")._approver, object)
    assert isinstance(build_gate("always")._approver, AutoApprover)
    assert isinstance(build_gate("never")._approver, DenyApprover)
    with pytest.raises(ValueError):
        build_gate("unknown")
