# 核心层（Core）

委派树编排：根 Agent 规划 → 拆解 → 并行派发子 Agent → 汇总，用 LangGraph 状态图承载（见 ADR-004）。

## 委派图结构

LangGraph `StateGraph`，四阶段：

```
START → plan（规划）→ dispatch（fan-out 条件边）→ execute（并行执行叶子任务）→ summarize（汇总）→ END
```

- `plan`：调用 `Planner.plan(goal)` 产出 `tasks: list[Task]`
- `dispatch`：条件边函数，按 `tasks` 返回 `list[Send]` 动态 fan-out
- `execute`：每个 `Send` 分支并行执行一个叶子任务（经工具），产出 `results`（用 `operator.add` reducer 累加）
- `summarize`：把 `results` 合成 `report`

> 并行用 LangGraph `Send` API（`add_conditional_edges` 的条件函数返回 `list[Send]`）。生产化时再引入子图（Subgraph）与检查点（Checkpoint）。

## 状态模型

见 `state.py`：`DelegationState`（goal / tasks / current_task / results / report）。

## 推理强度（reasoning effort）

规划阶段按任务复杂度给出 low / medium / high 档位（`state.py` 的 `Effort` 枚举），决定每个委派节点投入多少推理。mock 阶段用确定性启发式分档，后续升级为 LLM 自适应决策。

## 文件划分

- `state.py`：`Effort` / `Task` / `Result` / `DelegationState`
- `planner.py`：`Planner` 协议 + `MockPlanner`（确定性，预留 `LLMPlanner`）
- `graph.py`：`build_delegation_graph(planner, tools)` 构建并编译委派图

依赖 langchain-core + langgraph（不引入 langchain 元包，见 ADR-003）。
