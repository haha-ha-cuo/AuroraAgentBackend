# 沙箱（Sandbox）

面向本地个人 Agent 的跨平台文件写入沙箱。目标命令仍使用宿主工具链，但进程及其后代只能按模式修改文件。

## 平台后端

| 平台 | 后端 | 选择方式 |
|---|---|---|
| Linux | Bubblewrap | 优先使用，根文件系统只读，工作区重新绑定为可写，`/tmp` 使用 tmpfs |
| Linux | Landlock | Bubblewrap 不可用时使用内置 ctypes runner，要求 ABI 3+ |
| macOS | Seatbelt | 使用系统 `sandbox-exec`，默认允许操作但拒绝白名单外写入 |
| Windows | Restricted Token + ACL | 使用 `WRITE_RESTRICTED`、工作区 SID、私有临时目录 SID 和 Job Object |

后端在首次执行前进行功能探测。受限模式没有可用后端时抛出 `SandboxUnavailableError`，不会运行原始命令。

## 权限模式

- `read-only`：除 `/dev/null` 等平台必要对象外禁止写入。
- `workspace-write`：默认模式，只允许写入沙箱工作区和平台临时目录。
- `danger-full-access`：显式使用普通宿主子进程，不施加文件隔离。

## 组成

- `executor.py`：执行协议、结果模型、流式限额输出与进程树超时处理。
- `local.py`：平台探测、profile 构造和 runner 选择。
- `landlock_runner.py`：Linux Landlock 自限制后执行启动器。
- `windows_runner.py`：Windows ACL 授权和受限主令牌启动器。
- `sandbox.py`：工作区路径圈定、文件操作与命令执行门面。

## 安全边界

该机制只承诺限制文件写入，不隔离文件读取、网络、系统调用、设备或进程可见性。Windows 机制受 Everyone ACL、硬链接和非 NTFS 文件系统影响，只能视为部分强制执行。它适用于用户本人运行的桌面 Agent，不适用于互不信任的服务器多租户。
