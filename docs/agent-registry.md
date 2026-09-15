# Agent 注册与动态委派

在侧边栏「Agent」中新建或编辑角色，选择已配置的模型，填写能力描述与执行 Prompt，并设置工具白名单。能力描述供主 Agent 选择角色，Prompt 用于子 Agent 实际执行。新建角色默认不开放调用，勾选「允许主 Agent 调用」后，后续运行可发现它。

供应商、模型和固定工作流仍在「设置」管理。文本和多模态模型可执行，多模态第一版支持图片理解；向量模型仅登记展示，不能作为执行角色。旧 Agent 迁移后默认不开放调用。

## 代码位置

注册、状态、运行时和委派逻辑统一位于 `aurora.agent.manager`。公共入口为 `AgentRegistry` 与 `AgentRuntime`；流程实现位于 `manager.workflows`，子执行实现位于 `manager.delegation`。对话和预览分别位于 `conversation`、`preview` 功能包。

## 管理协议

沿用协议 v1 的 WebSocket 和桌面 NDJSON 请求，扩展字段通过既有 `data` 对象提交；写入字段为 snake_case，返回实体字段为 camelCase。

- `agent.list/create/update`：新增 `description`、`callable`；`instructions`、`model_config_id`、`allowed_tools`、`enabled` 保持原含义。
- `model.create/update`：新增 `model_type`，取值 `text`、`multimodal`、`embedding`。模型类型同步输入能力；旧客户端仅提交 `input_types` 时仍可更新文本或图片能力。
- `agent.status`：返回 `items`，包括配置问题 `issues`、最近测试 `test`、`runningCount`、`waitingCount`、当前任务 `active` 和最近执行 `latest`。执行摘要含会话、运行和 Agent 执行 ID；完整结果通过 `run.get` 或 `agentRun.get` 读取。
- `agent.test`：参数 `{ "id": "Agent ID" }`，执行最小模型请求，保存 `passed/failed`、测试时间和配置指纹。多模态测试包含图片；配置变化后状态为 `stale`。不会自动探测供应商。

模型和 Agent 配置不包含 API Key；凭据继续使用环境变量或系统钥匙串。状态页每 3 秒刷新，页面不可见时停止轮询。长任务与连接测试异步处理，查询无需等待执行完成。

## 主 Agent 工具

主角色的工具权限需要包含 `*` 或对应工具名。规划前会注入本轮可调用角色清单；不包含主角色自身、停用角色、不可用模型或未开放调用的角色。

`list_agents()` 返回角色 ID、名称、能力描述、模型类型和输入能力。

`call_agent(agent_id, task, artifact_ids=[])` 在父运行内执行子任务。`artifact_ids` 只能引用当前运行持有的 PNG、JPEG 或 WebP 产物，文本模型拒绝图片。返回 JSON 文本，包含 `agentRunId`、`status` 和 `output`；执行失败进入父任务失败路径并保留子执行错误记录。

子角色使用独立模型与 Prompt、自己的普通工具白名单，并继承父运行的工作区、沙箱和审批策略。`agent_respond` 是子角色内部的文本生成/图片理解能力，即使普通工具白名单为空也可使用。子角色不获得 `list_agents` 或 `call_agent`，因此不能递归委派。

## 执行与恢复

每轮冻结可调用角色配置；修改或启停从下一轮生效。AgentRun 保存配置快照和 `parent_task_id`，历史显示使用快照中的角色与模型名称。

带委派工具的执行图串行运行。子图独立保存检查点，审批或澄清透传到父运行，恢复后不重放已完成的叶子任务。各审批点拥有独立公开 ID。子任务失败停止该串行执行；父运行终止时收束仍在执行或等待的子记录。

运行中的检查点与现有工作流一样保留在当前进程；进程重启会将未结束执行标记为中断，不自动重放历史审批。全局角色配置和执行历史保存在 SQLite，通过迁移 `002_agent_registry.sql` 升级。
