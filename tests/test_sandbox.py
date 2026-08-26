"""沙箱工作区与执行后端的测试。"""

from __future__ import annotations

import pytest

from aurora.agent.sandbox import Sandbox, UnsafeSubprocessExecutor


def make_sandbox(tmp_path):
    """创建显式无隔离的执行测试夹具。"""
    return Sandbox(root=tmp_path, executor=UnsafeSubprocessExecutor())


def test_write_and_read_roundtrip(tmp_path):
    sandbox = make_sandbox(tmp_path)
    sandbox.write_file("src/a.py", "print(1)")
    assert "print(1)" in sandbox.read_file("src/a.py")


def test_absolute_path_rejected(tmp_path):
    sandbox = make_sandbox(tmp_path)
    with pytest.raises(ValueError):
        sandbox.write_file("/etc/passwd", "x")


def test_path_traversal_rejected(tmp_path):
    sandbox = make_sandbox(tmp_path)
    with pytest.raises(ValueError):
        sandbox.write_file("../evil.txt", "x")


def test_run_command_captures_output(tmp_path):
    sandbox = make_sandbox(tmp_path)
    result = sandbox.run("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout


def test_run_command_cwd_is_sandbox(tmp_path):
    sandbox = make_sandbox(tmp_path)
    result = sandbox.run("pwd")
    assert result.exit_code == 0
    assert str(tmp_path) in result.stdout


def test_run_python(tmp_path):
    sandbox = make_sandbox(tmp_path)
    result = sandbox.run_python("print(1 + 1)")
    assert result.exit_code == 0
    assert "2" in result.stdout


def test_run_timeout(tmp_path):
    sandbox = make_sandbox(tmp_path)
    result = sandbox.run("sleep 5", timeout=1)
    assert result.timed_out


def test_list_files(tmp_path):
    sandbox = make_sandbox(tmp_path)
    sandbox.write_file("a.py", "x")
    sandbox.write_file("sub/b.py", "y")
    listing = sandbox.list_files()
    assert "a.py" in listing
    assert "b.py" in listing
