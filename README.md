# Aurora

形如 CodeX 的全平台桌面 AI Agent 应用 —— 用户把项目交给 Agent，Agent 会自主规划、委派并完成开发任务。

> 完整的设计与使用文档见：[AuroraAgent-demo 文档站](https://haha-ha-cuo.github.io/AuroraAgent-demo/)

## 项目定位

传统编码助手以「单轮对话 + 单点补全」为主，Aurora 要交付的是一个可长时间自主运行的项目级 Agent：

- **树形委派**：根 Agent 顶层规划 → 拆解 → 并行派发子 Agent → 汇总
- **推理强度**：按任务复杂度在 low / medium / high 三档间权衡成本与质量
- **能力特化层**：用户可插拔地注入上下文、工具、提示与检索策略
- **全平台分发**：最终形态是跨 macOS / Windows / Linux 的桌面应用（Tauri 2 + Nuxt）

## 当前状态

参考文档路线图，当前处于 **阶段二（Agent 核心）→ 阶段三（桌面壳 MVP）** 的过渡期。

**已实现（Python 运行时，最小纵向切片）**

- 最小委派图：LangGraph 状态图 + Send 并行派发（plan → dispatch → execute → summarize）
- 推理强度三档（low / medium / high）+ mock 确定性分档
- 工具层：list_files / read_file + 风险分级（read / write / execute）
- 统一彩色日志（rich 的 RichHandler）
- CLI 入口（`uv run aurora demo`）

**规划中**

- 确认门 / 安全权限模型（write / execute 默认拒绝）
- 本地 API 管理层（mock 客户端 + 真实 OpenAI 兼容 API）
- 能力特化层 / 缓存层 / MCP 集成
- SQLite 持久化 / 运行时协议（stdio NDJSON）
- 桌面壳（Tauri 2 + Nuxt）与 Python sidecar 打包

## 技术栈

| 组件 | 选型 |
|---|---|
| Python | 3.13（uv 管理） |
| 编排 | langchain-core + langgraph（不引入 langchain 元包，见 ADR-003） |
| 终端美化 / 日志 | rich |
| 测试 | pytest |
| 桌面壳（规划） | Tauri 2 + Nuxt（Vue 3 + TypeScript，SPA） |

## 目录结构

```
src/aurora/
├── logging.py          # 统一彩色日志（RichHandler）
├── cli/                # 命令行入口（argparse）
├── agent/
│   ├── core/           # 委派图 + 规划器 + 推理强度
│   ├── tools/          # 工具抽象 + 内置工具 + 风险分级
│   ├── safety/         # 确认门 / 安全权限模型（待实现）
│   ├── model_access/   # 本地 API 管理层（待实现）
│   ├── capability/     # 能力特化层（待实现）
│   ├── cache/          # 缓存层（待实现）
│   ├── mcp/            # MCP 集成（待实现）
│   ├── store/          # SQLite 持久化（待实现）
│   └── transport/      # 运行时协议 / stdio NDJSON（待实现）
└── eval/               # 评估与校准（待实现）
tests/                  # pytest 测试
```

## 快速开始

### 环境准备

- [uv](https://docs.astral.sh/uv/)（Python 环境与依赖管理）
- Python 3.13

### 安装

```bash
uv sync
```

### 运行最小 demo（mock 模式，无需密钥）

```bash
uv run aurora demo
# 或指定目标
uv run aurora demo "列出项目文件结构"
```

默认目标为「列出项目文件结构，并阅读 README.md 总结项目定位」，会规划出子任务、并行派发并输出汇总报告。

### 日志级别

默认 INFO，可用环境变量调整：

```bash
AURORA_LOG_LEVEL=DEBUG uv run aurora demo
```

## 参考文档

设计文档、架构决策（ADR）与路线图见 [AuroraAgent-demo](https://haha-ha-cuo.github.io/AuroraAgent-demo/)。
