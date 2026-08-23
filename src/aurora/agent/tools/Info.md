# 工具层（Tools）

Agent 可用的工具与风险分级表。

## Tool 接口

见 `base.py`：

- `name`：工具唯一名
- `description`：供规划器/模型理解用途
- `risk`：风险分级（`RiskLevel`）
- `run(**kwargs) -> str`：执行并返回文本结果

## 风险分级（RiskLevel）

- `read`：只读，默认放行
- `write`：写文件，需确认
- `execute`：执行命令，需确认

写与执行默认拒绝，需确认门放行（见 ADR-005，确认门位于 `safety` 包，后续接入）。

## 内置工具

见 `builtin.py`：

- `list_files(path=".")`：递归列出目录结构（read）
- `read_file(path)`：读 UTF-8 文本文件，不存在时返回明确提示（read）
- `write_file`：后续随确认门一起实现（write，默认拒绝）

> 说明：mock 模式指「规划/模型输出确定」，工具本身是真实读写文件系统，不是假实现。
