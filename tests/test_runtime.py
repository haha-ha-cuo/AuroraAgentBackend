"""会话级运行时与前端协议测试。"""

from __future__ import annotations

import io
import json

from aurora.agent.core import Effort, NoClarifier
from aurora.agent.runtime import AgentRuntime, validate_workspace
from aurora.agent.sandbox import Sandbox, UnsafeSubprocessExecutor
from aurora.agent.transport import RuntimeApi, serve_ndjson
from aurora.cli.main import build_parser
from aurora.text import sanitize_text


class StaticPlanner:
    """返回固定任务的测试规划器。"""

    def __init__(self, task):
        self._task = task

    def plan(self, goal):
        return [self._task]


def task(tool, args):
    """创建单个固定测试任务。"""
    return {
        "id": "t1",
        "description": "固定任务",
        "effort": Effort.LOW,
        "tool": tool,
        "args": args,
    }


def make_runtime(planned_task):
    """创建不访问网络且不启用系统沙箱的测试运行时。"""
    return AgentRuntime(
        llm_factory=lambda: object(),
        sandbox_factory=lambda root, mode: Sandbox(root, executor=UnsafeSubprocessExecutor()),
        planner_factory=lambda llm, tools: StaticPlanner(planned_task),
        clarifier_factory=lambda llm: NoClarifier(),
    )


def test_validate_workspace_reports_git_repository(tmp_path):
    (tmp_path / ".git").mkdir()
    info = validate_workspace(tmp_path)
    assert info.path == str(tmp_path.resolve())
    assert info.is_git_repository
    assert info.readable


def test_sanitize_text_recovers_valid_bytes_and_replaces_incomplete_bytes():
    assert sanitize_text("选\udce5\udc8f\udc96目录") == "选取目录"
    assert sanitize_text("工作\udce5\udc8e区") == "工作�区"


def test_sessions_keep_sandbox_tools_bound_to_their_workspace(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "marker.txt").write_text("first", encoding="utf-8")
    (second / "marker.txt").write_text("second", encoding="utf-8")
    runtime = make_runtime(task("read_file", {"path": "marker.txt"}))

    first_run = runtime.create_session(str(first), approval_mode="never").start("读取")
    second_run = runtime.create_session(str(second), approval_mode="never").start("读取")

    assert first_run.state["results"][0]["output"] == "first"
    assert second_run.state["results"][0]["output"] == "second"


def test_interactive_approval_pauses_and_resumes(tmp_path):
    runtime = make_runtime(task("write_file", {"path": "created.txt", "content": "ok"}))
    session = runtime.create_session(str(tmp_path), approval_mode="interactive")

    paused = session.start("写文件")
    assert paused.status == "waiting"
    assert paused.interruptions[0].value["kind"] == "approval"
    assert not (tmp_path / "created.txt").exists()

    completed = session.resume(
        paused.run_id,
        {"approved": True},
        paused.interruptions[0].id,
    )
    assert completed.status == "completed"
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "ok"


def test_runtime_api_emits_approval_and_completion_events(tmp_path):
    api = RuntimeApi(make_runtime(task("write_file", {"path": "api.txt", "content": "ok"})))
    created = api.handle(
        {
            "id": "1",
            "method": "session.create",
            "params": {"workspacePath": str(tmp_path)},
        }
    )[0]["result"]
    started_frames = api.handle(
        {
            "id": "2",
            "method": "run.start",
            "params": {"sessionId": created["sessionId"], "goal": "写文件"},
        }
    )
    pending = started_frames[1]["data"]
    assert started_frames[1]["event"] == "approval.required"

    resumed_frames = api.handle(
        {
            "id": "3",
            "method": "run.resume",
            "params": {
                "sessionId": created["sessionId"],
                "runId": pending["runId"],
                "interruptId": pending["interruptId"],
                "response": {"approved": True},
            },
        }
    )
    assert resumed_frames[0]["result"]["status"] == "completed"
    assert resumed_frames[1]["event"] == "run.completed"


def test_ndjson_server_returns_protocol_frame():
    source = io.StringIO('{"id":"1","method":"runtime.initialize"}\n')
    target = io.StringIO()
    serve_ndjson(RuntimeApi(make_runtime(task("sandbox_list_files", {}))), source, target)
    frame = json.loads(target.getvalue())
    assert frame["id"] == "1"
    assert frame["result"]["protocolVersion"] == "1"


def test_runtime_command_is_registered():
    args = build_parser().parse_args(["runtime"])
    assert args.command == "runtime"
