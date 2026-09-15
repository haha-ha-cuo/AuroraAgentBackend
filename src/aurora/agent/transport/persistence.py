"""数据库管理、历史查询与产物传输协议。"""

from __future__ import annotations

import base64
import json
import re

from sqlalchemy import delete, select

from aurora.agent.store.model import ModelProvider

from ..store.database import now
from ..store.model import (
    AgentRun,
    Artifact,
    ConversationSession,
    Interaction,
    LegacyImport,
    Message,
    Project,
    Review,
    ReviewArtifact,
    ReviewFinding,
    Run,
    RunEvent,
    Task,
)
from ..store.records import TABLES, decode


def camel(key):
    """转换数据库字段为协议命名。"""
    return re.sub(r"_([a-z])", lambda match: match[1].upper(), key)


def wire(row):
    """转换实体外层字段，保留任意 JSON 内部字段。"""
    return {camel(key): value for key, value in row.items()}


def message_wire(row):
    """映射历史消息并保留 Agent 归属。"""
    return wire(row) | {"content": row["content_text"], "attachments": []}


class PersistenceApi:
    """提供与运行执行解耦的数据库接口。"""

    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.records

    @property
    def capabilities(self):
        """返回所有新增接口。"""
        return [
            f"{kind}.{action}" for kind in TABLES for action in ("list", "create", "update")
        ] + [
            "project.delete",
            "project.import",
            "session.list",
            "session.get",
            "session.update",
            "session.delete",
            "session.clear",
            "message.list",
            "run.get",
            "agentRun.get",
            "agent.status",
            "agent.test",
            "review.get",
            "artifact.get",
            "settings.get",
            "settings.update",
            "provider.credential.set",
            "provider.credential.delete",
        ]

    def session(self, row, details=False):
        """读取会话摘要，按需加载最新一页消息与执行摘要。"""
        result = wire(row)
        latest = self.store.db.first(
            select(Run.status)
            .where(Run.session_id == row["id"])
            .order_by(Run.created_at.desc())
            .limit(1)
        )
        result["status"] = latest["status"] if latest else "idle"
        result |= {"messages": [], "runs": [], "tasks": [], "approvals": []}
        if details:
            page = self.messages({"sessionId": row["id"]})
            result["messages"] = page["messages"]
            result["nextBeforeSeq"] = page["nextBeforeSeq"]
            runs = self.store.db.rows(
                select(Run).where(Run.session_id == row["id"]).order_by(Run.created_at)
            )
            result["runs"] = [wire(decode(run)) | {"objective": run["goal"]} for run in runs]
            result["tasks"] = [
                wire(decode(task)) | {"sessionId": row["id"], "parentId": task["parent_task_id"]}
                for task in self.store.db.rows(
                    select(Task, AgentRun.run_id, AgentRun.parent_task_id)
                    .join(Task.agent_run)
                    .join(AgentRun.run)
                    .where(Run.session_id == row["id"])
                    .order_by(Task.created_at)
                )
            ]
            result["approvals"] = [
                wire(decode(item)) | {"sessionId": row["id"], **json.loads(item["request_json"])}
                for item in self.store.db.rows(
                    select(Interaction).join(Interaction.run).where(Run.session_id == row["id"])
                )
            ]
        return result

    def messages(self, params):
        """按消息序号向前分页，结果保持正序。"""
        session_id = params["sessionId"]
        self.store.get(ConversationSession, session_id)
        limit = params.get("limit", 50)
        before = params.get("beforeSeq", 2**63 - 1)
        if not isinstance(limit, int) or not 1 <= limit <= 200 or not isinstance(before, int):
            raise ValueError("分页参数无效")
        rows = self.store.db.rows(
            select(Message)
            .where(Message.session_id == session_id, Message.seq < before)
            .order_by(Message.seq.desc())
            .limit(limit + 1)
        )
        page = list(reversed(rows[:limit]))
        return {
            "messages": [message_wire(decode(row)) for row in page],
            "nextBeforeSeq": page[0]["seq"] if len(rows) > limit else None,
        }

    def dispatch(self, method, params):
        """执行已声明的持久化协议方法。"""
        if method not in self.capabilities:
            raise ValueError("未知持久化接口")
        if method in {"agent.status", "agent.test"}:
            from ..manager import AgentRegistry

            registry = AgentRegistry(self.runtime)
            return registry.status() if method == "agent.status" else registry.test(params["id"])
        kind, action = method.split(".", 1)
        if kind in TABLES and action in {"list", "create", "update"}:
            if action == "list":
                return {"items": [wire(row) for row in self.store.list_configs(kind)]}
            data = dict(params.get("data", {}))
            if not data:
                raise ValueError("data 必须为非空配置对象")
            result = self.store.save(kind, data, params["id"] if action == "update" else None)
            return wire(result)
        if method == "project.delete":
            with self.store.db.transaction() as connection:
                if self.store.db.first(
                    select(ConversationSession.id).where(
                        ConversationSession.project_id == params["id"]
                    )
                ):
                    raise ValueError("请先删除项目中的会话")
                connection.execute(delete(Project).where(Project.id == params["id"]))
            return {"deleted": True}
        if method == "project.import":
            with self.store.db.transaction():
                key = params.get("sourceKey", "aurora:workspaces:v1")
                if self.store.db.first(
                    select(LegacyImport.source_key).where(LegacyImport.source_key == key)
                ):
                    return {"imported": False}
                for row in params.get("projects", []):
                    self.store.project(row["path"])
                self.store.insert(LegacyImport, source_key=key, imported_at=now())
            return {"imported": True}
        if method == "session.list":
            rows = self.store.db.rows(
                select(ConversationSession).order_by(ConversationSession.updated_at.desc())
            )
            return {"sessions": [self.session(row) for row in rows]}
        if method == "session.get":
            return self.session(self.store.get(ConversationSession, params["sessionId"]), True)
        if method == "session.update":
            session_id = params["sessionId"]
            with self.store.db.transaction():
                self.store.require_idle(session_id)
                values = {}
                if "title" in params:
                    if not isinstance(params["title"], str) or not params["title"].strip():
                        raise ValueError("标题不能为空")
                    values["title"] = params["title"].strip()
                if "workflowId" in params:
                    self.store.snapshot(params["workflowId"], "workspace-write", "interactive")
                    values["workflow_id"] = params["workflowId"]
                if "archived" in params:
                    values["archived_at"] = now() if params["archived"] else None
                self.store.update(ConversationSession, session_id, **values, updated_at=now())
            return self.session(self.store.get(ConversationSession, session_id))
        if method == "session.delete":
            self.store.require_idle(params["sessionId"])
            self.runtime.close_session(params["sessionId"])
            self.store.delete_session(params["sessionId"])
            return {"deleted": True}
        if method == "session.clear":
            self.store.clear_context(params["sessionId"])
            return {"cleared": True}
        if method == "message.list":
            return self.messages(params)
        if method == "run.get":
            run = self.store.get(Run, params["runId"])
            result = wire(run)
            result["agentRuns"] = [
                wire(decode(row))
                for row in self.store.db.rows(
                    select(AgentRun)
                    .where(AgentRun.run_id == run["id"])
                    .order_by(AgentRun.created_at)
                )
            ]
            configs = {
                step["agent_id"]: step["config"]
                for step in run["config_snapshot"]["steps"]
                if step.get("agent_id")
            }
            for execution in result["agentRuns"]:
                config = execution.get("configSnapshot") or configs.get(execution["agentId"], {})
                execution["agentName"] = config.get("agent", {}).get("name", "")
                execution["modelName"] = config.get("model", {}).get("model_name", "")
            result["artifacts"] = [
                wire(decode(row))
                for row in self.store.db.rows(
                    select(Artifact)
                    .where(Artifact.run_id == run["id"])
                    .order_by(Artifact.created_at)
                )
            ]
            result["reviews"] = [
                self.review(row["id"])
                for row in self.store.db.rows(
                    select(Review.id)
                    .join(Review.agent_run)
                    .where(AgentRun.run_id == run["id"])
                    .order_by(Review.created_at)
                )
            ]
            result["events"] = [
                wire(decode(row))
                for row in self.store.db.rows(
                    select(RunEvent).where(RunEvent.run_id == run["id"]).order_by(RunEvent.seq)
                )
            ]
            return result
        if method == "agentRun.get":
            row = self.store.get(AgentRun, params["agentRunId"])
            return wire(row) | {
                "tasks": [
                    wire(decode(task))
                    for task in self.store.db.rows(
                        select(Task).where(Task.agent_run_id == row["id"]).order_by(Task.created_at)
                    )
                ]
            }
        if method == "review.get":
            return self.review(params["reviewId"])
        if method == "artifact.get":
            row, content = self.store.artifact_bytes(params["artifactId"])
            return wire(row) | {"base64": base64.b64encode(content).decode()}
        if method == "settings.get":
            return {"defaultWorkflowId": self.store.setting("default_workflow_id")}
        if method == "settings.update":
            self.store.snapshot(params["defaultWorkflowId"], "workspace-write", "interactive")
            self.store.set_setting("default_workflow_id", params["defaultWorkflowId"])
            return self.dispatch("settings.get", {})
        if method.startswith("provider.credential."):
            import keyring
            from keyring.errors import PasswordDeleteError

            provider = self.store.get(ModelProvider, params["providerId"])
            if action == "credential.set":
                secret = params.get("secret")
                if not isinstance(secret, str) or not secret:
                    raise ValueError("凭据不能为空")
                keyring.set_password("Aurora", provider["id"], secret)
                self.store.update(
                    ModelProvider,
                    provider["id"],
                    credential_ref="keyring:" + provider["id"],
                    updated_at=now(),
                )
            else:
                try:
                    keyring.delete_password("Aurora", provider["id"])
                except PasswordDeleteError:
                    pass
                self.store.update(ModelProvider, provider["id"], updated_at=now())
            return {"configured": action == "credential.set"}
        raise ValueError("未实现的协议方法")

    def review(self, review_id):
        """读取一次审查及其证据和问题。"""
        row = self.store.get(Review, review_id)
        return wire(row) | {
            "findings": [
                wire(decode(finding))
                for finding in self.store.db.rows(
                    select(ReviewFinding).where(ReviewFinding.review_id == review_id)
                )
            ],
            "artifactIds": [
                item["artifact_id"]
                for item in self.store.db.rows(
                    select(ReviewArtifact.artifact_id).where(ReviewArtifact.review_id == review_id)
                )
            ],
        }
