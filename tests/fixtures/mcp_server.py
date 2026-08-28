"""供 MCP 客户端集成测试使用的 stdio Server。"""

from mcp import types
from mcp.server.mcpserver import MCPServer

server = MCPServer("aurora-test-server", version="1.0.0")


@server.tool(annotations=types.ToolAnnotations(read_only_hint=True))
def add(a: int, b: int) -> int:
    """计算两个整数之和。"""
    return a + b


@server.tool()
def store(value: str) -> str:
    """模拟有副作用的写入操作。"""
    return f"stored:{value}"


if __name__ == "__main__":
    server.run(transport="stdio")
