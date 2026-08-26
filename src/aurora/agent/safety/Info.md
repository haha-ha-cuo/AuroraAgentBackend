# 安全权限模型（Safety）

确认门与权限模型（见 ADR-005）。

已落地：
- 确认门：工具按 read / write / execute 分级，写与执行默认拒绝，需确认放行（`gate.py`）
- 三种放行策略：`DenyApprover`（只读）/ `InteractiveApprover`（交互确认）/ `AutoApprover`（显式全放行）
- 确认门已接入委派图执行节点，拒绝时以 `ToolDeniedError` 记为失败分支
- 文件系统沙箱：Linux Bubblewrap/Landlock、macOS Seatbelt、Windows 受限令牌，默认仅工作区可写
- 敏感文件守卫：密钥文件等不进入模型上下文

待实现：
- 命令执行工具的白名单与 dry-run
- 网络、读取与系统调用隔离
- 能力特化层注入上下文的防提示注入策略
