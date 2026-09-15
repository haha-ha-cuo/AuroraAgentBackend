"""验证 Agent 注册、动态委派与审批恢复。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select
from test_persistence import runtime_at

from aurora.agent.core import Effort
from aurora.agent.manager.registry import AgentRegistry
from aurora.agent.store.database import Database
from aurora.agent.store.model import AgentRun


class DelegatingPlanner:
    """根据所在角色返回确定的父子任务。"""

    def __init__(self, tools, child_id, seen, denied=False):
        self.tools = tools
        self.child_id = child_id
        self.seen = seen
        self.denied = denied

    def set_instructions(self, value):
        self.seen.append(value)

    def plan(self, goal):
        if "call_agent" in self.tools:
            assert self.child_id in self.tools["list_agents"].description
            return [
                dict(
                    id="parent",
                    description="交给专家",
                    effort=Effort.LOW,
                    tool="call_agent",
                    args={"agent_id": self.child_id, "task": "完成两个写入"},
                )
            ]
        assert "list_agents" not in self.tools
        return [
            dict(
                id=str(i),
                description="写入",
                effort=Effort.LOW,
                tool="write_file",
                args={"path": f"{i}.txt", "content": str(i)},
            )
            for i in (1, 2)
        ]


def setup_registry(tmp_path, monkeypatch, allowed=None):
    """创建主 Agent 与可调用的专家角色。"""
    monkeypatch.setenv("AGENT_API_KEY", "registry-test-credential")
    runtime, workspace, _, _ = runtime_at(tmp_path)
    store = runtime.records
    model = store.list_configs("model")[0]
    store.save("model", {"model_name": "text-test"}, model["id"])
    child = store.save(
        "agent",
        {
            "name": "专家",
            "description": "写入两个文件",
            "instructions": "你是专家，只处理分配的子任务。",
            "model_config_id": model["id"],
            "callable": True,
            "allowed_tools": ["write_file"] if allowed is None else allowed,
        },
    )
    seen = []
    runtime._planner_factory = lambda model, tools: DelegatingPlanner(tools, child["id"], seen)
    return runtime, workspace, child, seen


def test_delegate_multiple_approvals_do_not_replay(tmp_path, monkeypatch):
    runtime, workspace, child, seen = setup_registry(tmp_path, monkeypatch)
    try:
        session = runtime.create_session(str(workspace), approval_mode="interactive")
        result = session.start("执行专家任务")
        assert result.status == "waiting"
        assert not (workspace / "1.txt").exists()
        registry = AgentRegistry(runtime)
        status = next(row for row in registry.status()["items"] if row["id"] == child["id"])
        assert status["waitingCount"] == 1
        runtime.records.save("agent", {"enabled": False, "instructions": "新 Prompt"}, child["id"])
        result = session.resume(result.run_id, {"approved": True})
        assert result.status == "waiting"
        assert (workspace / "1.txt").read_text() == "1"
        assert not (workspace / "2.txt").exists()
        (workspace / "1.txt").write_text("不要重放")
        result = session.resume(result.run_id, {"approved": True})
        assert result.status == "completed"
        assert (workspace / "1.txt").read_text() == "不要重放"
        assert (workspace / "2.txt").read_text() == "2"
        rows = runtime.records.db.rows(select(AgentRun).where(AgentRun.agent_id == child["id"]))
        assert len(rows) == 1
        assert rows[0]["parent_task_id"]
        assert rows[0]["status"] == "completed"
        assert (
            json.loads(rows[0]["config_snapshot_json"])["agent"]["instructions"]
            == child["instructions"]
        )
        assert child["instructions"] in seen
    finally:
        runtime.close()


def test_delegate_denial_and_tool_permissions_fail_parent(tmp_path, monkeypatch):
    runtime, workspace, child, _ = setup_registry(tmp_path, monkeypatch, allowed=[])
    try:
        session = runtime.create_session(str(workspace), approval_mode="always")
        with pytest.raises(RuntimeError, match="失败"):
            session.start("不允许使用写入工具")
        assert not (workspace / "1.txt").exists()
        rows = runtime.records.db.rows(select(AgentRun).where(AgentRun.agent_id == child["id"]))
        assert rows[0]["status"] == "failed"
    finally:
        runtime.close()


def test_registry_types_tests_and_config_invalidation(tmp_path, monkeypatch):
    runtime, _, child, _ = setup_registry(tmp_path, monkeypatch)
    try:
        registry = AgentRegistry(runtime)
        calls = []

        class Model:
            def invoke(self, messages):
                calls.append(messages)
                return AIMessage(content="OK")

        runtime._configured_llm_factory = lambda config: Model()
        assert registry.test(child["id"])["status"] == "passed"
        runtime.records.save("agent", {"description": "更新能力"}, child["id"])
        assert registry.status()["items"][-1]["test"]["status"] == "stale"
        runtime.records.save("model", {"model_type": "multimodal"}, child["model_config_id"])
        assert registry.test(child["id"])["status"] == "passed"
        assert calls[-1][0].content[1]["type"] == "image_url"
        runtime.records.save("model", {"model_type": "embedding"}, child["model_config_id"])
        assert registry.snapshot() == []
        assert registry.test(child["id"])["status"] == "failed"
        with pytest.raises(ValueError, match="向量"):
            runtime.records.snapshot(
                runtime.records.setting("default_workflow_id"), "workspace-write", "never"
            )
        assert "registry-test-credential" not in json.dumps(registry.status())
    finally:
        runtime.close()


def test_registry_migrates_existing_agents(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    sql = Path("src/aurora/agent/store/migrations/001_initial.sql").read_text()
    connection.executescript(sql)
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    connection.execute("INSERT INTO schema_migrations VALUES(1,'old')")
    connection.execute(
        "INSERT INTO model_providers(id,name,base_url,credential_ref,created_at,updated_at) VALUES('p','p','https://example.org','env:KEY','a','a')"
    )
    connection.execute(
        "INSERT INTO model_configs(id,provider_id,name,model_name,"
        "input_types_json,created_at,updated_at) "
        "VALUES('m','p','m','vision','[\"text\",\"image\"]','a','a')"
    )
    connection.execute(
        "INSERT INTO agents(id,name,model_config_id,created_at,updated_at) "
        "VALUES('a','a','m','a','a')"
    )
    connection.commit()
    connection.close()
    db = Database(path)
    try:
        assert db.one("SELECT model_type FROM model_configs")["model_type"] == "multimodal"
        assert db.one("SELECT callable FROM agents")["callable"] == 0
        db.migrate()
        assert db.one("SELECT count(*) n FROM schema_migrations")["n"] == 2
    finally:
        db.close()


def test_delegate_rejection_and_close_settle_child(tmp_path, monkeypatch):
    runtime, workspace, child, _ = setup_registry(tmp_path, monkeypatch)
    try:
        session = runtime.create_session(str(workspace), approval_mode="interactive")
        result = session.start("执行专家任务")
        with pytest.raises(RuntimeError, match="失败"):
            session.resume(result.run_id, {"approved": False})
        assert not (workspace / "1.txt").exists()
        assert (
            runtime.records.db.rows(select(AgentRun).where(AgentRun.agent_id == child["id"]))[0][
                "status"
            ]
            == "failed"
        )
        result = session.start("再次执行")
        assert result.status == "waiting"
        session.close()
        rows = runtime.records.db.rows(select(AgentRun).where(AgentRun.agent_id == child["id"]))
        assert all(row["status"] in {"failed", "interrupted"} for row in rows)
        runtime.records.delete_session(session.id)
        assert not runtime.records.db.rows(select(AgentRun))
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "model_type,foreign", [("text", False), ("multimodal", True), ("multimodal", False)]
)
def test_delegate_validates_and_passes_actual_images(tmp_path, monkeypatch, model_type, foreign):
    from test_persistence import PNG

    from aurora.agent.manager.delegation import execute_delegate
    from aurora.agent.store.database import dumps, now, uid
    from aurora.agent.store.model import Run, Task

    runtime, workspace, child, _ = setup_registry(tmp_path, monkeypatch)
    try:
        runtime.records.save("model", {"model_type": model_type}, child["model_config_id"])
        session = runtime.create_session(str(workspace), approval_mode="always")
        store = runtime.records
        config = AgentRegistry(runtime).config(child["id"])
        run_id, agent_run, task_id = uid(), uid(), uid()
        store.insert(
            Run,
            id=run_id,
            session_id=session.id,
            request_key=uid(),
            goal="图片",
            status="running",
            config_snapshot_json=dumps({}),
            created_at=now(),
            updated_at=now(),
        )
        store.insert(
            AgentRun,
            id=agent_run,
            run_id=run_id,
            agent_id=child["id"],
            step_key="code",
            iteration=0,
            status="running",
            input_json="{}",
            created_at=now(),
            updated_at=now(),
        )
        store.insert(
            Task,
            id=task_id,
            agent_run_id=agent_run,
            task_key="p",
            tool="call_agent",
            args_json="{}",
            description="理解图片",
            effort="low",
            status="running",
            created_at=now(),
            updated_at=now(),
        )
        artifact = store.artifact(run_id, agent_run, "screenshot", PNG, "image/png", {})
        calls = []

        class ImageModel:
            def invoke(self, messages):
                calls.append(messages)
                return AIMessage(content="图片内容")

        class ImagePlanner:
            def set_instructions(self, value):
                assert value == child["instructions"]

            def set_images(self, images):
                assert images[0]["image_url"]["url"].endswith(
                    __import__("base64").b64encode(PNG).decode()
                )

            def plan(self, goal):
                return [
                    dict(
                        id="answer",
                        description="图片理解",
                        effort=Effort.LOW,
                        tool="agent_respond",
                        args={"prompt": "描述图片"},
                    )
                ]

        runtime._configured_llm_factory = lambda config: ImageModel()
        runtime._planner_factory = lambda model, tools: ImagePlanner()
        state = {
            "code_agent_run_id": agent_run,
            "task_ids": {"p": task_id},
            "iteration": 0,
            "snapshot": {"approval_mode": "always"},
        }
        task = {
            "id": "p",
            "args": {"agent_id": child["id"], "task": "理解图片", "artifact_ids": [artifact["id"]]},
        }
        if model_type == "text" or foreign:
            with pytest.raises(ValueError, match="图片"):
                execute_delegate(
                    session, "foreign" if foreign else run_id, state, task, [config], None
                )
            assert not calls
        else:
            result = json.loads(execute_delegate(session, run_id, state, task, [config], None))
            assert result["status"] == "completed"
            assert calls[0][0].content == child["instructions"]
            assert calls[0][1].content[1]["type"] == "image_url"
        store.update(Run, run_id, status="completed")
    finally:
        runtime.close()


def test_ndjson_status_responds_during_long_run():
    import io
    import threading

    from aurora.agent.transport.api import serve_ndjson

    started, queried = threading.Event(), threading.Event()

    class Api:
        def process_wire(self, request, emit):
            if request["method"] == "run.start":
                started.set()
                assert queried.wait(3)
            else:
                assert started.wait(3)
                queried.set()
            emit({"request_id": request["request_id"], "ok": True})

    source = io.StringIO(
        "\n".join(
            json.dumps({"protocol_version": 1, "request_id": name, "method": method})
            for name, method in [("run", "run.start"), ("status", "agent.status")]
        )
    )
    output = io.StringIO()
    serve_ndjson(Api(), source, output)
    assert queried.is_set()
    assert {json.loads(line)["request_id"] for line in output.getvalue().splitlines()} == {
        "run",
        "status",
    }


def test_child_clarification_updates_tasks_and_resumes(tmp_path, monkeypatch):
    from aurora.agent.core.planner import ClarificationDecision, NoClarifier
    from aurora.agent.store.model import Task

    runtime, workspace, child, _ = setup_registry(tmp_path, monkeypatch)
    rounds = []

    class Clarifier:
        def assess(self, goal, tasks, clarifications):
            return ClarificationDecision(not clarifications, "写入哪一种文案？", "需要文案要求")

    class ChildPlanner:
        def plan(self, goal):
            rounds.append(goal)
            return [
                dict(
                    id="same",
                    description="更新文件",
                    effort=Effort.LOW,
                    tool="write_file",
                    args={"path": "chosen.txt", "content": "new" if len(rounds) > 1 else "old"},
                )
            ]

    original_factory = runtime._planner_factory
    runtime._planner_factory = lambda model, tools: (
        original_factory(model, tools) if "call_agent" in tools else ChildPlanner()
    )
    count = []

    def clarifier_factory(model):
        count.append(model)
        return NoClarifier() if len(count) == 1 else Clarifier()

    runtime._clarifier_factory = clarifier_factory
    try:
        session = runtime.create_session(str(workspace), approval_mode="interactive")
        result = session.start("生成文案")
        assert result.status == "waiting"
        assert result.interruptions[0].value["kind"] == "clarification"
        result = session.resume(result.run_id, "使用新文案")
        assert result.status == "waiting"
        assert result.interruptions[0].value["kind"] == "approval"
        rows = runtime.records.db.rows(
            select(Task).join(Task.agent_run).where(AgentRun.agent_id == child["id"])
        )
        assert len(rows) == 1
        assert json.loads(rows[0]["args_json"])["content"] == "new"
        result = session.resume(result.run_id, {"approved": True})
        assert result.status == "completed"
        assert (workspace / "chosen.txt").read_text() == "new"
    finally:
        runtime.close()


def test_registry_protocol_and_invalid_model_types(tmp_path, monkeypatch):
    from aurora.agent.transport import RuntimeApi

    runtime, _, child, _ = setup_registry(tmp_path, monkeypatch)
    try:
        api = RuntimeApi(runtime)
        frames = []
        api.process_wire(
            {"protocol_version": 1, "request_id": "status", "method": "agent.status"}, frames.append
        )
        response = next(frame for frame in frames if frame.get("request_id") == "status")
        assert response["ok"]
        assert response["result"]["items"][-1]["id"] == child["id"]
        assert response["result"]["items"][-1]["active"] == []
        for data in ({"input_types": None}, {"input_types": [{}]}, {"model_type": []}):
            with pytest.raises(ValueError, match="类型"):
                runtime.records.save("model", data, child["model_config_id"])
    finally:
        runtime.close()


def test_websocket_status_responds_while_run_is_active():
    import asyncio
    import socket
    import threading
    from contextlib import suppress

    from websockets.asyncio.client import connect

    from aurora.agent.transport.websocket import serve_websocket

    started, queried = threading.Event(), threading.Event()

    class Api:
        def process_wire(self, request, emit):
            if request["method"] == "run.start":
                started.set()
                assert queried.wait(3)
            else:
                assert started.wait(3)
                queried.set()
            emit({"request_id": request["request_id"], "ok": True})

    async def scenario():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = asyncio.create_task(serve_websocket(Api(), port=port))
        try:
            for _ in range(50):
                try:
                    ws = await connect(f"ws://127.0.0.1:{port}/ws")
                    break
                except OSError:
                    await asyncio.sleep(0.01)
            else:
                raise AssertionError("WebSocket 服务未启动")
            async with ws:
                for name, method in [("run", "run.start"), ("status", "agent.status")]:
                    await ws.send(
                        json.dumps({"protocol_version": 1, "request_id": name, "method": method})
                    )
                responses = [json.loads(await asyncio.wait_for(ws.recv(), 4)) for _ in range(2)]
                assert {row["request_id"] for row in responses} == {"run", "status"}
                assert queried.is_set()
        finally:
            queried.set()
            server.cancel()
            with suppress(asyncio.CancelledError):
                await server

    asyncio.run(scenario())
