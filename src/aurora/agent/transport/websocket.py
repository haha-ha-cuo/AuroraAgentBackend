"""WebSocket 传输层：浏览器开发模式下的运行时协议服务。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping

from websockets.asyncio.server import ServerConnection, serve

from .api import RuntimeApi
from aurora.text import sanitize_value

logger = logging.getLogger(__name__)


async def serve_websocket(api: RuntimeApi, host: str = "127.0.0.1", port: int = 8765) -> None:
    """启动 WebSocket 服务，桥接浏览器与 RuntimeApi。"""

    async def handler(websocket: ServerConnection) -> None:
        logger.info("WebSocket 客户端已连接: %s", websocket.remote_address)
        try:
            async for raw in websocket:
                if not isinstance(raw, str) or not raw.strip():
                    continue
                try:
                    request = json.loads(raw)
                    if not isinstance(request, Mapping):
                        await websocket.send(json.dumps(
                            {"error": {"code": "invalid_request", "message": "请求必须是 JSON 对象"}},
                            ensure_ascii=False,
                        ))
                        continue
                except json.JSONDecodeError as exc:
                    await websocket.send(json.dumps(
                        {"error": {"code": "invalid_json", "message": str(exc)}},
                        ensure_ascii=False,
                    ))
                    continue

                if "request_id" in request or "protocol_version" in request:
                    async def emit(frame: dict[str, Any]) -> None:
                        await websocket.send(json.dumps(sanitize_value(frame), ensure_ascii=False, separators=(",", ":")))
                    api.process_wire(request, lambda frame: asyncio.ensure_future(emit(frame)))
                else:
                    frames = api.handle(request)
                    for frame in frames:
                        await websocket.send(json.dumps(sanitize_value(frame), ensure_ascii=False, separators=(",", ":")))
        except Exception:
            logger.debug("WebSocket 客户端断开", exc_info=True)
        finally:
            logger.info("WebSocket 客户端已断开: %s", websocket.remote_address)

    async with serve(handler, host, port) as server:
        logger.info("WebSocket 运行时服务已启动: ws://%s:%s/ws", host, port)
        await server.serve_forever()
