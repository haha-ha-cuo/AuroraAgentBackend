"""提供全局 Agent 发现、配置检查与连接测试。"""

from __future__ import annotations

import hashlib
import os

from langchain_core.messages import HumanMessage
from sqlalchemy import func, select

from ..model_access.config import build_configured_llm
from ..store.database import dumps, now
from ..store.model import Agent, AgentRun, ModelConfig, ModelProvider, Run
from ..store.records import decode
from ..tools.base import Tool

TEST_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAD0lEQVR4nGP4jwMwDC0JALoev0Ewkwr8AAAAAElFTkSuQmCC"
)


class AgentRegistry:
    """复用配置存储并聚合 Agent 可用性。"""

    def __init__(self, runtime):
        self.runtime = runtime
        self.records = runtime.records

    def config(self, agent_id):
        """读取完整关联配置。"""
        agent = self.records.get(Agent, agent_id)
        model = self.records.get(ModelConfig, agent["model_config_id"])
        provider = self.records.get(ModelProvider, model["provider_id"])
        return {"agent": agent, "model": model, "provider": provider}

    def fingerprint(self, config):
        """计算不包含测试结果的配置指纹。"""
        value = dict(
            config,
            agent={
                key: value
                for key, value in config["agent"].items()
                if key not in {"test_result", "updated_at"}
            },
        )
        return hashlib.sha256(dumps(value).encode()).hexdigest()

    def issues(self, config):
        """只检查本地配置，不向供应商发请求。"""
        issues = []
        for key, label in (("agent", "Agent"), ("model", "模型"), ("provider", "供应商")):
            if not config[key]["enabled"]:
                issues.append(label + "已停用")
        if config["model"]["model_type"] == "embedding":
            issues.append("向量模型暂不支持调用")
        if config["model"]["model_name"] == "unconfigured":
            issues.append("模型名称未配置")
        ref = config["provider"]["credential_ref"]
        try:
            if ref.startswith("env:"):
                secret = os.getenv(ref[4:])
            elif ref.startswith("keyring:"):
                import keyring

                secret = keyring.get_password("Aurora", ref[8:])
            else:
                secret = None
            if not secret:
                issues.append("供应商凭据未配置")
        except Exception:
            issues.append("无法读取供应商凭据")
        return issues

    def snapshot(self):
        """冻结允许委派且配置可用的 Agent。"""
        configs = [self.config(agent["id"]) for agent in self.records.list_configs("agent")]
        return [
            config for config in configs if config["agent"]["callable"] and not self.issues(config)
        ]

    def status(self):
        """聚合持久化执行状态与最近连接测试。"""
        items = []
        for agent in self.records.list_configs("agent"):
            config = self.config(agent["id"])
            query = (
                select(
                    AgentRun.id,
                    AgentRun.run_id,
                    AgentRun.status,
                    AgentRun.input_json,
                    AgentRun.output,
                    AgentRun.error,
                    AgentRun.updated_at,
                    Run.session_id,
                    Run.goal,
                )
                .join(AgentRun.run)
                .where(AgentRun.agent_id == agent["id"])
                .order_by(AgentRun.created_at.desc())
            )
            latest = self.records.db.first(query)
            active = self.records.db.rows(query.where(AgentRun.status.in_(("running", "waiting"))))
            counts = {
                row["status"]: row["count"]
                for row in self.records.db.rows(
                    select(AgentRun.status, func.count().label("count"))
                    .where(
                        AgentRun.agent_id == agent["id"],
                        AgentRun.status.in_(("running", "waiting")),
                    )
                    .group_by(AgentRun.status)
                )
            }
            test = dict(agent["test_result"])
            if test and test.get("fingerprint") != self.fingerprint(config):
                test["status"] = "stale"
            items.append(
                {
                    "id": agent["id"],
                    "modelType": config["model"]["model_type"],
                    "modelName": config["model"]["model_name"],
                    "issues": self.issues(config),
                    "test": test,
                    "runningCount": counts.get("running", 0),
                    "waitingCount": counts.get("waiting", 0),
                    "active": [_execution(row) for row in active],
                    "latest": _execution(latest) if latest else None,
                }
            )
        return {"items": items}

    def test(self, agent_id):
        """执行最小文本或图片请求并保存版本化测试结果。"""
        config = self.config(agent_id)
        result = {"fingerprint": self.fingerprint(config), "testedAt": now(), "status": "passed"}
        try:
            if issues := self.issues(config):
                raise ValueError("；".join(issues))
            factory = self.runtime._configured_llm_factory or build_configured_llm
            model = factory(config)
            content: list[str | dict] = [{"type": "text", "text": "Reply OK."}]
            if config["model"]["model_type"] == "multimodal":
                content.append({"type": "image_url", "image_url": {"url": TEST_IMAGE}})
            model.invoke([HumanMessage(content=content)])
        except Exception as exc:
            result |= {"status": "failed", "error": self.runtime.redact(str(exc))}
        self.records.update(Agent, agent_id, test_result_json=dumps(result))
        return result


def _execution(row):
    """提供状态页所需的执行摘要与任务链接。"""
    value = decode(row)
    result = {key: value[key] for key in ("id", "run_id", "session_id", "status", "updated_at")}
    return result | {
        "task": value["input"].get("task") or value["input"].get("current") or value["goal"],
        "output": value["output"][:2000],
        "error": value["error"][:2000],
    }


def catalog(configs):
    """提取规划器可见的能力清单。"""
    return [
        {
            "id": c["agent"]["id"],
            "name": c["agent"]["name"],
            "description": c["agent"]["description"],
            "model_type": c["model"]["model_type"],
            "input_types": c["model"]["input_types"],
        }
        for c in configs
    ]


def registry_tools(configs):
    """构造当前运行专属的发现与委派工具。"""

    def list_agents() -> str:
        """列出本轮可调用的 Agent。"""
        return dumps(catalog(configs))

    def call_agent(agent_id: str, task: str, artifact_ids: list[str] | None = None) -> str:
        """由执行图接管子 Agent 委派。"""
        raise ValueError("Agent 委派必须在运行执行图中调用")

    call = Tool("call_agent", "使用注册 Agent 完成子任务；图片使用当前运行的产物 ID", call_agent)
    call.params_schema["properties"]["artifact_ids"] = {
        "type": "array",
        "items": {"type": "string"},
        "default": [],
    }
    return {
        "list_agents": Tool("list_agents", "本轮可调用 Agent：" + list_agents(), list_agents),
        "call_agent": call,
    }
