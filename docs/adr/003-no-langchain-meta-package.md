# ADR-003：不引入 LangChain 元包

- 状态：接受
- 日期：2026-08-29

## 决策

仅依赖 `langchain-core`、具体模型适配包和 `langgraph`，避免引入 `langchain` 元包，以缩小依赖面并降低版本耦合。
