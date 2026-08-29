"""Windows 受限令牌与 ACL 沙箱启动器。"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FAILURE_EXIT = 127
WRITE_RESTRICTED = 0x8
SE_GROUP_LOGON_ID = 0xC0000000


def available() -> bool:
    """检查 Windows 沙箱依赖是否可用。"""
    if sys.platform != "win32":
        return False
    try:
        _modules()
        return True
    except ImportError:
        return False


def workspace_sid(path: Path):
    """为规范工作区派生稳定 SID。"""
    win32security, *_ = _modules()
    canonical = os.path.normcase(str(path.resolve())).encode("utf-8")
    parts = [
        int.from_bytes(hashlib.sha256(canonical).digest()[index : index + 4], "little")
        for index in range(0, 16, 4)
    ]
    return win32security.ConvertStringSidToSid("S-1-5-21-" + "-".join(str(part) for part in parts))


def ensure_write_grant(path: Path, sid) -> None:
    """幂等地向目录及其后代授予指定 SID 写权限。"""
    win32security, _, _, _, ntsecuritycon, _ = _modules()
    path.mkdir(parents=True, exist_ok=True)
    security = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = security.GetSecurityDescriptorDacl() or win32security.ACL()
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        if len(ace) >= 3 and ace[2] == sid:
            return
    inheritance = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
    mask = ntsecuritycon.FILE_GENERIC_WRITE | ntsecuritycon.FILE_DELETE_CHILD | ntsecuritycon.DELETE
    dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, inheritance, mask, sid)
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


def run_restricted(command: list[str], workspace: Path, temp_dir: Path, mode: str) -> int:
    """使用 WRITE_RESTRICTED 主令牌运行命令。"""
    win32security, win32api, win32con, win32process, _, win32job = _modules()
    workspace_write_sid = workspace_sid(workspace)
    temp_write_sid = workspace_sid(temp_dir)
    if mode == "workspace-write":
        ensure_write_grant(workspace, workspace_write_sid)
        ensure_write_grant(temp_dir, temp_write_sid)
    access = (
        win32security.TOKEN_DUPLICATE
        | win32security.TOKEN_QUERY
        | win32security.TOKEN_ASSIGN_PRIMARY
    )
    source_token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), access)
    groups = win32security.GetTokenInformation(source_token, win32security.TokenGroups)
    logon_sid = next((sid for sid, attributes in groups if attributes & SE_GROUP_LOGON_ID), None)
    if logon_sid is None:
        raise RuntimeError("无法取得当前登录 SID")
    world_sid = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
    restricted_sids = [(world_sid, 0), (logon_sid, 0)]
    if mode == "workspace-write":
        restricted_sids.extend([(workspace_write_sid, 0), (temp_write_sid, 0)])
    token = win32security.CreateRestrictedToken(
        source_token,
        win32security.DISABLE_MAX_PRIVILEGE | WRITE_RESTRICTED,
        [],
        [],
        restricted_sids,
    )
    startup = win32process.STARTUPINFO()
    startup.dwFlags |= win32process.STARTF_USESTDHANDLES
    startup.hStdInput = win32api.GetStdHandle(win32con.STD_INPUT_HANDLE)
    startup.hStdOutput = win32api.GetStdHandle(win32con.STD_OUTPUT_HANDLE)
    startup.hStdError = win32api.GetStdHandle(win32con.STD_ERROR_HANDLE)
    for handle in (startup.hStdInput, startup.hStdOutput, startup.hStdError):
        win32api.SetHandleInformation(
            handle, win32con.HANDLE_FLAG_INHERIT, win32con.HANDLE_FLAG_INHERIT
        )
    flags = win32con.CREATE_SUSPENDED | win32con.CREATE_UNICODE_ENVIRONMENT
    environment = dict(os.environ)
    environment.update({"TMP": str(temp_dir), "TEMP": str(temp_dir)})
    process_handle, thread_handle, _, _ = win32process.CreateProcessAsUser(
        token,
        None,
        subprocess.list2cmdline(command),
        None,
        None,
        True,
        flags,
        environment,
        str(workspace),
        startup,
    )
    job = win32job.CreateJobObject(None, "")
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    win32job.AssignProcessToJobObject(job, process_handle)
    win32process.ResumeThread(thread_handle)
    win32event = __import__("win32event")
    win32event.WaitForSingleObject(process_handle, win32event.INFINITE)
    exit_code = win32process.GetExitCodeProcess(process_handle)
    thread_handle.Close()
    process_handle.Close()
    token.Close()
    source_token.Close()
    job.Close()
    return int(exit_code)


def _modules():
    """延迟加载仅 Windows 可用的模块。"""
    import ntsecuritycon
    import win32api
    import win32con
    import win32job
    import win32process
    import win32security

    return win32security, win32api, win32con, win32process, ntsecuritycon, win32job


def main(argv: list[str] | None = None) -> int:
    """解析 runner 参数并运行受限命令。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--mode", choices=["read-only", "workspace-write"])
    parser.add_argument("--workspace")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.probe:
        return 0 if available() else FAILURE_EXIT
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not args.mode or not args.workspace or not command:
        print("aurora-windows-acl: 参数不完整", file=sys.stderr)
        return FAILURE_EXIT
    temp_dir = Path(tempfile.mkdtemp(prefix="aurora-sandbox-"))
    try:
        return run_restricted(command, Path(args.workspace), temp_dir, args.mode)
    except Exception as exc:
        print(f"aurora-windows-acl: {exc}", file=sys.stderr)
        return FAILURE_EXIT
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
