# 核心层（Core）

委派树编排：根 Agent 规划 → 拆解 → 并行派发子 Agent → 汇总，用 LangGraph 状态图承载（见 ADR-004）。

## 委派图结构

LangGraph `StateGraph`，主链路与澄清循环：

```
START → plan → clarify ──无需澄清──→ dispatch → execute → summarize → [feedback] → END
                    └──需要澄清──→ ask_user ──恢复──→ plan
```

- `plan`：调用 `Planner.plan(goal)` 产出 `tasks: list[Task]`
- `clarify`：调用 `Clarifier.assess(...)` 判断当前目标和计划是否缺少用户决策
- `ask_user`：通过 LangGraph `interrupt` 暂停，恢复后把回答加入上下文并重新规划
- `dispatch`：条件边函数，按 `tasks` 返回 `list[Send]` 动态 fan-out
- `execute`：每个 `Send` 分支并行执行一个叶子任务（经工具），产出 `results`（用 `operator.add` reducer 累加）
- `summarize`：把 `results` 合成 `report`
- `feedback`：可选评分中断，将 1-5 分、文字反馈和轨迹交给 `feedback_sink`

澄清默认最多三轮，避免模型反复询问形成死循环。启用人工中断时应传入 checkpointer，并在每次调用配置稳定的 `thread_id`。`interrupt_before` / `interrupt_after` 可在任意节点增加静态断点。

`trace` 使用 reducer 汇集 plan / clarify / ask_user / execute / summarize / feedback 调用轨迹。`JsonlFeedbackStore` 可把已评分轨迹追加到 eval JSONL 集。

## 状态模型

见 `state.py`：`DelegationState`（目标、任务、澄清历史、结果、报告、轨迹与评分）。

## 推理强度（reasoning effort）

规划阶段按任务复杂度给出 low / medium / high 档位（`state.py` 的 `Effort` 枚举），决定每个委派节点投入多少推理。mock 阶段用确定性启发式分档，后续升级为 LLM 自适应决策。

## 文件划分

- `state.py`：`Effort` / `Task` / `Result` / `DelegationState`
- `planner.py`：`Planner` / `Clarifier` 协议及默认的 `NoClarifier`
- `llm_planner.py`：`LLMPlanner` 与 `LLMClarifier`
- `graph.py`：`build_delegation_graph(planner, tools)` 构建并编译委派图

依赖 langchain-core + langgraph（不引入 langchain 元包，见 ADR-003）。
