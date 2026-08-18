"""mailslot_probe — 系统级 Mailslot 探测脚本.

任务:
    1. 列举系统上所有可能的 Mailslot (FindFirstFileW + 备用 cmd `dir`)
    2. 尝试打开候选 Mailslot 名 (含任务占位值 \\.\mailslot\\civsend 和
       现有静态反汇编推断值 \\.\mailslot\\RemoteUtyCtrlCmd)
    3. 对存在的 Mailslot, 发送 cmd_open / cmd_count 探活包, 看是否被接收
    4. 输出结构化探测报告

用法:
    cd d:\\my git\\rs-ba1-reverse
    d:\\my git\\scratchpad\\.venv\\Scripts\\python.exe scripts\\mailslot_probe.py

    仅列举不发送探活包:
        python scripts\\mailslot_probe.py --list-only

    指定额外候选名 (多个用逗号分隔):
        python scripts\\mailslot_probe.py --extra mailslot1,mailslot2

注意:
    - 本脚本不依赖真实硬件 / 真实 RemoteUtility 进程, 仅探测 Mailslot 名字空间
    - 探活包选用 CMD_GET_COUNT_CLIENT_TRANS (cmd_code=0, payload 空),
      这是最安全的查询命令 (无副作用)
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from typing import List, Optional, Tuple

# 把 src/ 加到 sys.path, 让 rsba1.mailslot 可被 import
_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from rsba1.mailslot import protocol as P  # noqa: E402
from rsba1.mailslot.client import (       # noqa: E402
    MailslotClient,
    MailslotError,
    MailslotNotFoundError,
    MailslotWriteError,
    MailslotTimeoutError,
    INVALID_HANDLE_VALUE,
)


# ============================================================
# 候选 Mailslot 名清单 (按可能性排序)
# ============================================================
# 1. 任务规格占位值 (待沈遥动态确认)
# 2. 现有静态反汇编推断值 (UtyCtrl_deep_analysis.md 5.4):
#    - RemoteUtyCtrlCmd : UtyCtrl -> RemoteUtility 命令通道 (本客户端要写的目标)
#    - RemoteCivCtrlCmd : CivCtrl -> RemoteUtility (CI-V 控制)
#    - RemoteHidCtrlCmd : HidCtrl -> RemoteUtility (HID 控制)
# 3. 通用候选 (常见命名约定)
CANDIDATE_MAILSLOT_NAMES = [
    r"\\.\mailslot\civsend",                 # 任务占位
    r"\\.\mailslot\RemoteUtyCtrlCmd",        # 静态分析推断 (UtyCtrl 写)
    r"\\.\mailslot\RemoteCivCtrlCmd",        # 静态分析推断 (CivCtrl 写)
    r"\\.\mailslot\RemoteHidCtrlCmd",        # 静态分析推断 (HidCtrl 写)
    r"\\.\mailslot\RemoteUtyCtrlRes",        # 响应 mailslot (UtyCtrl 读)
    r"\\.\mailslot\RemoteUtility",           # 通用候选
    r"\\.\mailslot\IcomRemoteUty",           # Icom 命名约定候选
    r"\\.\mailslot\IcomRemoteUtility",
]


# ============================================================
# 1. 列举系统 Mailslot
# ============================================================

def list_mailslots_via_findfirstfile() -> List[str]:
    """用 FindFirstFileW / FindNextFileW 列举 \\.\mailslot\\* 下的 mailslot。

    返回: 找到的 mailslot 短名列表 (不含 \\.\mailslot\\ 前缀)。

    说明:
        - Win32 Mailslot 名字空间支持 FindFirstFile/FindNextFile 枚举 (本机)
        - 每个返回项是一个 mailslot 名 (服务端 CreateMailslot 时指定的名字)
        - 失败 (FindFirstFile 返回 INVALID_HANDLE_VALUE) 时返回空列表,
          调用方可回退到 cmd `dir` 方式
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class WIN32_FIND_DATAW(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("dwReserved0", wintypes.DWORD),
            ("dwReserved1", wintypes.DWORD),
            ("cFileName", wintypes.WCHAR * 260),
            ("cAlternateFileName", wintypes.WCHAR * 14),
        ]

    kernel32.FindFirstFileW.restype = wintypes.HANDLE
    kernel32.FindFirstFileW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(WIN32_FIND_DATAW)]
    kernel32.FindNextFileW.restype = wintypes.BOOL
    kernel32.FindNextFileW.argtypes = [wintypes.HANDLE, ctypes.POINTER(WIN32_FIND_DATAW)]
    kernel32.FindClose.restype = wintypes.BOOL
    kernel32.FindClose.argtypes = [wintypes.HANDLE]

    find_data = WIN32_FIND_DATAW()
    search_pattern = r"\\.\mailslot\*"
    handle = kernel32.FindFirstFileW(search_pattern, ctypes.byref(find_data))

    if handle == INVALID_HANDLE_VALUE or handle is None:
        # 错误码: ERROR_FILE_NOT_FOUND (2) = 无 mailslot, ERROR_PATH_NOT_FOUND (3) = 路径错
        return []

    names: List[str] = []
    try:
        # 第一个结果已经在 find_data 里
        # 跳过 "." / ".."
        name = find_data.cFileName
        if name not in (".", ".."):
            names.append(name)
        while kernel32.FindNextFileW(handle, ctypes.byref(find_data)):
            name = find_data.cFileName
            if name not in (".", ".."):
                names.append(name)
    finally:
        kernel32.FindClose(handle)

    return names


