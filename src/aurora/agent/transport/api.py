"""面向桌面壳的运行时协议处理器。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TextIO

from aurora.text import sanitize_text, sanitize_value

from ..mcp import McpClientError, McpServerConfig
from ..runtime import AgentRuntime, RunUpdate, validate_workspace

PROTOCOL_VERSION = "1"


class MethodNotFoundError(ValueError):
    """请求使用了运行时不支持的方法。"""


class RuntimeApi:
    """把 NDJSON 协议请求分发到 AgentRuntime。"""

    def __init__(self, runtime: AgentRuntime | None = None) -> None:
        self._runtime = runtime or AgentRuntime()

    def handle(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        """处理单个请求并返回响应与后续事件。"""
        request = sanitize_value(request)
        request_id = request.get("id")
        try:
            method = _required_string(request, "method")
            params = request.get("params", {})
            if not isinstance(params, Mapping):
                raise ValueError("params 必须是对象")
            result, events = self._dispatch(method, params)
            return [{"id": request_id, "result": result}, *events]
        except MethodNotFoundError as exc:
            return [_error(request_id, "method_not_found", str(exc))]
        except McpClientError as exc:
            return [_error(request_id, "mcp_error", str(exc))]
        except (TypeError, ValueError) as exc:
            return [_error(request_id, "invalid_request", str(exc))]
        except Exception as exc:
            return [_error(request_id, "internal_error", str(exc))]

    def _dispatch(
        self,
        method: str,
        params: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """执行一个协议方法。"""
        if method == "runtime.initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": [
                    "workspace.validate",
                    "session.create",
                    "run.start",
                    "run.resume",
                    "session.close",
                    "mcp.server.connect",
                    "mcp.server.list",
                    "mcp.server.disconnect",
                    "mcp.package.catalog",
                    "mcp.package.connect",
                    "mcp.package.list",
                    "mcp.package.disconnect",
                ],
            }, []
        if method == "workspace.validate":
            return validate_workspace(_required_string(params, "path")).to_dict(), []
        if method == "mcp.server.connect":
            return self._runtime.connect_mcp_server(McpServerConfig.from_mapping(params)), []
        if method == "mcp.server.list":
            return {"servers": self._runtime.list_mcp_servers()}, []
        if method == "mcp.server.disconnect":
            name = _required_string(params, "name")
            self._runtime.disconnect_mcp_server(name)
            return {"name": name, "disconnected": True}, []
        if method == "mcp.package.catalog":
            return {
                "packages": self._runtime.catalog_mcp_packages(),
                "pluginErrors": self._runtime.mcp_package_plugin_errors(),
            }, []
        if method == "mcp.package.connect":
            package_id = _required_string(params, "packageId")
            instance_name = _optional_string(params, "instanceName") or package_id
            config = params.get("config", {})
            if not isinstance(config, Mapping):
                raise ValueError("config 必须是对象")
            return self._runtime.connect_mcp_package(
                package_id,
                instance_name,
                config,
            ), []
        if method == "mcp.package.list":
            return {"packages": self._runtime.list_connected_mcp_packages()}, []
        if method == "mcp.package.disconnect":
            instance_name = _required_string(params, "instanceName")
            self._runtime.disconnect_mcp_package(instance_name)
            return {"instanceName": instance_name, "disconnected": True}, []
        if method == "session.create":
            session = self._runtime.create_session(
                _required_string(params, "workspacePath"),
                sandbox_mode=str(params.get("sandboxMode", "workspace-write")),
                approval_mode=str(params.get("approvalMode", "interactive")),
            )
            return {
                "sessionId": session.id,
                "workspace": session.workspace.to_dict(),
                "sandboxMode": str(params.get("sandboxMode", "workspace-write")),
                "approvalMode": str(params.get("approvalMode", "interactive")),
            }, []
        if method == "run.start":
            session = self._runtime.get_session(_required_string(params, "sessionId"))
            return _frames_for_update(session.start(_required_string(params, "goal")))
        if method == "run.resume":
            session = self._runtime.get_session(_required_string(params, "sessionId"))
            update = session.resume(
                _required_string(params, "runId"),
                params.get("response"),
                _optional_string(params, "interruptId"),
            )
            return _frames_for_update(update)
        if method == "session.close":
            session_id = _required_string(params, "sessionId")
            self._runtime.close_session(session_id)
            return {"sessionId": session_id, "closed": True}, []
        raise MethodNotFoundError(f"未知方法: {method}")

    def close(self) -> None:
        """关闭运行时持有的外部资源。"""
        self._runtime.close()


def serve_ndjson(api: RuntimeApi, input_stream: TextIO, output_stream: TextIO) -> None:
    """持续读取 NDJSON 请求并写出协议帧。"""
    for line in input_stream:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise ValueError("请求必须是 JSON 对象")
            frames = api.handle(request)
        except (json.JSONDecodeError, ValueError) as exc:
            frames = [_error(None, "invalid_json", str(exc))]
        for frame in frames:
            output_stream.write(
                json.dumps(sanitize_value(frame), ensure_ascii=False, separators=(",", ":")) + "\n"
            )
        output_stream.flush()


def _frames_for_update(update: RunUpdate) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """把运行快照转换为响应结果和广播事件。"""
    result = update.to_dict()
    if update.status == "completed":
        return result, [{"event": "run.completed", "data": result}]
    events: list[dict[str, Any]] = []
    names = {
        "approval": "approval.required",
        "clarification": "clarification.required",
        "evaluation": "evaluation.required",
    }
    for pending in update.interruptions:
        data = {
            "sessionId": update.session_id,
            "runId": update.run_id,
            **pending.to_dict(),
        }
        events.append({"event": names.get(str(pending.value.get("kind")), "run.input_required"), "data": data})
    return result, events


def _required_string(values: Mapping[str, Any], name: str) -> str:
    """读取必填非空字符串。"""
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return sanitize_text(value).strip()


def _optional_string(values: Mapping[str, Any], name: str) -> str | None:
    """读取可选字符串。"""
    value = values.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _error(request_id: Any, code: str, message: str) -> dict[str, Any]:
    """构造稳定的协议错误帧。"""
    return {"id": request_id, "error": {"code": code, "message": message}}
