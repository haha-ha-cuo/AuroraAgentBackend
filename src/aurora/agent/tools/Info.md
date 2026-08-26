# 工具层（Tools）

Agent 可用的工具与风险分级表。

## Tool 接口

见 `base.py`：

- `name`：工具唯一名
- `description`：供规划器/模型理解用途
- `risk`：风险分级（`RiskLevel`）
- `params_schema`：由函数签名自动生成的参数 JSON Schema
- `run(**kwargs) -> str`：执行并返回文本结果

## 声明式注册（装饰器）

工具用 `@tool` 装饰器声明，自动注册进模块级注册表：

```python
from aurora.agent.tools import RiskLevel, tool

@tool(name="read_file", description="读取 UTF-8 文本文件", risk=RiskLevel.READ)
def read_file(path: str) -> str:
    ...
```

执行阶段用 `get_available_tools()` 一次性取回全部工具（`{name: Tool}`）：

```python
from aurora.agent.tools import get_available_tools

tools = get_available_tools()
```

- 加一个工具 = 写一个函数 + 加一个装饰器，无需再改 `__init__.py` / `cli/main.py`。
- 参数 schema 由函数签名（类型注解 + 默认值）生成，规划器 prompt 里的工具清单也由
  `format_tools_for_llm(tools)` 渲染，不再手写参数格式。

## 风险分级（RiskLevel）

- `read`：只读，默认放行
- `write`：写文件，需确认
- `execute`：执行命令，需确认

写与执行默认拒绝，由 `safety` 包的确认门放行（见 ADR-005），已接入委派图执行节点。

## 内置工具

见 `builtin.py`（真实项目文件系统，只读）：

- `list_files(path=".")`：递归列出目录结构（read）
- `read_file(path)`：读 UTF-8 文本文件，不存在时返回明确提示（read）

见 `sandbox_tools.py`（圈定在沙箱工作区内）：

- `write_file(path, content)`：写入/覆盖沙箱内文本文件（write）
- `run_command(command, timeout)`：沙箱内运行 shell 命令（execute）
- `run_python(code, timeout)`：沙箱内运行 Python 代码（execute）
- `sandbox_list_files(path=".")`：列出沙箱目录结构（read）
- `sandbox_read_file(path)`：读取沙箱内文本文件（read）

> 说明：mock 模式指「规划/模型输出确定」，工具本身是真实读写文件系统，不是假实现。
