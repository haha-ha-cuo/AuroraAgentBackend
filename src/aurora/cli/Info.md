# 命令行接口

一个命令对应一个 Python 文件，`main.py` 负责注册总入口。

- `serve`：启动持续终端会话，支持 `/say`、`/plan`、`/run`、`/clear`、`/help` 和 `/exit`。
- `sandbox`：执行单个 Agent 目标，默认使用本机 `workspace-write` 沙箱。
- `demo`：运行最小委派图示例。
- `eval`：评估入口，尚未实现。

`serve` 与 `sandbox` 均可用 `--mode` 切换只读、工作区可写或显式无隔离模式。
