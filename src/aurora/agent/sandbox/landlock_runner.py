"""Linux Landlock 自限制启动器。"""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
import sys
from pathlib import Path

CREATE_RULESET = 444
ADD_RULE = 445
RESTRICT_SELF = 446
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
ACCESS_EXECUTE = 1 << 0
ACCESS_WRITE_FILE = 1 << 1
ACCESS_READ_FILE = 1 << 2
ACCESS_READ_DIR = 1 << 3
ACCESS_REFER = 1 << 13
ACCESS_TRUNCATE = 1 << 14
ACCESS_V1 = (1 << 13) - 1
READ_ACCESS = ACCESS_EXECUTE | ACCESS_READ_FILE | ACCESS_READ_DIR
FILE_ACCESS = ACCESS_EXECUTE | ACCESS_WRITE_FILE | ACCESS_READ_FILE | ACCESS_TRUNCATE
FAILURE_EXIT = 125


class RulesetAttr(ctypes.Structure):
    """Landlock ruleset 属性。"""

    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    """Landlock 路径规则属性。"""

    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def landlock_abi() -> int:
    """查询当前内核支持的 Landlock ABI。"""
    if not sys.platform.startswith("linux"):
        return 0
    result = _syscall(CREATE_RULESET, 0, 0, CREATE_RULESET_VERSION)
    return result if result >= 0 else 0


def apply_landlock(workspace: Path, mode: str) -> None:
    """限制当前进程及其后代的文件访问。"""
    abi = landlock_abi()
    if abi < 3:
        raise OSError(errno.ENOSYS, "需要支持截断限制的 Landlock ABI 3+")
    handled = ACCESS_V1 | ACCESS_REFER | ACCESS_TRUNCATE
    ruleset_attr = RulesetAttr(handled)
    ruleset_fd = _checked_syscall(
        CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    try:
        _add_path_rule(ruleset_fd, Path("/"), READ_ACCESS)
        _add_path_rule(ruleset_fd, Path("/dev/null"), FILE_ACCESS)
        if mode == "workspace-write":
            _add_path_rule(ruleset_fd, workspace.resolve(), handled)
            _add_path_rule(ruleset_fd, Path("/tmp"), handled)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            _raise_errno("prctl")
        _checked_syscall(RESTRICT_SELF, ruleset_fd, 0)
    finally:
        os.close(ruleset_fd)


def _add_path_rule(ruleset_fd: int, path: Path, access: int) -> None:
    """向 ruleset 添加路径授权。"""
    flags = os.O_PATH | os.O_CLOEXEC
    parent_fd = os.open(path, flags)
    try:
        allowed = access if path.is_dir() else access & FILE_ACCESS
        attr = PathBeneathAttr(allowed, parent_fd)
        _checked_syscall(ADD_RULE, ruleset_fd, RULE_PATH_BENEATH, ctypes.byref(attr), 0)
    finally:
        os.close(parent_fd)


def _syscall(number: int, *args) -> int:
    """调用 Linux 系统调用并保留 errno。"""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return int(libc.syscall(number, *args))


def _checked_syscall(number: int, *args) -> int:
    """调用系统调用并把失败转为异常。"""
    result = _syscall(number, *args)
    if result < 0:
        _raise_errno(f"syscall {number}")
    return result


def _raise_errno(operation: str) -> None:
    """抛出当前 errno。"""
    code = ctypes.get_errno()
    raise OSError(code, f"{operation}: {os.strerror(code)}")


def main(argv: list[str] | None = None) -> int:
    """解析启动器参数并执行目标命令。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--mode", choices=["read-only", "workspace-write"])
    parser.add_argument("--workspace")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.probe:
        return 0 if landlock_abi() >= 3 else FAILURE_EXIT
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not args.mode or not args.workspace or not command:
        print("aurora-landlock: 参数不完整", file=sys.stderr)
        return FAILURE_EXIT
    try:
        apply_landlock(Path(args.workspace), args.mode)
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"aurora-landlock: {exc}", file=sys.stderr)
        return FAILURE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
