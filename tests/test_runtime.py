"""会话级运行时与前端协议测试。"""

from __future__ import annotations

import asyncio
import io
import json
import socket
from contextlib import suppress

from websockets.asyncio.client import connect

from aurora.agent.core import Effort, NoClarifier
from aurora.agent.runtime import AgentRuntime, validate_workspace
from aurora.agent.sandbox import Sandbox, UnsafeSubprocessExecutor
from aurora.agent.transport import RuntimeApi, serve_ndjson, serve_websocket
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


def test_ndjson_server_accepts_desktop_wire_envelope():
    source = io.StringIO(
        '{"protocol_version":1,"request_id":"req_1","method":"runtime.initialize","params":{}}\n'
    )
    target = io.StringIO()
    serve_ndjson(RuntimeApi(make_runtime(task("sandbox_list_files", {}))), source, target)
    frame = json.loads(target.getvalue())
    assert frame["request_id"] == "req_1"
    assert frame["ok"] is True
    assert "workspace.validate" in frame["result"]["capabilities"]


def test_websocket_server_returns_protocol_frame():
    async def scenario():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        api = RuntimeApi(make_runtime(task("sandbox_list_files", {})))
        server = asyncio.create_task(serve_websocket(api, port=port))
        try:
            for _ in range(50):
                try:
                    websocket = await connect(f"ws://127.0.0.1:{port}/ws")
                    break
                except OSError:
                    await asyncio.sleep(0.01)
            else:
                raise AssertionError("WebSocket 服务未按时启动")

            try:
                await websocket.send('{"id":"1","method":"runtime.initialize"}')
                frame = json.loads(await websocket.recv())
                assert frame["id"] == "1"
                assert frame["result"]["protocolVersion"] == "1"
            finally:
                await websocket.close()
        finally:
            server.cancel()
            with suppress(asyncio.CancelledError):
                await server
            api.close()

    asyncio.run(scenario())


def test_wire_run_emits_progress_and_message_deltas(tmp_path):
    api = RuntimeApi(make_runtime(task("sandbox_list_files", {})))
    created = api.handle(
        {
            "id": "create",
            "method": "session.create",
            "params": {"workspacePath": str(tmp_path)},
        }
    )[0]["result"]
    frames = []
    api.process_wire(
        {
            "protocol_version": 1,
            "request_id": "run",
            "method": "run.start",
            "params": {"sessionId": created["sessionId"], "goal": "列出文件"},
        },
        frames.append,
    )

    event_types = [frame["type"] for frame in frames if "type" in frame]
    assert event_types[:4] == ["run.started", "plan.created", "task.started", "task.completed"]
    assert "message.started" in event_types
    assert "message.delta" in event_types
    assert "message.completed" in event_types
    assert event_types[-1] == "run.completed"
    response = next(frame for frame in frames if frame.get("request_id") == "run")
    assert response["ok"] is True
    assert response["result"]["status"] == "completed"


def test_wire_stream_resumes_after_approval(tmp_path):
    api = RuntimeApi(make_runtime(task("write_file", {"path": "stream.txt", "content": "ok"})))
    created = api.handle(
        {
            "id": "create",
            "method": "session.create",
            "params": {"workspacePath": str(tmp_path)},
        }
    )[0]["result"]
    started = []
    api.process_wire(
        {
            "protocol_version": 1,
            "request_id": "start",
            "method": "run.start",
            "params": {"sessionId": created["sessionId"], "goal": "写文件"},
        },
        started.append,
    )
    waiting = next(frame["result"] for frame in started if frame.get("request_id") == "start")
    approval = next(
        frame["payload"] for frame in started if frame.get("type") == "approval.required"
    )
    assert waiting["status"] == "waiting"

    resumed = []
    api.process_wire(
        {
            "protocol_version": 1,
            "request_id": "resume",
            "method": "run.resume",
            "params": {
                "sessionId": created["sessionId"],
                "runId": waiting["runId"],
                "interruptId": approval["interruptId"],
                "response": {"approved": True},
            },
        },
        resumed.append,
    )
    assert any(frame.get("type") == "run.resumed" for frame in resumed)
    assert any(frame.get("type") == "message.delta" for frame in resumed)
    assert (tmp_path / "stream.txt").read_text(encoding="utf-8") == "ok"


def test_runtime_command_is_registered():
    args = build_parser().parse_args(["runtime"])
    assert args.command == "runtime"
