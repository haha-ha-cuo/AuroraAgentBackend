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
- 沙箱：按平台使用 Bubblewrap / Landlock / Seatbelt / Windows 受限令牌限制本机进程写入
- 确认门：read 放行、write / execute 默认拒绝，支持交互确认 / 全放行 / 只读三种策略
- 统一彩色日志（rich 的 RichHandler）
- CLI 入口（`uv run aurora demo` / `uv run aurora sandbox`）

**规划中**

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
├── cli/                # 命令行入口（一个命令一个文件）
├── agent/
│   ├── core/           # 委派图 + 规划器 + 推理强度
│   ├── tools/          # 工具抽象 + 内置工具 + 沙箱工具 + 风险分级
│   ├── safety/         # 确认门 / 安全权限模型
│   ├── sandbox/        # 沙箱：隔离工作区 + 受限执行后端
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

### 启动持续会话

```bash
uv run aurora serve
uv run aurora serve --mode read-only --approve never
```

服务启动后支持：

```text
/say <内容>    直接对话并保留多轮上下文；普通文本等同于 /say
/plan <目标>   生成工具执行计划，但不执行
/run <目标>    规划并执行目标
/clear         清空直接对话上下文
/help          显示帮助
/exit          停止服务
```

`/say` 和 `/plan` 不执行工具；`/run` 使用启动参数指定的确认门与本机沙箱。

### 运行沙箱命令（让 Agent 写代码并运行）

```bash
uv run aurora sandbox "在沙箱里写一个计算斐波那契数列的脚本并运行"
# 确认门默认交互式；write / execute 会逐个询问 y/N
uv run aurora sandbox --approve always "..."   # 全放行（危险）
uv run aurora sandbox --approve never "..."    # 只读，写与执行一律拒绝
uv run aurora sandbox --sandbox-dir /tmp/mybox "..."  # 自定义沙箱目录
uv run aurora sandbox --mode read-only "..."   # 文件系统只读
uv run aurora sandbox --mode workspace-write "..."  # 默认，仅工作区和临时目录可写
uv run aurora sandbox --mode danger-full-access "..."  # 无隔离（危险）
```

### 本机沙箱后端

Aurora 会功能探测并按平台选择后端：

- Linux：优先 `bwrap`，不可用时使用内置 Landlock runner；可用包管理器安装 `bubblewrap` 获得更完整的挂载隔离。
- macOS：使用系统自带的 `sandbox-exec` / Seatbelt。
- Windows：使用 `WRITE_RESTRICTED` 受限令牌、NTFS ACL 与 Job Object；`uv sync` 会按平台安装 `pywin32`。

受限模式找不到可用后端时会拒绝执行，不会静默降级到普通子进程。该沙箱面向本地个人 Agent，约束文件写入而不限制读取、网络和进程可见性；不要把它作为公网多租户代码执行边界。

### 日志级别

默认 INFO，可用环境变量调整：

```bash
AURORA_LOG_LEVEL=DEBUG uv run aurora demo
```

## 参考文档

设计文档、架构决策（ADR）与路线图见 [AuroraAgent-demo](https://haha-ha-cuo.github.io/AuroraAgent-demo/)。
