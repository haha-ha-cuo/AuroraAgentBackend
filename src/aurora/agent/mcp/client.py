"""运行于独立事件循环的 stdio MCP 客户端。"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from mcp import Client, StdioServerParameters, types

from .config import McpServerConfig


class McpClientError(RuntimeError):
    """MCP 客户端基础错误。"""


class McpConnectionError(McpClientError):
    """MCP Server 启动或握手失败。"""


class McpCallError(McpClientError):
    """MCP Tool 调用失败。"""


class StdioMcpClient:
    """为同步 Agent Tool 提供长生命周期 stdio MCP 连接。"""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Client | None = None
        self._shutdown: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._close_requested = threading.Event()
        self._startup_error: BaseException | None = None
        self._connect_lock = threading.Lock()
        self._server_info: dict[str, Any] | None = None
        self._protocol_version: str | None = None

    @property
    def connected(self) -> bool:
        """返回连接是否已经完成握手。"""
        return self._client is not None and not self._closed.is_set()

    def connect(self) -> None:
        """启动 MCP Server 子进程并完成协议握手。"""
        with self._connect_lock:
            if self.connected:
                return
            self._ready.clear()
            self._closed.clear()
            self._close_requested.clear()
            self._startup_error = None
            self._thread = threading.Thread(
                target=self._thread_main,
                name=f"mcp-{self.config.name}",
                daemon=True,
            )
            self._thread.start()
            if not self._ready.wait(self.config.timeout):
                self._close_requested.set()
                raise McpConnectionError(f"MCP Server {self.config.name} 连接超时")
            if self._startup_error is not None:
                raise McpConnectionError(
                    f"MCP Server {self.config.name} 连接失败: {self._startup_error}"
                ) from self._startup_error

    def list_tools(self) -> list[types.Tool]:
        """获取 Server 当前暴露的全部工具。"""
        result = self._submit(self._require_client().list_tools())
        return list(result.tools)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用远端 MCP Tool 并渲染为 Agent 可消费的文本。"""
        result = self._submit(
            self._require_client().call_tool(
                name,
                arguments=arguments,
                read_timeout_seconds=self.config.timeout,
            )
        )
        rendered = _render_tool_result(result)
        if result.result_type == "input_required":
            raise McpCallError(f"MCP Tool {name} 请求额外交互，当前客户端尚未提供输入")
        if result.is_error:
            raise McpCallError(rendered or f"MCP Tool {name} 执行失败")
        return rendered

    def status(self) -> dict[str, Any]:
        """返回可安全发送给前端的连接状态。"""
        return {
            **self.config.to_dict(),
            "connected": self.connected,
            "protocolVersion": self._protocol_version,
            "serverInfo": self._server_info,
        }

    def close(self) -> None:
        """关闭会话并回收 MCP Server 子进程。"""
        self._close_requested.set()
        loop = self._loop
        shutdown = self._shutdown
        if loop is not None and shutdown is not None:
            loop.call_soon_threadsafe(shutdown.set)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.config.timeout)
        self._thread = None

    def _thread_main(self) -> None:
        """在专用线程中运行异步 MCP Client。"""
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            self._client = None
            self._loop = None
            self._shutdown = None
            self._closed.set()

    async def _serve(self) -> None:
        """维持 SDK 上下文直到收到关闭信号。"""
        self._loop = asyncio.get_running_loop()
        self._shutdown = asyncio.Event()
        params = StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
            env=dict(self.config.env),
            cwd=self.config.cwd,
            encoding_error_handler="replace",
        )
        async with Client(params, read_timeout_seconds=self.config.timeout) as client:
            self._client = client
            info = client.server_info
            self._server_info = info.model_dump(by_alias=True) if info is not None else None
            self._protocol_version = str(client.protocol_version)
            self._ready.set()
            if self._close_requested.is_set():
                self._shutdown.set()
            await self._shutdown.wait()

    def _require_client(self) -> Client:
        """返回已连接客户端。"""
        if not self.connected or self._client is None:
            raise McpConnectionError(f"MCP Server {self.config.name} 尚未连接")
        return self._client

    def _submit(self, coroutine):
        """把异步 SDK 调用提交到客户端事件循环。"""
        loop = self._loop
        if loop is None:
            raise McpConnectionError(f"MCP Server {self.config.name} 尚未连接")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=self.config.timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise McpCallError(f"MCP Server {self.config.name} 调用超时") from exc
        except McpClientError:
            raise
        except Exception as exc:
            raise McpCallError(f"MCP Server {self.config.name} 调用失败: {exc}") from exc


def _render_tool_result(result: types.CallToolResult) -> str:
    """把 MCP 多模态结果压缩为当前文本 Tool 输出。"""
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        elif isinstance(block, types.ImageContent):
            parts.append(f"[图片 {block.mime_type}，{len(block.data)} 字符]")
        elif isinstance(block, types.AudioContent):
            parts.append(f"[音频 {block.mime_type}，{len(block.data)} 字符]")
        else:
            parts.append(json.dumps(block.model_dump(by_alias=True), ensure_ascii=False, default=str))
    if result.structured_content is not None and not parts:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False, default=str))
    return "\n".join(part for part in parts if part)
