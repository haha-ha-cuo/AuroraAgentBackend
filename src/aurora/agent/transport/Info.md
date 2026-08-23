# 传输层（Transport）

运行时协议 v1：与上层应用（桌面壳）通信。

- 走 stdio（NDJSON 中继，经 Rust 转发），运行时**不监听任何本地端口**
- 请求关联、超时、pending request 失败回收
- 事件广播，客户端按实体 ID 幂等 upsert
- 对 stdout 协议帧与 stderr 日志脱敏，不打印 API Key

权威字段定义见文档「运行时协议 v1」。
