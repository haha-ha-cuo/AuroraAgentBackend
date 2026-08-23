# MCP 集成（Model Context Protocol）

MCP 客户端：内嵌于 Agent 运行时，负责连接与协议通信。

- Server 以 stdio / SSE / HTTP 运行，暴露 tools / resources / prompts
- 桌面场景优先本地 stdio 进程，远程场景可用 SSE / HTTP
- 能力特化层可把 MCP Server 注册为特化工具
