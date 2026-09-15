# Agent 管理

- `registry.py`：全局 Agent 注册配置、可用性、状态聚合、连接测试和工具发现。
- `runtime.py`：会话生命周期、工作区校验与运行时依赖。
- `workflows.py`：固定协作流程、配置快照、执行记录与审批恢复。
- `delegation.py`：父运行内的一级子 Agent 委派和检查点桥接。

公共入口为 `aurora.agent.manager.AgentRegistry`、`AgentRuntime` 和 `validate_workspace`。执行图与规划器保留在 `core`；对话和预览分别使用 `conversation`、`preview` 包。

`aurora.agent` 是命名空间目录，顶层不放 Python 文件；新增功能应放进对应功能包。setuptools 显式启用命名空间发现。
