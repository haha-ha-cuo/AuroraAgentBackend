# 传输层（Transport）

运行时协议 v1：与上层应用（桌面壳）通信。

- 走 stdio（NDJSON 中继，经 Rust 转发），运行时**不监听任何本地端口**
- 请求关联、超时、pending request 失败回收
- 事件广播，客户端按实体 ID 幂等 upsert
- 对 stdout 协议帧与 stderr 日志脱敏，不打印 API Key

已实现的方法：

- `runtime.initialize`
- `workspace.validate`
- `session.create`
- `run.start`
- `run.resume`
- `session.close`

运行可能产生 `approval.required`、`clarification.required`、`evaluation.required` 和 `run.completed` 事件。文件夹选择器由桌面壳负责，运行时只接收、规范化并校验绝对路径。每个 session 独立持有 Sandbox、工具集合和 LangGraph checkpointer。
