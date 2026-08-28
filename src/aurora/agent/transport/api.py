"""面向桌面壳的运行时协议处理器。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TextIO

from aurora.text import sanitize_text, sanitize_value

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
                ],
            }, []
        if method == "workspace.validate":
            return validate_workspace(_required_string(params, "path")).to_dict(), []
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
