"""面向桌面壳的运行时协议处理器。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, TextIO
from uuid import uuid4

from aurora.protocol import PROTOCOL_VERSION
from aurora.text import sanitize_text, sanitize_value

from ..mcp import McpClientError, McpServerConfig
from ..runtime import AgentRuntime, RunUpdate, validate_workspace
from ..sandbox import SandboxMode


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

    def handle_wire(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        """处理桌面端协议信封并返回响应与事件。"""
        request_id = request.get("request_id")
        if request.get("protocol_version") != PROTOCOL_VERSION:
            return [_wire_error(request_id, "invalid_request", "仅支持 protocol_version=1")]
        frames = self.handle(
            {
                "id": request_id,
                "method": request.get("method"),
                "params": request.get("params", {}),
            }
        )
        result: list[dict[str, Any]] = []
        for frame in frames:
            if "event" in frame:
                result.append(_wire_event(str(frame["event"]), frame.get("data", {})))
            elif "error" in frame:
                result.append(
                    _wire_error(
                        request_id,
                        str(frame["error"].get("code", "internal_error")),
                        str(frame["error"].get("message", "运行时请求失败")),
                    )
                )
            else:
                result.append(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": request_id,
                        "ok": True,
                        "result": frame.get("result", {}),
                    }
                )
        return result

    def process_wire(
        self,
        request: Mapping[str, Any],
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        """处理桌面端请求并实时发送协议帧。"""
        request = sanitize_value(request)
        request_id = request.get("request_id")
        if request.get("protocol_version") != PROTOCOL_VERSION:
            emit(_wire_error(request_id, "invalid_request", "仅支持 protocol_version=1"))
            return
        try:
            method = _required_string(request, "method")
            params = request.get("params", {})
            if not isinstance(params, Mapping):
                raise ValueError("params 必须是对象")
            if method not in {"run.start", "run.resume"}:
                for frame in self.handle_wire(request):
                    emit(frame)
                return
            result, events = self._dispatch_stream(method, params, emit)
            emit(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "ok": True,
                    "result": result,
                }
            )
            for event in events:
                emit(_wire_event(str(event["event"]), event.get("data", {})))
        except MethodNotFoundError as exc:
            emit(_wire_error(request_id, "method_not_found", str(exc)))
        except McpClientError as exc:
            emit(_wire_error(request_id, "mcp_error", str(exc)))
        except (TypeError, ValueError) as exc:
            emit(_wire_error(request_id, "invalid_request", str(exc)))
        except Exception as exc:
            emit(_wire_error(request_id, "internal_error", str(exc)))

    def _dispatch_stream(
        self,
        method: str,
        params: Mapping[str, Any],
        emit: Callable[[dict[str, Any]], None],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """执行运行方法并实时转换图节点事件。"""
        session_id = _required_string(params, "sessionId")
        session = self._runtime.get_session(session_id)
        tasks: dict[str, Mapping[str, Any]] = {}
        started: set[str] = set()
        message_id: str | None = None

        def progress(run_id: str, node: str, update: Mapping[str, Any]) -> None:
            nonlocal message_id
            base = {"sessionId": session_id, "runId": run_id}

            def emit_task_starts() -> None:
                for task_id, task in tasks.items():
                    if task_id not in started:
                        started.add(task_id)
                        emit(_wire_event("task.started", {**base, "taskId": task_id, "task": task}))

            if node in {"run.started", "run.resumed"}:
                emit(_wire_event(node, {**base, **dict(update)}))
                return
            if node == "plan":
                planned = [dict(item) for item in update.get("tasks", [])]
                tasks.clear()
                started.clear()
                tasks.update({str(item.get("id")): item for item in planned})
                emit(_wire_event("plan.created", {**base, "tasks": planned}))
                return
            if node == "clarify" and not update.get("clarification_needed", False):
                emit_task_starts()
                return
            if node == "dispatch":
                emit_task_starts()
                return
            if node == "execute":
                for result in update.get("results", []):
                    task_id = str(result.get("task_id", ""))
                    task = dict(tasks.get(task_id, {}))
                    ok = bool(result.get("ok"))
                    event = "task.completed" if ok else "task.failed"
                    emit(
                        _wire_event(
                            event,
                            {
                                **base,
                                "taskId": task_id,
                                "task": {**task, "status": "completed" if ok else "failed"},
                                "output": result.get("output", ""),
                                "error": "" if ok else result.get("output", ""),
                            },
                        )
                    )
                return
            if node == "summarize":
                report = str(update.get("report", ""))
                message_id = message_id or f"message_{run_id}"
                emit(_wire_event("message.started", {**base, "messageId": message_id}))
                for delta in _report_deltas(report):
                    emit(
                        _wire_event(
                            "message.delta",
                            {**base, "messageId": message_id, "delta": delta},
                        )
                    )
                emit(
                    _wire_event(
                        "message.completed",
                        {**base, "messageId": message_id, "content": report},
                    )
                )

        if method == "run.start":
            update = session.start(_required_string(params, "goal"), progress)
        else:
            update = session.resume(
                _required_string(params, "runId"),
                params.get("response"),
                _optional_string(params, "interruptId"),
                progress,
            )
        return _frames_for_update(update)

    def _dispatch(
        self,
        method: str,
        params: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """执行一个协议方法。"""
        if method == "runtime.initialize":
            return {
                "protocolVersion": str(PROTOCOL_VERSION),
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
            sandbox_mode = _sandbox_mode(params.get("sandboxMode", "workspace-write"))
            session = self._runtime.create_session(
                _required_string(params, "workspacePath"),
                sandbox_mode=sandbox_mode,
                approval_mode=str(params.get("approvalMode", "interactive")),
            )
            return {
                "sessionId": session.id,
                "workspace": session.workspace.to_dict(),
                "sandboxMode": sandbox_mode,
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
            if "request_id" in request or "protocol_version" in request:
                api.process_wire(
                    request,
                    lambda frame: _write_frame(output_stream, frame),
                )
                continue
            frames = api.handle(request)
        except (json.JSONDecodeError, ValueError) as exc:
            frames = [_error(None, "invalid_json", str(exc))]
        for frame in frames:
            _write_frame(output_stream, frame)


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
        events.append(
            {"event": names.get(str(pending.value.get("kind")), "run.input_required"), "data": data}
        )
    return result, events


def _report_deltas(report: str, size: int = 80) -> list[str]:
    """把最终报告切分为稳定的流式文本片段。"""
    if not report:
        return []
    return [report[index : index + size] for index in range(0, len(report), size)]


def _write_frame(output_stream: TextIO, frame: Mapping[str, Any]) -> None:
    """写出并刷新单个 NDJSON 协议帧。"""
    output_stream.write(
        json.dumps(sanitize_value(frame), ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    output_stream.flush()


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


def _sandbox_mode(value: Any) -> SandboxMode:
    """校验并返回沙箱模式。"""
    if value not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError("sandboxMode 无效")
    return value


def _error(request_id: Any, code: str, message: str) -> dict[str, Any]:
    """构造稳定的协议错误帧。"""
    return {"id": request_id, "error": {"code": code, "message": message}}


def _wire_error(request_id: Any, code: str, message: str) -> dict[str, Any]:
    """构造桌面端协议错误信封。"""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def _wire_event(name: str, data: Any) -> dict[str, Any]:
    """构造桌面端协议事件信封。"""
    payload = dict(data) if isinstance(data, Mapping) else {"value": data}
    return {
        "protocol_version": PROTOCOL_VERSION,
        "event_id": f"evt_{uuid4().hex}",
        "type": name,
        "occurred_at": datetime.now(UTC).isoformat(),
        "session_id": payload.get("sessionId"),
        "run_id": payload.get("runId"),
        "task_id": payload.get("taskId"),
        "payload": payload,
    }
