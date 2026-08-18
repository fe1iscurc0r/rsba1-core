"""client — MailslotClient (RemoteController 端 Mailslot 写入客户端).

设计目标:
    模拟"第二 RemoteController", 不依赖 RemoteController.exe / UtyCtrl.dll,
    直接由 Python 进程把命令包写入 RemoteUtility 已经在监听的 Mailslot。

    RemoteUtility (服务端) 用 CreateMailslotA 创建 \\.\mailslot\\<name> 并
    循环 ReadFile 等待命令; 客户端 (本类) 用 CreateFile + WriteFile 把
    4 字节头 + payload 的命令包写进去。

主路径 (pywin32 / win32file):
    优先使用, 因为 pywin32 已经安装且 API 更 Pythonic。
    CreateFile 失败时抛 pywintypes.error(winerror=2 -> MailslotNotFoundError)。
    WriteFile 返回 (winerr, bytes_written) 元组。

后备路径 (ctypes / kernel32):
    pywin32 不可用时自动启用。直接调 kernel32.CreateFileW + WriteFile,
    use_last_error=True, 失败用 ctypes.get_last_error() 取错误码。

CreateFile 参数 (与 UtyCtrl.dll 0x10001743..0x1000174E 一致):
    dwDesiredAccess   = GENERIC_WRITE           (0x40000000)
    dwShareMode       = FILE_SHARE_READ | FILE_SHARE_WRITE   (3)
        ↑ 第二个 FILE_SHARE_WRITE 必须有, 否则第二个写者会被拒绝
          (UtyCtrl 与本客户端可能并发写同一 mailslot)
    lpSecurityAttributes = None
    dwCreationDisposition = OPEN_EXISTING       (3, mailslot 必须已由服务端创建)
    dwFlagsAndAttributes = FILE_ATTRIBUTE_NORMAL (0x80)
    hTemplateFile = None

写入超时说明 (MSDN):
    Mailslot 写操作实际是阻塞调用, 阻塞时长由服务端 CreateMailslot 的
    dwReadTimeout 决定 (本机 mailslot 用本机超时, 跨机 mailslot 用
    MAILSLOT_WAIT_FOREVER)。客户端无法直接设置写入超时。

    本类提供 write_timeout_ms 参数, 仅在 pywin32 + overlapped I/O 路径
    下生效 (MVP 未实现 overlapped, 参数当前作为占位/日志用); 同步路径
    下, WriteFile 阻塞到服务端读走消息或读超时返回。

参考:
    - phase2-re/notes/pe_analysis/UtyCtrl_deep_analysis.md (5.4 Mailslot 参数)
    - MSDN: CreateFile / WriteFile / CreateMailslotA / Mailslots
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional, Tuple

from rsba1.mailslot.protocol import (
    serialize_command,
    deserialize_command,
    MAX_PACKET_SIZE,
    MAX_PAYLOAD_SIZE,
    COMMAND_HEADER_SIZE,
)

__all__ = [
    "MailslotError",
    "MailslotNotFoundError",
    "MailslotWriteError",
    "MailslotTimeoutError",
    "MailslotClient",
    "DEFAULT_MAILSLOT_NAME",
    "GENERIC_WRITE",
    "FILE_SHARE_READ",
    "FILE_SHARE_WRITE",
    "OPEN_EXISTING",
    "FILE_ATTRIBUTE_NORMAL",
    "INVALID_HANDLE_VALUE",
    "ERROR_FILE_NOT_FOUND",
    "ERROR_TIMEOUT",
    "BACKEND_PYWIN32",
    "BACKEND_CTYPES",
]

# ============================================================
# 默认 Mailslot 名 (占位, 待沈遥动态确认)
# ============================================================
# 任务规格默认值: \\.\mailslot\civsend (可配置)
# 现有静态反汇编 (UtyCtrl_deep_analysis.md 5.4) 推断实际值为:
#     \\.\mailslot\RemoteUtyCtrlCmd  (RemoteUtility 创建/读, UtyCtrl 写)
#     \\.\mailslot\RemoteUtyCtrlRes  (UtyCtrl 创建/读, RemoteUtility 写)
# 动态确认 (2026-08-09 mailslot_probe.py 探测结果, 主机 LAPTOP-3AE66LI6):
#     - \\.\mailslot\civsend            -> 不存在 (WinError 2)
#     - \\.\mailslot\RemoteUtyCtrlCmd  -> 存在且接受 WriteFile (4 字节探活包成功)
#     - 其它候选 (RemoteCivCtrlCmd / RemoteHidCtrlCmd / RemoteUtyCtrlRes) -> 不存在
# 沈遥逆向确认 (command_protocol.md §1): 核心函数 0x100010C7 + ExecCmd 0x1000174E 均引用此字符串
DEFAULT_MAILSLOT_NAME = r"\\.\mailslot\RemoteUtyCtrlCmd"

# ============================================================
# Windows API 常量 (与 win32file / kernel32 头文件一致)
# ============================================================
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value  # 0xFFFFFFFFFFFFFFFF

# Windows 错误码 (子集)
ERROR_FILE_NOT_FOUND = 2       # mailslot 不存在 (服务端未 CreateMailslot)
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5       # FILE_SHARE_WRITE 缺失时第二写者会撞这个
ERROR_INVALID_HANDLE = 6
ERROR_TIMEOUT = 1460           # 写入超时 (服务端读超时触发)

# Backend 标识
BACKEND_PYWIN32 = "pywin32"
BACKEND_CTYPES = "ctypes"

# 尝试导入 pywin32 (主路径), 失败则启用 ctypes 后备路径
try:
    import win32file       # type: ignore
    import pywintypes      # type: ignore
    _HAS_PYWIN32 = True
except ImportError:         # pragma: no cover - 仅在 pywin32 未装时触发
    win32file = None        # type: ignore
    pywintypes = None       # type: ignore
    _HAS_PYWIN32 = False


# ============================================================
# 异常类型
# ============================================================

class MailslotError(Exception):
    """Mailslot 客户端基础异常。"""

    def __init__(self, message: str, *, win_error: Optional[int] = None):
        super().__init__(message)
        self.win_error = win_error


class MailslotNotFoundError(MailslotError):
    """Mailslot 不存在 (服务端未 CreateMailslot, 或名字错误)。

    对应 CreateFile 失败 + GetLastError == ERROR_FILE_NOT_FOUND (2)。
    """


class MailslotWriteError(MailslotError):
    """WriteFile 失败 (非超时类错误)。"""


class MailslotTimeoutError(MailslotError):
    """WriteFile 超时 (服务端读超时触发, ERROR_TIMEOUT=1460)。"""


# ============================================================
# 辅助: 错误码 -> 异常类型 映射
# ============================================================

def _classify_create_error(win_error: int, mailslot_name: str) -> MailslotError:
    """CreateFile 失败时, 根据 GetLastError 选择具体异常类型。"""
    msg = f"CreateFile({mailslot_name!r}) 失败: WinError={win_error}"
    if win_error in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
        return MailslotNotFoundError(
            f"Mailslot 不存在 (服务端未创建或名字错误): {mailslot_name} "
            f"[WinError {win_error}]",
            win_error=win_error,
        )
    if win_error == ERROR_ACCESS_DENIED:
        return MailslotError(
            f"Mailslot 访问被拒 (可能 FILE_SHARE_WRITE 缺失): {mailslot_name} "
            f"[WinError {win_error}]",
            win_error=win_error,
        )
    return MailslotError(msg, win_error=win_error)


def _classify_write_error(win_error: int) -> MailslotError:
    """WriteFile 失败时, 根据 WinError 选择具体异常类型。"""
    if win_error == ERROR_TIMEOUT:
        return MailslotTimeoutError(
            f"WriteFile 超时 (服务端读超时触发) [WinError {win_error}]",
            win_error=win_error,
        )
    return MailslotWriteError(
        f"WriteFile 失败 [WinError {win_error}]",
        win_error=win_error,
    )


# ============================================================
# MailslotClient
# ============================================================

class MailslotClient:
    """Windows Mailslot 写入客户端 (模拟 RemoteController 写命令到 RemoteUtility)。

    用法 (上下文管理器, 推荐):
        with MailslotClient(r"\\\\.\\mailslot\\RemoteUtyCtrlCmd") as c:
            n = c.write_command(CMD_GET_COUNT_CLIENT_TRANS)
            # n = 实际写入字节数 (== 4 + len(payload))

    用法 (显式 open/close, 适合反复写入):
        c = MailslotClient()
        c.open()
        try:
            c.write_command(CMD_EXEC_CMD, b"\\x01\\x02\\x03")
        finally:
            c.close()

    参数:
        mailslot_name:    Mailslot 全路径 (默认 \\\\.\\mailslot\\civsend 占位;
                          实际逆向值 \\\\.\\mailslot\\RemoteUtyCtrlCmd 见
                          UtyCtrl_deep_analysis.md 5.4)。
        backend:          "pywin32" (默认, 需 pywin32) 或 "ctypes" (后备);
                          None 时按 pywin32 -> ctypes 顺序自动选择。
        write_timeout_ms: 写入超时 (毫秒), 同步路径下仅作日志占位;
                          0 表示使用 OS 默认行为 (阻塞直到服务端读走)。
        reserved:         命令包 reserved 字段默认值 (2 字节, 默认 0)。
    """

    def __init__(
        self,
        mailslot_name: str = DEFAULT_MAILSLOT_NAME,
        *,
        backend: Optional[str] = None,
        write_timeout_ms: int = 0,
        reserved: int = 0,
    ):
        if not isinstance(mailslot_name, str) or not mailslot_name:
            raise ValueError(f"mailslot_name 必须是非空 str, 实际 {mailslot_name!r}")
        # 规范化: 允许用户传正斜杠形式, 统一转反斜杠 (Win32 API 接受两者, 但
        # Mailslot 名规范是 \\.\mailslot\<name>)
        self.mailslot_name = mailslot_name.replace("/", "\\")

        # 选择 backend
        if backend is None:
            backend = BACKEND_PYWIN32 if _HAS_PYWIN32 else BACKEND_CTYPES
        elif backend not in (BACKEND_PYWIN32, BACKEND_CTYPES):
            raise ValueError(f"未知 backend: {backend!r}")
        if backend == BACKEND_PYWIN32 and not _HAS_PYWIN32:
            raise RuntimeError(
                "backend=pywin32 但 pywin32 未安装; 请 pip install pywin32 "
                "或显式传 backend='ctypes'"
            )
        self.backend = backend

        if write_timeout_ms < 0:
            raise ValueError(
                f"write_timeout_ms 必须 >= 0, 实际 {write_timeout_ms}"
            )
        self.write_timeout_ms = int(write_timeout_ms)

        if not isinstance(reserved, int):
            raise TypeError(f"reserved 必须是 int, 实际 {type(reserved).__name__}")
        self.default_reserved = reserved & 0xFFFF

        # 句柄状态
        self._handle = None  # type: Optional[object]
        # 上次操作诊断信息 (调试用)
        self.last_bytes_written: int = 0
        self.last_win_error: Optional[int] = None

    # ============================================================
    # 打开 / 关闭 Mailslot 写入端
    # ============================================================

    def open(self) -> None:
        """CreateFile 打开 Mailslot 写入端 (GENERIC_WRITE, OPEN_EXISTING)。

        幂等: 已打开时直接返回。失败抛 MailslotNotFoundError / MailslotError。

        CreateFile 参数与 UtyCtrl.dll 0x10001743..0x1000174E 一致:
            GENERIC_WRITE | FILE_SHARE_READ|FILE_SHARE_WRITE | OPEN_EXISTING
            (FILE_SHARE_WRITE 必须有, 否则并发写者被拒)
        """
        if self._handle is not None:
            return  # 幂等
        if self.backend == BACKEND_PYWIN32:
            self._handle = self._open_pywin32()
        else:
            self._handle = self._open_ctypes()

    def _open_pywin32(self):
        """pywin32 路径: win32file.CreateFile。

        失败时 win32file 抛 pywintypes.error, 含 .winerror 字段。
        """
        try:
            handle = win32file.CreateFile(
                self.mailslot_name,
                GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,                       # lpSecurityAttributes
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                None,                       # hTemplateFile
            )
        except pywintypes.error as e:
            self.last_win_error = e.winerror
            raise _classify_create_error(e.winerror, self.mailslot_name) from e
        return handle

    def _open_ctypes(self):
        """ctypes 路径: kernel32.CreateFileW (后备)。

        use_last_error=True 后用 ctypes.get_last_error() 取错误码。
        """
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 确保函数原型正确 (CreateFileW 签名)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,           # lpFileName
            wintypes.DWORD,             # dwDesiredAccess
            wintypes.DWORD,             # dwShareMode
            wintypes.LPVOID,            # lpSecurityAttributes
            wintypes.DWORD,             # dwCreationDisposition
            wintypes.DWORD,             # dwFlagsAndAttributes
            wintypes.HANDLE,            # hTemplateFile
        ]
        handle = kernel32.CreateFileW(
            self.mailslot_name,
            GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            err = ctypes.get_last_error()
            self.last_win_error = err
            raise _classify_create_error(err, self.mailslot_name)
        return handle

    def close(self) -> None:
        """CloseHandle 关闭 Mailslot 句柄。幂等 (已关闭则 no-op)。"""
        if self._handle is None:
            return
        try:
            if self.backend == BACKEND_PYWIN32:
                # win32file.CloseHandle 接受 PyHANDLE
                win32file.CloseHandle(self._handle)
            else:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle(self._handle)
        except Exception:
            # CloseHandle 失败一般无害 (句柄可能已失效), 仅吞掉避免二次异常
            pass
        finally:
            self._handle = None

    # ============================================================
    # 写命令
    # ============================================================

    def write_command(
        self,
        cmd_code: int,
        payload: bytes = b"",
        reserved: Optional[int] = None,
    ) -> int:
        """序列化命令包并 WriteFile 到 Mailslot。

        参数:
            cmd_code: 命令码 (0..255), 见 protocol.CMD_*。
            payload:  负载 bytes (0..255 字节, 受 data_len 单字节限制)。
            reserved: 命令包 reserved 字段 (2 字节); None 时用 self.default_reserved。

        返回:
            实际写入字节数 (== COMMAND_HEADER_SIZE + len(payload) = 4 + len(payload))。

        异常:
            MailslotNotFoundError - Mailslot 不存在 (自动 open 阶段失败)。
            MailslotWriteError    - WriteFile 失败 (非超时)。
            MailslotTimeoutError  - WriteFile 超时 (ERROR_TIMEOUT=1460)。
            (来自 protocol 层) InvalidCommandCodeError / PayloadTooLargeError /
                TypeError - 序列化阶段参数校验失败。
        """
        # 1. 序列化 (校验在 protocol 层完成)
        if reserved is None:
            reserved = self.default_reserved
        packet = serialize_command(cmd_code, payload, reserved=reserved)

        # 防御: 协议层应已保证 packet 长度 <= MAX_PACKET_SIZE
        if len(packet) > MAX_PACKET_SIZE:
            raise MailslotWriteError(
                f"序列化后包长 {len(packet)} 超过 {MAX_PACKET_SIZE} (协议上限)"
            )

        # 2. 自动 open (懒打开, 便于 mock 测试只 patch WriteFile)
        opened_here = False
        if self._handle is None:
            self.open()
            opened_here = True

        # 3. WriteFile
        try:
            written = self._write(packet)
        finally:
            if opened_here:
                # 懒打开后失败也确保关闭句柄, 避免泄漏
                self.close()

        self.last_bytes_written = written
        return written

    def _write(self, data: bytes) -> int:
        """分发到具体 backend 执行 WriteFile, 返回写入字节数。"""
        if self.backend == BACKEND_PYWIN32:
            return self._write_pywin32(data)
        return self._write_ctypes(data)

    def _write_pywin32(self, data: bytes) -> int:
        """pywin32 路径: win32file.WriteFile。

        返回 (winerr, bytes_written); winerr == 0 表示成功。
        非 0 时按错误码分类抛异常 (ERROR_TIMEOUT -> MailslotTimeoutError)。
        """
        # 注意: 同步 (无 overlapped) 写入, 第三参数 None
        try:
            err, bytes_written = win32file.WriteFile(self._handle, data)
        except pywintypes.error as e:
            self.last_win_error = e.winerror
            raise _classify_write_error(e.winerror) from e
        if err != 0:
            self.last_win_error = err
            raise _classify_write_error(err)
        return int(bytes_written)

    def _write_ctypes(self, data: bytes) -> int:
        """ctypes 路径: kernel32.WriteFile (后备)。

        WriteFile(hFile, lpBuffer, nNumberOfBytesToWrite,
                  lpNumberOfBytesWritten, lpOverlapped) -> BOOL
        """
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,                   # hFile
            wintypes.LPCVOID,                  # lpBuffer (const void*)
            wintypes.DWORD,                    # nNumberOfBytesToWrite
            wintypes.LPDWORD,                  # lpNumberOfBytesWritten
            wintypes.LPVOID,                   # lpOverlapped
        ]
        written = wintypes.DWORD(0)
        # 用 (c_char * len)(*.data) 而非 c_char_p, 避免中间 NUL 截断问题
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        ok = kernel32.WriteFile(
            self._handle,
            buf,
            len(data),
            ctypes.byref(written),
            None,
        )
        if not ok:
            err = ctypes.get_last_error()
            self.last_win_error = err
            raise _classify_write_error(err)
        return int(written.value)

    # ============================================================
    # 便捷方法
    # ============================================================

    def write_and_deserialize_echo(
        self,
        cmd_code: int,
        payload: bytes = b"",
        *,
        reserved: Optional[int] = None,
    ) -> Tuple[int, int, int, bytes]:
        """写命令后立即反序列化本地写入的包 (自检/调试用)。

        返回 deserialize_command(本地写入的字节流)。
        注意: 此方法不读 Mailslot 响应; 真正的响应需读 RemoteUtyCtrlRes
        mailslot (RemoteUtility 写, UtyCtrl 读), 由服务端实现, 不在客户端 MVP 范围内。
        """
        if reserved is None:
            reserved = self.default_reserved
        packet = serialize_command(cmd_code, payload, reserved=reserved)
        self.write_command(cmd_code, payload, reserved=reserved)
        return deserialize_command(packet)

    # ============================================================
    # 上下文管理器
    # ============================================================

    def __enter__(self) -> "MailslotClient":
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False  # 不吞异常

    def __repr__(self) -> str:
        return (
            f"<MailslotClient mailslot={self.mailslot_name!r} "
            f"backend={self.backend!r} open={self._handle is not None}>"
        )