def list_mailslots_via_cmd_dir() -> Tuple[List[str], str]:
    """备用: 用 cmd `dir \\.\mailslot\\` 列举 mailslot。

    返回: (mailslot 短名列表, cmd 原始输出)

    说明:
        - cmd `dir` 在某些 Windows 版本上能枚举 mailslot 名字空间
        - 现代版本可能因安全策略不返回任何内容, 故仅作后备
    """
    try:
        result = subprocess.run(
            ["cmd", "/c", "dir", r"\\.\mailslot\\"],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        output = result.stdout + result.stderr
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        return [], f"cmd 调用失败: {e}"

    # 失败特征: cmd 报错 (例如 "找不到文件" / "语法不正确" / "系统找不到指定的路径")
    # 现代版本 Windows 上 dir \\.\mailslot\ 通常直接报错, 不返回 mailslot 列表
    failure_markers = (
        "incorrect",
        "找不到",
        "File Not Found",
        "cannot find",
        "syntax is",
        "系统找不到",
        "语法不正确",
    )
    output_lower = output.lower()
    has_failure = any(m.lower() in output_lower for m in failure_markers)

    names: List[str] = []
    if has_failure:
        # cmd 报错了, 不解析 (避免误把错误信息里的词当成 mailslot 名)
        return names, output

    # 解析 dir 输出: 找形如 "<name>" 的条目 (跳过 . / .. / 卷标行)
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("Volume") or line.startswith("Directory"):
            continue
        if line in (".", ".."):
            continue
        # dir 输出格式: "date time <DIR> name" 或 "name"
        # 取最后一个 token 作 mailslot 名
        tokens = line.split()
        if tokens and not tokens[0].startswith("/") and not line.startswith(" "):
            candidate = tokens[-1]
            # 过滤明显非 mailslot 名 (含冒号 / 等号 / 句号结尾的错误信息词)
            if (":" not in candidate and "=" not in candidate
                    and not candidate.endswith(".")
                    and len(candidate) < 200):
                if candidate not in names and candidate not in (".", ".."):
                    names.append(candidate)
    return names, output


# ============================================================
# 2. 探测单个 Mailslot 是否存在
# ============================================================

def probe_mailslot_exists(mailslot_name: str) -> Tuple[bool, str]:
    """尝试用 CreateFile(GENERIC_WRITE, OPEN_EXISTING) 打开 mailslot。

    返回: (exists, detail_message)
        exists=True  -> 成功打开 (服务端已创建并接受写入)
        exists=False -> 打开失败 (MailslotNotFoundError / 其他错误)
    """
    try:
        client = MailslotClient(mailslot_name)
        client.open()
        client.close()
        return True, "CreateFile 成功 (Mailslot 存在, 接受写入)"
    except MailslotNotFoundError as e:
        return False, f"Mailslot 不存在 [WinError {e.win_error}]"
    except MailslotError as e:
        return False, f"打开失败: {e} [WinError {e.win_error}]"
    except Exception as e:  # pylint: disable=broad-except
        return False, f"意外异常: {type(e).__name__}: {e}"


# ============================================================
# 3. 发送探活包
# ============================================================

def send_probe_packet(mailslot_name: str,
                      cmd_code: int = P.CMD_GET_COUNT_CLIENT_TRANS,
                      payload: bytes = b"") -> Tuple[bool, str]:
    """向存在的 mailslot 发送探活命令包, 报告结果。

    选用 CMD_GET_COUNT_CLIENT_TRANS (cmd_code=0, payload 空) 作为最安全的探活命令:
        - 不修改 RemoteUtility 状态 (纯查询)
        - data_len=0, 总包长 4 字节 (最小包)

    返回: (success, detail_message)
        success=True  -> 写入成功 (不能确认 RemoteUtility 是否处理, 但
                        至少 mailslot 接受了消息)
        success=False -> 写入失败 (含异常类型)
    """
    try:
        with MailslotClient(mailslot_name) as c:
            n = c.write_command(cmd_code, payload)
        return True, (
            f"WriteFile 成功: cmd_code={cmd_code} ({P.CMD_CODES.get(cmd_code, '?')}), "
            f"写入 {n} 字节 (4 头 + {n - 4} payload)"
        )
    except MailslotNotFoundError as e:
        return False, f"Mailslot 在 write 阶段消失: {e}"
    except MailslotTimeoutError as e:
        return False, f"写入超时 (服务端读超时): {e}"
    except MailslotWriteError as e:
        return False, f"写入失败 [WinError {e.win_error}]: {e}"
    except MailslotError as e:
        return False, f"Mailslot 错误: {e}"
    except Exception as e:  # pylint: disable=broad-except
        return False, f"意外异常: {type(e).__name__}: {e}"


# ============================================================
# 4. 报告输出
# ============================================================

def print_section(title: str) -> None:
    line = "=" * 70
    print(f"\n{line}\n{title}\n{line}")


def print_report(list_only: bool, extra_names: List[str]) -> int:
    """主报告生成。返回退出码 (0 = 至少一个 mailslot 探测成功)。"""
    print_section("Mailslot 探测报告")
    print(f"主机: {os.environ.get('COMPUTERNAME', '?')}")
    print(f"模式: {'仅列举' if list_only else '列举 + 探活'}")

    # ----- 1. 列举系统 Mailslot -----
    print_section("[1] 系统已存在的 Mailslot (枚举)")
    print("(a) FindFirstFileW / FindNextFileW 方法:")
    names_find = list_mailslots_via_findfirstfile()
    if names_find:
        for n in names_find:
            print(f"    - {n}")
    else:
        print("    (无 mailslot, 或枚举失败)")

    print("\n(b) cmd `dir \\\\.\\mailslot\\` 方法 (备用):")
    names_dir, dir_output = list_mailslots_via_cmd_dir()
    if names_dir:
        for n in names_dir:
            print(f"    - {n}")
    else:
        print("    (cmd 未返回 mailslot 列表; 现代版本可能不允许枚举)")
        # 仅在 -v 时输出原始 dir 内容
        if "--verbose" in sys.argv or "-v" in sys.argv:
            print("    --- cmd 原始输出 ---")
            for line in dir_output.splitlines()[:30]:
                print(f"    | {line}")

    # 合并去重
    all_enumerated = sorted(set(names_find + names_dir))
    print(f"\n枚举汇总: {len(all_enumerated)} 个 mailslot")
    if all_enumerated:
        for n in all_enumerated:
            print(f"    -> \\\\.\\mailslot\\{n}")

    if list_only:
        print("\n[--list-only] 跳过候选名探测与探活包发送。")
        return 0

    # ----- 2. 候选名探测 -----
    print_section("[2] 候选 Mailslot 名直接探测 (CreateFile)")
    candidates = list(CANDIDATE_MAILSLOT_NAMES) + [
        rf"\\.\mailslot\{n}" for n in extra_names
    ]
    # 去重保序
    seen = set()
    unique_candidates = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique_candidates.append(c)

    found_alive: List[str] = []
    found_writable: List[str] = []
    for name in unique_candidates:
        exists, detail = probe_mailslot_exists(name)
        marker = "[存在]" if exists else "[不存在]"
        print(f"  {marker} {name}")
        print(f"          -> {detail}")
        if exists:
            found_writable.append(name)

    if not found_writable:
        print("\n[结论] 没有候选 mailslot 存在。可能 RemoteUtility 未运行, "
              "或真实 mailslot 名不在候选清单中。")
        print("       建议启动 RemoteUtility.exe 后再次运行本脚本。")
        return 1

    # ----- 3. 发送探活包 -----
    print_section("[3] 发送探活包 (cmd_code=0 GetCountClientTrans, 4 字节)")
    for name in found_writable:
        ok, detail = send_probe_packet(name)
        marker = "[OK]" if ok else "[FAIL]"
        print(f"  {marker} {name}")
        print(f"          -> {detail}")
        if ok:
            found_alive.append(name)

    # ----- 4. 结论 -----
    print_section("[4] 结论")
    if found_alive:
        print(f"以下 mailslot 接受探活包写入 ({len(found_alive)} 个):")
        for n in found_alive:
            print(f"  -> {n}")
        print("\n建议下一步:")
        print("  1. 用 MailslotClient 写入真实 cmd_code (见 protocol.CMD_*)")
        print("  2. 监听 \\\\.\\mailslot\\RemoteUtyCtrlRes (响应通道) 读回 echo")
        print("     响应包 offset 0 应 == 原 cmd_code (echo 确认机制)")
        return 0
    else:
        print("以下 mailslot 存在但写入失败 (可能权限/超时/格式不符):")
        for n in found_writable:
            print(f"  -> {n}")
        print("\n建议:")
        print("  - 检查 RemoteUtility 是否真的在监听 (而非残留 mailslot 句柄)")
        print("  - 检查 FILE_SHARE_WRITE 是否被服务端允许")
        return 2


# ============================================================
# 5. CLI 入口
# ============================================================

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mailslot_probe",
        description="系统级 Mailslot 探测脚本 (RS-BA1 V2 RemoteUtility IPC)"
    )
    p.add_argument(
        "--list-only", action="store_true",
        help="仅列举 mailslot, 不尝试打开/发送探活包"
    )
    p.add_argument(
        "--extra", default="",
        help="额外候选 mailslot 名 (不含 \\\\.\\mailslot\\ 前缀, 逗号分隔)"
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="输出 cmd dir 的原始内容 (调试用)"
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    extra = [n.strip() for n in args.extra.split(",") if n.strip()]
    return print_report(list_only=args.list_only, extra_names=extra)


if __name__ == "__main__":
    sys.exit(main())
