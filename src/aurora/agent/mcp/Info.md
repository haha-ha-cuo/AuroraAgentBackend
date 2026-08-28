# MCP 功能包与客户端

MCP 集成分为目录式功能包、统一客户端和 Aurora Tool 适配三层。前端面向功能包，底层 Server 接口保留给调试和兼容场景。

- 当前使用官方 MCP Python SDK v2 实现本地 stdio transport
- 每个 Server 在独立事件循环线程中维持异步会话
- Server 工具以 `mcp.<server>.<tool>` 转换成同步 Aurora Tool
- `readOnlyHint=true` 映射为 READ，其余工具保守映射为 EXECUTE，功能包可以向更高风险覆盖
- SDK 负责子进程启动、握手和关闭回收，环境变量仅显式传递

功能包协议方法：

- `mcp.package.catalog`：返回功能包 Manifest、配置 JSON Schema 和插件加载错误
- `mcp.package.connect`：按 `packageId`、可选 `instanceName` 和 `config` 建立连接
- `mcp.package.list`：列出通过功能包建立的连接
- `mcp.package.disconnect`：按 `instanceName` 断开连接

底层协议方法：

- `mcp.server.connect`
- `mcp.server.list`
- `mcp.server.disconnect`

内置 `blender` 包默认运行 `uvx blender-mcp`。内置 `qq` 包不绑定具体机器人实现，要求配置已安装 QQ MCP Server 的 `command`，从而兼容不同 OneBot 或官方机器人适配器。

每个软件对应一个完整目录：

```text
packages/
├── blender/
│   └── package.yaml
└── qq/
    └── package.yaml
```

`package.yaml` 统一声明 `package`、`server`、`configSchema` 和 `functions`。一个 function 表示软件中的一项能力，通过 `tools` glob 关联一个或多个远端 MCP Tool，并可配置 `read`、`write` 或 `execute` 风险级别：

```yaml
functions:
  message.send:
    description: 向好友或群发送消息。
    tools:
      - send_*
    risk: execute
```

新增内置软件时只需增加目录。外部 Python 包也可以携带同样的目录，并让 entry point 返回该目录路径：

```toml
[project.entry-points."aurora.mcp_packages"]
my_package = "my_plugin:package_directory"
```

MCP Server 应在 `session.create` 前连接，新会话会获取当时已经发现的 MCP 工具快照。当前版本尚未暴露 resources、prompts、采样回调和交互式 elicitation。
