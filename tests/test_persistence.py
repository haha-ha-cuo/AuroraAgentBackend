"""真实 SQLite 上的会话、配置和多 Agent 协作验收。"""

from __future__ import annotations

import base64
import json
import sqlite3

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.exc import IntegrityError

from aurora.agent.core import Effort, NoClarifier
from aurora.agent.manager.runtime import AgentRuntime
from aurora.agent.manager.workflows import Finding, ReviewResult
from aurora.agent.sandbox import Sandbox, UnsafeSubprocessExecutor
from aurora.agent.store.database import Database
from aurora.agent.transport import RuntimeApi

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
)


class Capture:
    """提供可追溯的测试截图并统计轮次。"""

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def capture(self, config, sandbox):
        self.calls += 1
        if self.fail:
            raise ValueError("截图失败")
        return [(PNG, {"url": config["url"], "viewport": {"width": 1440, "height": 900}})], "ready"


class Model:
    """记录模型收到的真实消息和审查证据。"""

    def __init__(self, responses, calls):
        self.responses = responses
        self.calls = calls
        self.review = False

    def with_structured_output(self, schema, **kwargs):
        self.review = True
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        if not self.review:
            return AIMessage(
                content="已完成",
                usage_metadata={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
            )
        assert "JSON Schema" in messages[0].content
        assert "artifact_id" in messages[0].content
        content = messages[-1].content
        image = next(item["image_url"]["url"] for item in content if item["type"] == "image_url")
        assert base64.b64decode(image.split(",")[1]) == PNG
        artifact_id = next(
            item["text"].split("\n")[0].split("：")[1]
            for item in content
            if item["type"] == "text" and item["text"].startswith("截图 ID：")
        )
        verdict = self.responses.pop(0)
        return ReviewResult(
            verdict=verdict,
            summary="视觉检查结果",
            findings=[
                Finding(
                    severity="blocking",
                    description="按钮比例失衡",
                    artifact_id=artifact_id,
                    location="页面右上角",
                    suggestion="缩小按钮宽度",
                )
            ]
            if verdict == "changes_requested"
            else [],
        )


class Planner:
    """记录输入并执行可观察的工作区修改。"""

    def __init__(self, goals):
        self.goals = goals

    def plan(self, goal):
        self.goals.append(goal)
        return [
            {
                "id": "same-model-task-id",
                "description": "修改页面",
                "tool": "write_file",
                "args": {"path": "index.html", "content": f"<h1>version {len(self.goals)}</h1>"},
                "effort": Effort.LOW,
            }
        ]


def runtime_at(tmp_path, responses=None, capture=None):
    """创建使用真实文件数据库的可控运行时。"""
    goals, calls = [], []
    runtime = AgentRuntime(
        database=Database(tmp_path / "data" / "aurora.db"),
        configured_llm_factory=lambda config: Model(
            responses if responses is not None else [], calls
        ),
        planner_factory=lambda llm, tools: Planner(goals),
        clarifier_factory=lambda llm: NoClarifier(),
        sandbox_factory=lambda root, mode: Sandbox(
            root, mode=mode, executor=UnsafeSubprocessExecutor()
        ),
        capture=capture or Capture(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return runtime, workspace, goals, calls


def configure_visual(runtime):
    """配置独立的视觉模型和审查角色。"""
    store = runtime.records
    original = store.list_configs("agent")[0]
    model = store.save(
        "model",
        {
            "name": "视觉模型",
            "provider_id": store.list_configs("provider")[0]["id"],
            "model_name": "vision",
            "input_types": ["text", "image"],
        },
    )
    reviewer = store.save(
        "agent",
        {
            "name": "视觉审查",
            "model_config_id": model["id"],
            "instructions": "检查视觉布局",
            "allowed_tools": [],
        },
    )
    flow = store.save(
        "workflow",
        {
            "name": "前端协作",
            "kind": "frontend-review",
            "code_agent_id": original["id"],
            "review_agent_id": reviewer["id"],
        },
    )
    return flow


def visual_session(runtime, workspace):
    """创建具备预览配置的视觉会话。"""
    flow = configure_visual(runtime)
    project = runtime.records.project(workspace)
    runtime.records.save("project", {"preview": {"url": "http://127.0.0.1:3000"}}, project["id"])
    return runtime.create_session(str(workspace), workflow_id=flow["id"], approval_mode="always")


@pytest.mark.parametrize(
    "verdicts,expected",
    [(["passed"], 1), (["changes_requested", "passed"], 2), (["changes_requested"] * 3, 3)],
)
def test_visual_review_loop_records_independent_agents_and_evidence(tmp_path, verdicts, expected):
    runtime, workspace, goals, calls = runtime_at(tmp_path, list(verdicts))
    session = visual_session(runtime, workspace)
    update = session.start("设计一个前端页面", request_key="request-one")
    assert len(goals) == expected
    assert runtime.capture.calls == expected
    detail = RuntimeApi(runtime)._persistence.dispatch("run.get", {"runId": update.run_id})
    assert len(detail["reviews"]) == expected
    assert len({item["artifactIds"][0] for item in detail["reviews"]}) == expected
    agent_ids = {item["agentId"] for item in detail["agentRuns"]}
    assert len(agent_ids) == 2
    assert len(runtime.records.db.all("SELECT id FROM tasks")) == expected
    assert session.start("重复请求", request_key="request-one").run_id == update.run_id
    assert len(goals) == expected
    if expected == 3:
        assert update.status == "waiting"
        update = session.resume(update.run_id, {"action": "accept"}, update.interruptions[0].id)
        assert "尚未通过" in update.state["report"]
    assert update.status == "completed"
    runtime.close()


def test_restart_preserves_history_and_prevents_old_approval_replay(tmp_path):
    runtime, workspace, goals, calls = runtime_at(tmp_path)
    session = runtime.create_session(str(workspace), approval_mode="interactive")
    first = session.start("用户偏好：蓝色", mode="say")
    paused = session.start("写页面")
    assert paused.status == "waiting"
    runtime.records.close()
    session.repository.close()
    restarted, _, later_goals, _ = runtime_at(tmp_path)
    saved = restarted.records.get("runs", paused.run_id)
    assert saved["status"] == "interrupted"
    restored = restarted.get_session(session.id)
    with pytest.raises(ValueError, match="旧审批"):
        restored.resume(paused.run_id, {"approved": True}, paused.interruptions[0].id)
    restored.record["approval_mode"] = "always"
    restarted.records.update("sessions", session.id, approval_mode="always")
    restored.start("按我的偏好调整页面")
    assert "用户偏好：蓝色" in later_goals[-1]
    assert restarted.records.get("runs", first.run_id)["status"] == "completed"
    restarted.records.clear_context(session.id)
    restored.start("新的需求")
    assert "用户偏好：蓝色" not in later_goals[-1]
    assert restarted.records.db.one("SELECT COUNT(*) AS n FROM messages")["n"] > 4
    restarted.close()


def test_failed_capture_is_not_passed_and_does_not_repeat_code(tmp_path):
    capture = Capture(fail=True)
    runtime, workspace, goals, _ = runtime_at(tmp_path, ["passed"], capture)
    session = visual_session(runtime, workspace)
    update = session.start("设计页面")
    assert update.status == "waiting"
    assert runtime.records.db.one("SELECT verdict FROM reviews")["verdict"] == "unable_to_review"
    assert len(goals) == 1
    capture.fail = False
    completed = session.resume(update.run_id, {"action": "retry"}, update.interruptions[0].id)
    assert completed.status == "completed"
    assert len(goals) == 1
    assert runtime.capture.calls == 2
    runtime.close()


def test_configuration_snapshot_does_not_change_during_approval(tmp_path):
    runtime, workspace, _, _ = runtime_at(tmp_path)
    session = runtime.create_session(str(workspace))
    paused = session.start("创建文件")
    snapshot = runtime.records.get("runs", paused.run_id)["config_snapshot"]
    agent = runtime.records.list_configs("agent")[0]
    runtime.records.save("agent", {"instructions": "改用新指令"}, agent["id"])
    assert runtime.records.get("runs", paused.run_id)["config_snapshot"] == snapshot
    with pytest.raises(ValueError, match="活动运行"):
        session.start("第二个请求")
    with pytest.raises(ValueError, match="活动运行"):
        runtime.records.delete_session(session.id)
    session.resume(paused.run_id, {"approved": True}, paused.interruptions[0].id)
    with pytest.raises(ValueError):
        session.resume(paused.run_id, {"approved": True}, paused.interruptions[0].id)
    runtime.close()


def test_sqlite_migrations_foreign_keys_and_pagination(tmp_path):
    runtime, workspace, _, _ = runtime_at(tmp_path)
    store = runtime.records
    store.db.migrate()
    assert store.db.one("SELECT COUNT(*) AS n FROM schema_migrations")["n"] == 2
    assert store.db.one("PRAGMA foreign_keys")["foreign_keys"] == 1
    assert store.db.one("PRAGMA journal_mode")["journal_mode"] == "wal"
    with pytest.raises(IntegrityError):
        store.save("agent", {"name": "broken", "model_config_id": "missing"})
    session = runtime.create_session(str(workspace))
    for index in range(55):
        store.message(session.id, "user", f"message {index}")
    api = RuntimeApi(runtime)._persistence
    recent = api.messages({"sessionId": session.id})
    older = api.messages({"sessionId": session.id, "beforeSeq": recent["nextBeforeSeq"]})
    assert len(recent["messages"]) == 50
    assert len(older["messages"]) == 5
    assert not {row["id"] for row in older["messages"]} & {row["id"] for row in recent["messages"]}
    store.clear_context(session.id)
    assert store.context(session.id, 8000, "new") == []
    runtime.close()


def test_secrets_and_invalid_image_capability_are_not_silently_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "private-credential-test")
    runtime, workspace, _, _ = runtime_at(tmp_path)
    with pytest.raises(ValueError, match="敏感字段"):
        runtime.records.save("model", {"name": "bad", "parameters": {"api_key": "secret"}})
    session = visual_session(runtime, workspace)
    model = runtime.records.list_configs("model")[-1]
    runtime.records.save("model", {"input_types": ["text"]}, model["id"])
    result = session.start("页面")
    assert result.status == "waiting"
    assert runtime.records.db.one("SELECT verdict FROM reviews")["verdict"] == "unable_to_review"
    assert "private-credential-test" not in json.dumps(runtime.records.get("runs", result.run_id))
    runtime.close()


def test_two_runtimes_cannot_recover_each_others_database(tmp_path):
    runtime, workspace, _, _ = runtime_at(tmp_path)
    session = runtime.create_session(str(workspace))
    waiting = session.start("需要审批")
    with pytest.raises(ValueError, match="另一个"):
        runtime_at(tmp_path)
    assert runtime.records.get("runs", waiting.run_id)["status"] == "waiting"
    runtime.close()


def test_persistence_protocol_roundtrip_and_deletion(tmp_path):
    runtime, workspace, _, _ = runtime_at(tmp_path)
    api = RuntimeApi(runtime)

    def request(method, params=None):
        response = api.handle({"id": "test", "method": method, "params": params or {}})[0]
        assert "error" not in response, response
        return response["result"]

    created = request("session.create", {"workspacePath": str(workspace), "title": "会话一"})
    session_id = created["sessionId"]
    assert request("session.list")["sessions"][0]["title"] == "会话一"
    request("session.update", {"sessionId": session_id, "title": "新标题"})
    request("session.close", {"sessionId": session_id})
    assert request("session.get", {"sessionId": session_id})["title"] == "新标题"
    request("run.start", {"sessionId": session_id, "goal": "你好", "mode": "say"})
    detail = request("session.get", {"sessionId": session_id})
    assert any(message["content"] == "你好" for message in detail["messages"])
    request("session.clear", {"sessionId": session_id})
    assert request("session.get", {"sessionId": session_id})["messages"]
    request("session.delete", {"sessionId": session_id})
    assert request("session.list")["sessions"] == []
    request("project.delete", {"id": created["projectId"]})
    assert request("project.list")["items"] == []
    runtime.close()


def test_failed_migration_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    db = Database(tmp_path / "migration.db")
    migration = tmp_path / "003_broken.sql"
    migration.write_text("CREATE TABLE partial(id TEXT); INSERT INTO nonexistent VALUES (1);")
    original_glob = __import__("pathlib").Path.glob
    monkeypatch.setattr(
        "pathlib.Path.glob",
        lambda path, pattern: (
            [migration] if path.name == "migrations" else original_glob(path, pattern)
        ),
    )
    with pytest.raises(sqlite3.OperationalError):
        db.migrate()
    assert db.one("SELECT name FROM sqlite_master WHERE name='partial'") is None
    assert db.one("SELECT version FROM schema_migrations WHERE version=3") is None
    db.close()


def test_progress_is_emitted_only_after_commit(tmp_path):
    runtime, workspace, _, _ = runtime_at(tmp_path, ["passed"])
    session = visual_session(runtime, workspace)
    events = []

    def progress(run_id, kind, payload):
        assert not runtime.records.db.connection.in_transaction
        if kind == "review.completed":
            review = runtime.records.get("reviews", payload["reviewId"])
            assert (
                runtime.records.get("agent_runs", review["agent_run_id"])["status"] == "completed"
            )
        events.append(kind)

    session.start("构建页面", progress)
    assert "review.completed" in events
    runtime.close()


def test_review_commit_failure_does_not_leave_passed_review(tmp_path, monkeypatch):
    runtime, workspace, _, _ = runtime_at(tmp_path, ["passed"])
    session = visual_session(runtime, workspace)
    original = runtime.records.message
    failed = False

    def fail_review_message(*args, **kwargs):
        nonlocal failed
        agent_run_id = kwargs.get("agent_run_id")
        if (
            agent_run_id
            and not failed
            and runtime.records.get("agent_runs", agent_run_id)["step_key"] == "review"
        ):
            failed = True
            raise sqlite3.OperationalError("模拟审查结果提交失败")
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.records, "message", fail_review_message)
    result = session.start("构建页面")
    assert result.status == "waiting"
    reviews = runtime.records.db.all("SELECT verdict FROM reviews")
    assert reviews == [{"verdict": "unable_to_review"}]
    runtime.close()


def test_close_during_execution_keeps_session_registered(tmp_path):
    runtime, workspace, _, _ = runtime_at(tmp_path)
    session = runtime.create_session(str(workspace))
    session._lock.acquire()
    try:
        with pytest.raises(ValueError, match="正在执行"):
            runtime.close_session(session.id)
        assert runtime.get_session(session.id) is session
    finally:
        session._lock.release()
        runtime.close()


def test_inflight_duplicate_request_returns_existing_run(tmp_path):
    runtime, workspace, goals, _ = runtime_at(tmp_path)
    session = runtime.create_session(str(workspace), approval_mode="always")
    duplicates = []

    def progress(run_id, kind, payload):
        if kind == "run.started":
            duplicates.append(session.start("重发", request_key="same-request"))

    result = session.start("写页面", progress, request_key="same-request")
    assert duplicates[0].run_id == result.run_id
    assert duplicates[0].status == "running"
    assert len(goals) == 1
    runtime.close()
