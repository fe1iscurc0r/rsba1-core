"""civ_via_execcmd — CI-V 命令经 ExecCmd (cmd_code=2) Mailslot 发送的高层 API。

设计目标:
    把 CI-V 命令帧包成 ExecCmd payload, 通过 MailslotClient 写入
    \\\\.\\mailslot\\RemoteUtyCtrlCmd, 让本机 RemoteUtility 把 CI-V 命令
    转发到电台 (如 IC-705)。陆墨作为"第二 RemoteController"无需依赖
    RemoteController.exe / UtyCtrl.dll / CivCtrl.dll。

核心发现 (mailslot_server.md §3.2 交叉验证 command_protocol.md §4.3):
    ExecCmd 在 RemoteUty 端有 **子命令分发**: packet[0x14] = sub_cmd (0-5)。
    RemoteUty 在 0x43b02a 处 `movzx eax, byte ptr [esi + 0x14]` 读取该字节,
    分发到不同处理函数:
        sub_cmd 0 -> 0x43a3f0
        sub_cmd 1 -> 0x43a5f0
        sub_cmd 2 -> 0x43a800
        sub_cmd 3 -> 0x43aa70
    各 sub_cmd 的具体语义 (CI-V 转发 / HID / UDP ...) 需动态确认。

    ExecCmd payload 布局 (20 字节固定头 + user_data):
        payload[0..3]   = 0 (栈残留)
        payload[4..7]   = arg3 (DWORD, 语义待动态确认)
        payload[8..11]  = arg5 (DWORD = user_data 长度)
        payload[12]     = arg6_byte (标志字节, 语义待动态确认)
        payload[13..15] = 0 (栈残留)
        payload[16]     = sub_cmd (0-5, RemoteUty 子命令分发)
        payload[17..19] = 0 (栈残留)
        payload[20+]    = user_data (CI-V 帧 / 命令体)

    data_len = (arg5 & 0xFF) + 0x14 = user_data_len + 20

关键限制:
    - ExecCmd 是 fire-and-forget: 写 Mailslot 后无法直接读响应
    - 响应走 \\\\.\\mailslot\\RemoteUtyCtrlRes (由 RemoteController 创建)
    - RemoteController 开着时, 响应被 RemoteController 收走, 陆墨只能靠 GUI 看效果
    - RemoteController 没开时, 陆墨可自行创建 RemoteUtyCtrlRes 独占响应
    - arg3 / arg6 / sub_cmd / user_data 格式语义均需动态确认

参考:
    - re/utyctrl/command_protocol.md §4.3 (ExecCmd 字段分解)
    - re/remoteuty/mailslot_server.md §3.2 (sub_cmd 嵌套分发)
    - src/rsba1/mailslot/commands.py (build_exec_cmd)
    - src/rsba1/ctypes_wrappers/civ_commands.py (CI-V 帧构造)
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

from rsba1.mailslot.commands import build_exec_cmd
from rsba1.mailslot.protocol import CMD_EXEC_CMD, MAX_PAYLOAD_SIZE
from rsba1.mailslot.client import (
    MailslotClient,
    MailslotError,
    MailslotNotFoundError,
    MailslotWriteError,
    MailslotTimeoutError,
    DEFAULT_MAILSLOT_NAME,
)
from rsba1.ctypes_wrappers import civ_commands as civcmd
from rsba1.mailslot import civ_response as civresp

__all__ = [
    "DEFAULT_TO_ADDR",
    "DEFAULT_FROM_ADDR",
    "DEFAULT_SUB_CMD",
    "DEFAULT_ARG3",
    "DEFAULT_ARG6",
    "RESPONSE_MAILSLOT_NAME",
    "RESPONSE_MAILSLOT_CB_MAX_MSG",
    "build_exec_cmd_civ",
    "build_read_freq_payload",
    "build_read_mode_payload",
    "build_set_freq_payload",
    "build_ptt_on_payload",
    "build_ptt_off_payload",
    "build_read_smeter_payload",
    "build_raw_civ_payload",
    "CivViaExecCmdSender",
    "ResponseReader",
    "ResponseReadError",
    "ResponseTimeoutError",
]


# ============================================================
# 默认值
# ============================================================

DEFAULT_TO_ADDR = civcmd.IC705_TO_ADDR        # 0xA4 (IC-705)
DEFAULT_FROM_ADDR = civcmd.DEFAULT_FROM_ADDR  # 0x00
DEFAULT_SUB_CMD = 0    # sub_cmd=0 调用 RemoteUty 0x43a3f0 (语义待确认)
DEFAULT_ARG3 = 0       # arg3 DWORD 语义待动态确认
DEFAULT_ARG6 = 0       # arg6 BYTE 语义待动态确认

# 响应 Mailslot (RemoteUty 写, RemoteController/UtyCtrl 创建并读)
RESPONSE_MAILSLOT_NAME = r"\\.\mailslot\RemoteUtyCtrlRes"
RESPONSE_MAILSLOT_CB_MAX_MSG = 260  # CreateMailslotA cbMaxMsg=0x104


# ============================================================
# ExecCmd payload 构造 (CI-V 命令 -> ExecCmd payload)
# ============================================================

def build_exec_cmd_civ(
    civ_frame: bytes,
    arg3: int = DEFAULT_ARG3,
    arg6: int = DEFAULT_ARG6,
    sub_cmd: int = DEFAULT_SUB_CMD,
) -> Tuple[bytes, int]:
    """把 CI-V 帧包成 ExecCmd payload。

    核心高层 API: 把任意 CI-V 帧/命令体 bytes 包成 ExecCmd (cmd_code=2)
    的 payload, 然后可交给 MailslotClient.write_command(CMD_EXEC_CMD, payload) 发送。

    参数:
        civ_frame: CI-V 帧 bytes。支持三种格式 (由调用方决定):
            - 完整帧: FE FE to from cmd... FD (civcmd.build_frame() 输出)
            - 命令体: to from cmd... (civcmd.build_frame()[2:-1], civSend 约定)
            - 纯命令: cmd... (civcmd.read_freq_bytes() 等输出)
            长度需 <= 235 (data_len <= 255)。
        arg3:    ExecCmd arg3 DWORD (语义待动态确认, 默认 0)
        arg6:    ExecCmd arg6 BYTE (语义待动态确认, 默认 0)
        sub_cmd: RemoteUty 子命令码 0-5 (默认 0, 语义待动态确认)

    返回:
        (payload, data_len): payload 长度 = len(civ_frame) + 20
    """
    if not isinstance(civ_frame, (bytes, bytearray)):
        raise TypeError(
            f"civ_frame 必须是 bytes/bytearray, 实际 {type(civ_frame).__name__}"
        )
    civ_frame = bytes(civ_frame)

    user_len = len(civ_frame)
    data_len = user_len + 0x14
    if data_len > MAX_PAYLOAD_SIZE:
        raise ValueError(
            f"civ_frame 过长: {user_len} 字节, data_len={data_len} 超过 "
            f"MAX_PAYLOAD_SIZE={MAX_PAYLOAD_SIZE} (最多 {MAX_PAYLOAD_SIZE - 0x14} 字节)"
        )

    arg5 = user_len
    return build_exec_cmd(arg3, arg5, arg6, civ_frame, sub_cmd=sub_cmd)


# ============================================================
# 高层 CI-V 命令 payload 构造器
# ============================================================
# user_data 格式采用 civSend 约定: [to, from, cmd...] (不含 FE/FD),
# 因为 RemoteUty 内部转发时很可能自行添加帧定界符 (与 CivCtrl.dll civSend
# 行为一致)。如需发送完整帧 (含 FE/FD), 用 build_raw_civ_payload()。
# ============================================================

def _civ_body(to_addr: int, from_addr: int, cmd_bytes: bytes) -> bytes:
    """构造 CI-V 命令体 [to, from, cmd...] (civSend 约定, 不含 FE/FD)。"""
    return bytes([to_addr & 0xFF, from_addr & 0xFF]) + bytes(cmd_bytes)


def build_read_freq_payload(
    to_addr: int = DEFAULT_TO_ADDR,
    from_addr: int = DEFAULT_FROM_ADDR,
    arg3: int = DEFAULT_ARG3,
    arg6: int = DEFAULT_ARG6,
    sub_cmd: int = DEFAULT_SUB_CMD,
) -> Tuple[bytes, int]:
    """读频率 CI-V 命令 (cmd=0x03) -> ExecCmd payload。

    CI-V 命令体: [to, from, 0x03] (3 字节)
    电台应答: [from, to, 0x03, BCD_freq(5)] (8 字节)
    """
    body = _civ_body(to_addr, from_addr, civcmd.read_freq_bytes())
    return build_exec_cmd_civ(body, arg3=arg3, arg6=arg6, sub_cmd=sub_cmd)


def build_read_mode_payload(
    to_addr: int = DEFAULT_TO_ADDR,
    from_addr: int = DEFAULT_FROM_ADDR,
    arg3: int = DEFAULT_ARG3,
    arg6: int = DEFAULT_ARG6,
    sub_cmd: int = DEFAULT_SUB_CMD,
) -> Tuple[bytes, int]:
    """读模式 CI-V 命令 (cmd=0x04) -> ExecCmd payload。

    CI-V 命令体: [to, from, 0x04] (3 字节)
    电台应答: [from, to, 0x04, mode, filter] (5 字节)
    """
    body = _civ_body(to_addr, from_addr, bytes([civcmd.CMD_READ_MODE]))
    return build_exec_cmd_civ(body, arg3=arg3, arg6=arg6, sub_cmd=sub_cmd)


def build_set_freq_payload(
    hz: int,
    to_addr: int = DEFAULT_TO_ADDR,
    from_addr: int = DEFAULT_FROM_ADDR,
    arg3: int = DEFAULT_ARG3,
    arg6: int = DEFAULT_ARG6,
    sub_cmd: int = DEFAULT_SUB_CMD,
) -> Tuple[bytes, int]:
    """设频率 CI-V 命令 (cmd=0x06 + 5 字节 BCD) -> ExecCmd payload。

    参数:
        hz: 频率 (Hz), 如 14270000 = 14.270 MHz

    CI-V 命令体: [to, from, 0x06, BCD_freq(5)] (8 字节)

    异常:
        ValueError - hz 不在业余频段白名单内 (安全约束, 见 civ_commands.assert_allowed_freq)。
    """
    civcmd.assert_allowed_freq(hz)  # 白名单校验: 拦截越界频率
    body = _civ_body(to_addr, from_addr, civcmd.set_freq_bytes(hz))
    return build_exec_cmd_civ(body, arg3=arg3, arg6=arg6, sub_cmd=sub_cmd)


def build_ptt_on_payload(
    to_addr: int = DEFAULT_TO_ADDR,
    from_addr: int = DEFAULT_FROM_ADDR,
    arg3: int = DEFAULT_ARG3,
    arg6: int = DEFAULT_ARG6,
    sub_cmd: int = DEFAULT_SUB_CMD,
) -> Tuple[bytes, int]:
    """PTT ON (TX) CI-V 命令 (cmd=0x1C 0x00 0x01) -> ExecCmd payload。

    CI-V 命令体: [to, from, 0x1C, 0x00, 0x01] (5 字节)
    """
    body = _civ_body(to_addr, from_addr, civcmd.ptt_on_bytes())
    return build_exec_cmd_civ(body, arg3=arg3, arg6=arg6, sub_cmd=sub_cmd)


def build_ptt_off_payload(
    to_addr: int = DEFAULT_TO_ADDR,
    from_addr: int = DEFAULT_FROM_ADDR,
    arg3: int = DEFAULT_ARG3,
    arg6: int = DEFAULT_ARG6,
    sub_cmd: int = DEFAULT_SUB_CMD,
) -> Tuple[bytes, int]:
    """PTT OFF (RX) CI-V 命令 (cmd=0x1C 0x00 0x00) -> ExecCmd payload。

    CI-V 命令体: [to, from, 0x1C, 0x00, 0x00] (5 字节)
    """
    body = _civ_body(to_addr, from_addr, civcmd.ptt_off_bytes())
    return build_exec_cmd_civ(body, arg3=arg3, arg6=arg6, sub_cmd=sub_cmd)


def build_read_smeter_payload(
    to_addr: int = DEFAULT_TO_ADDR,
    from_addr: int = DEFAULT_FROM_ADDR,
    arg3: int = DEFAULT_ARG3,
    arg6: int = DEFAULT_ARG6,
    sub_cmd: int = DEFAULT_SUB_CMD,
) -> Tuple[bytes, int]:
    """读 S-meter CI-V 命令 (cmd=0x1A 0x03) -> ExecCmd payload。

    CI-V 命令体: [to, from, 0x1A, 0x03] (4 字节)
    电台应答: [from, to, 0x1A, 0x03, S_meter_data] (5+ 字节)
    """
    body = _civ_body(to_addr, from_addr, bytes([civcmd.CMD_READ_SMETER, 0x03]))
    return build_exec_cmd_civ(body, arg3=arg3, arg6=arg6, sub_cmd=sub_cmd)


def build_raw_civ_payload(
    civ_frame: bytes,
    arg3: int = DEFAULT_ARG3,
    arg6: int = DEFAULT_ARG6,
    sub_cmd: int = DEFAULT_SUB_CMD,
) -> Tuple[bytes, int]:
    """原始 CI-V 帧 -> ExecCmd payload (透传, 不添加 to/from 地址)。

    与 build_exec_cmd_civ 相同, 显式命名以区分"原始帧"与"高层构造"场景。

    用法:
        frame = civcmd.build_frame(0xA4, 0x00, civcmd.read_freq_bytes())
        payload, _ = build_raw_civ_payload(frame)
    """
    return build_exec_cmd_civ(civ_frame, arg3=arg3, arg6=arg6, sub_cmd=sub_cmd)


# ============================================================
# CivViaExecCmdSender — 封装 MailslotClient 的高层发送器
# ============================================================

class CivViaExecCmdSender:
    """CI-V 命令经 ExecCmd Mailslot 发送的高层封装。

    封装 MailslotClient, 提供便捷的 CI-V 命令发送方法。
    所有方法均为 fire-and-forget (ExecCmd 不读响应)。

    用法 (上下文管理器, 推荐):
        with CivViaExecCmdSender() as s:
            s.send_read_freq()
            s.send_ptt_on()
            time.sleep(1)
            s.send_ptt_off()

    参数:
        mailslot_name: 命令 Mailslot 路径 (默认 RemoteUtyCtrlCmd)
        to_addr:       目标电台 CI-V 地址 (默认 0xA4 = IC-705)
        from_addr:     源控制器 CI-V 地址 (默认 0x00)
        arg3:          ExecCmd arg3 DWORD (默认 0, 语义待确认)
        arg6:          ExecCmd arg6 BYTE (默认 0, 语义待确认)
        sub_cmd:       RemoteUty 子命令码 0-5 (默认 0, 语义待确认)
        backend:       MailslotClient backend ("pywin32" / "ctypes" / None)
    """

    def __init__(
        self,
        mailslot_name: str = DEFAULT_MAILSLOT_NAME,
        *,
        to_addr: int = DEFAULT_TO_ADDR,
        from_addr: int = DEFAULT_FROM_ADDR,
        arg3: int = DEFAULT_ARG3,
        arg6: int = DEFAULT_ARG6,
        sub_cmd: int = DEFAULT_SUB_CMD,
        backend: Optional[str] = None,
    ):
        self.to_addr = to_addr & 0xFF
        self.from_addr = from_addr & 0xFF
        self.arg3 = arg3
        self.arg6 = arg6
        self.sub_cmd = sub_cmd
        self._client = MailslotClient(mailslot_name, backend=backend)

    def open(self) -> None:
        """打开 Mailslot 写入端 (幂等)。"""
        self._client.open()

    def close(self) -> None:
        """关闭 Mailslot 写入端 (幂等)。"""
        self._client.close()

    def __enter__(self) -> "CivViaExecCmdSender":
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def send_payload(self, payload: bytes) -> int:
        """发送已构造的 ExecCmd payload, 返回写入字节数 (= 4 + len(payload))。"""
        return self._client.write_command(CMD_EXEC_CMD, payload)

    def send_civ_frame(self, civ_frame: bytes) -> int:
        """发送原始 CI-V 帧 (透传, 不添加 to/from)。"""
        payload, _ = build_raw_civ_payload(
            civ_frame, arg3=self.arg3, arg6=self.arg6, sub_cmd=self.sub_cmd
        )
        return self.send_payload(payload)

    def send_read_freq(self) -> int:
        """发送读频率命令 (CI-V cmd=0x03)。fire-and-forget。"""
        payload, _ = build_read_freq_payload(
            self.to_addr, self.from_addr,
            arg3=self.arg3, arg6=self.arg6, sub_cmd=self.sub_cmd,
        )
        return self.send_payload(payload)

    def send_read_mode(self) -> int:
        """发送读模式命令 (CI-V cmd=0x04)。fire-and-forget。"""
        payload, _ = build_read_mode_payload(
            self.to_addr, self.from_addr,
            arg3=self.arg3, arg6=self.arg6, sub_cmd=self.sub_cmd,
        )
        return self.send_payload(payload)

    def send_set_freq(self, hz: int) -> int:
        """发送设频率命令 (CI-V cmd=0x06 + BCD)。

        参数:
            hz: 频率 (Hz), 如 14270000 = 14.270 MHz
        """
        payload, _ = build_set_freq_payload(
            hz, self.to_addr, self.from_addr,
            arg3=self.arg3, arg6=self.arg6, sub_cmd=self.sub_cmd,
        )
        return self.send_payload(payload)

    def send_ptt_on(self) -> int:
        """发送 PTT ON (TX) 命令 (CI-V cmd=0x1C 0x00 0x01)。fire-and-forget。"""
        payload, _ = build_ptt_on_payload(
            self.to_addr, self.from_addr,
            arg3=self.arg3, arg6=self.arg6, sub_cmd=self.sub_cmd,
        )
        return self.send_payload(payload)

    def send_ptt_off(self) -> int:
        """发送 PTT OFF (RX) 命令 (CI-V cmd=0x1C 0x00 0x00)。fire-and-forget。"""
        payload, _ = build_ptt_off_payload(
            self.to_addr, self.from_addr,
            arg3=self.arg3, arg6=self.arg6, sub_cmd=self.sub_cmd,
        )
        return self.send_payload(payload)

    def send_read_smeter(self) -> int:
        """发送读 S-meter 命令 (CI-V cmd=0x1A 0x03)。fire-and-forget。"""
        payload, _ = build_read_smeter_payload(
            self.to_addr, self.from_addr,
            arg3=self.arg3, arg6=self.arg6, sub_cmd=self.sub_cmd,
        )
        return self.send_payload(payload)

    # ==========================================================
    # 闭环查询 (query) — 发送命令 + 读 RemoteUtyCtrlRes + 解析
    # ==========================================================
    # 前置条件: RemoteController 未运行, 陆墨可独占创建 RemoteUtyCtrlRes。
    # RemoteController 运行时该 mailslot 被占用, 闭环查询会失败,
    # 此时只能用上面 fire-and-forget 的 send_* 方法。
    # ==========================================================

    def query(
        self,
        send_fn,
        parse_fn,
        *,
        timeout_ms: int = 2000,
        reader: Optional["ResponseReader"] = None,
    ):
        """发送命令, 从 RemoteUtyCtrlRes 读取响应并解析 (完整闭环)。

        参数:
            send_fn:   无参回调, 负责发送命令 (如 lambda: s.send_read_freq())。
            parse_fn:  接收原始响应 bytes 并返回结构化结果的回调。
            timeout_ms: 读响应总超时 (ms)。
            reader:    可选, 复用外部 ResponseReader; None 时内部新建并关闭。

        返回:
            parse_fn 的返回值 (如频率 Hz / 模式元组)。

        异常:
            ResponseTimeoutError - 超时未收到响应。
            ResponseReadError    - 读取失败。
            civ_response.*       - 解析失败 (帧未找到 / 命令不匹配)。
        """
        created_here = reader is None
        if created_here:
            reader = ResponseReader(read_timeout_ms=timeout_ms)
        reader.open()
        try:
            send_fn()
            deadline = time.time() + (timeout_ms / 1000.0)
            # 每轮最多等 200ms, 在总超时内持续轮询
            while time.time() < deadline:
                resp = reader.read(timeout_ms=200)
                if resp:
                    return parse_fn(resp)
            raise ResponseTimeoutError(
                f"查询超时 (>{timeout_ms}ms), 未在 RemoteUtyCtrlRes 收到响应。"
                "请确认 RemoteController 未运行 (未占用该 mailslot)。"
            )
        finally:
            if created_here:
                reader.close()

    def query_freq(self, timeout_ms: int = 2000) -> int:
        """闭环查询频率 -> Hz。

        发送 read_freq 命令, 读取 RemoteUtyCtrlRes 响应并解析为频率。
        """
        return self.query(
            self.send_read_freq,
            civresp.parse_freq,
            timeout_ms=timeout_ms,
        )

    def query_mode(self, timeout_ms: int = 2000) -> Tuple[int, int]:
        """闭环查询模式 -> (mode_code, filter)。"""
        return self.query(
            self.send_read_mode,
            civresp.parse_mode,
            timeout_ms=timeout_ms,
        )

    def query_smeter(self, timeout_ms: int = 2000) -> int:
        """闭环查询 S-meter -> 原始数据字节 (int)。"""
        return self.query(
            self.send_read_smeter,
            civresp.parse_smeter,
            timeout_ms=timeout_ms,
        )

    def __repr__(self) -> str:
        return (
            f"<CivViaExecCmdSender to=0x{self.to_addr:02X} "
            f"from=0x{self.from_addr:02X} sub_cmd={self.sub_cmd} "
            f"arg3={self.arg3} arg6={self.arg6} client={self._client!r}>"
        )


# ============================================================
# ResponseReader — 可选的响应 Mailslot 读取器
# ============================================================
# 仅当 RemoteController 未运行时, 陆墨可自行创建 RemoteUtyCtrlRes
# mailslot 独占接收响应 (mailslot_server.md §5.4 方案 A)。
# RemoteController 运行时, 该 mailslot 已被 RemoteController 创建,
# 陆墨创建会失败 — 此时只能 fire-and-forget。
# ============================================================


class ResponseReadError(Exception):
    """响应 Mailslot 读取异常。"""


class ResponseTimeoutError(ResponseReadError):
    """响应 Mailslot 读取超时。"""


class ResponseReader:
    """可选的响应 Mailslot 读取器 (创建 RemoteUtyCtrlRes 并读取响应)。

    用途:
        RemoteController 未运行时, 陆墨自行创建 RemoteUtyCtrlRes
        作为 server/reader, 独占接收 RemoteUtility 的响应包。

    限制:
        - RemoteController 运行时, CreateMailslot 会失败 (mailslot 已存在)
        - 仅 Windows 平台可用 (依赖 pywin32 或 ctypes)

    用法:
        with ResponseReader(timeout_ms=2000) as r:
            # 在另一个线程/进程中发送 ExecCmd...
            resp = r.read(timeout_ms=2000)
            if resp:
                print(f"响应: {resp.hex()}")
    """

    def __init__(
        self,
        mailslot_name: str = RESPONSE_MAILSLOT_NAME,
        cb_max_msg: int = RESPONSE_MAILSLOT_CB_MAX_MSG,
        read_timeout_ms: int = 2000,
    ):
        self.mailslot_name = mailslot_name
        self.cb_max_msg = cb_max_msg
        self.default_timeout_ms = read_timeout_ms
        self._handle = None
        self._backend = None

    def open(self) -> None:
        """CreateMailslot 创建响应 mailslot (作为 server/reader)。

        异常:
            ResponseReadError - mailslot 已存在 (RemoteController 运行中) 或创建失败
        """
        if self._handle is not None:
            return
        try:
            import win32file  # type: ignore
            import pywintypes  # type: ignore
            try:
                self._handle = win32file.CreateMailslot(
                    self.mailslot_name,
                    self.cb_max_msg,
                    self.default_timeout_ms,
                    None,
                )
                self._backend = "pywin32"
            except pywintypes.error as e:
                raise ResponseReadError(
                    f"CreateMailslot({self.mailslot_name!r}) 失败: "
                    f"WinError {e.winerror} - {e.strerror}"
                )
        except ImportError:
            self._open_ctypes()

    def _open_ctypes(self) -> None:
        """ctypes 后备路径: kernel32.CreateMailslotW。"""
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMailslotW.restype = wintypes.HANDLE
        kernel32.CreateMailslotW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD,
            wintypes.DWORD, wintypes.LPVOID,
        ]
        handle = kernel32.CreateMailslotW(
            self.mailslot_name, self.cb_max_msg,
            self.default_timeout_ms, None,
        )
        if handle is None or handle == ctypes.c_void_p(-1).value:
            err = ctypes.get_last_error()
            raise ResponseReadError(
                f"CreateMailslotW({self.mailslot_name!r}) 失败: WinError {err}"
            )
        self._handle = handle
        self._backend = "ctypes"

    def close(self) -> None:
        """CloseHandle 关闭 mailslot。幂等。"""
        if self._handle is None:
            return
        try:
            if self._backend == "pywin32":
                import win32file  # type: ignore
                win32file.CloseHandle(self._handle)
            else:
                import ctypes
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle(self._handle)
        except Exception:
            pass
        finally:
            self._handle = None
            self._backend = None

    def read(self, timeout_ms: Optional[int] = None) -> Optional[bytes]:
        """ReadFile 读取一条响应消息 (阻塞直到有消息或超时)。

        参数:
            timeout_ms: 超时 (ms), None 用 default_timeout_ms

        返回:
            响应 bytes, 或 None (超时无消息)

        异常:
            ResponseReadError - 读取失败
        """
        if self._handle is None:
            raise ResponseReadError("ResponseReader 未 open")
        if self._backend == "pywin32":
            return self._read_pywin32(timeout_ms)
        return self._read_ctypes(timeout_ms)

    def _read_pywin32(self, timeout_ms: Optional[int]) -> Optional[bytes]:
        import win32file  # type: ignore
        import pywintypes  # type: ignore

        timeout = timeout_ms if timeout_ms is not None else self.default_timeout_ms
        deadline = time.time() + (timeout / 1000.0) if timeout > 0 else None

        while True:
            try:
                info = win32file.GetMailslotInfo(self._handle)
                msg_size = info[1]
                if msg_size is not None and msg_size != -1 and msg_size > 0:
                    _, data = win32file.ReadFile(self._handle, msg_size)
                    return bytes(data)
            except pywintypes.error as e:
                raise ResponseReadError(
                    f"ReadFile 失败: WinError {e.winerror} - {e.strerror}"
                )
            if deadline is not None and time.time() >= deadline:
                return None
            time.sleep(0.01)

    def _read_ctypes(self, timeout_ms: Optional[int]) -> Optional[bytes]:
        import ctypes
        from ctypes import wintypes

        timeout = timeout_ms if timeout_ms is not None else self.default_timeout_ms
        deadline = time.time() + (timeout / 1000.0) if timeout > 0 else None

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetMailslotInfo.restype = wintypes.BOOL
        kernel32.GetMailslotInfo.argtypes = [
            wintypes.HANDLE, wintypes.LPDWORD,
            wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPDWORD, wintypes.LPVOID,
        ]

        msg_count = wintypes.DWORD(0)
        next_size = wintypes.DWORD(0)
        max_size = wintypes.DWORD(0)
        read_timeout = wintypes.DWORD(0)

        while True:
            ok = kernel32.GetMailslotInfo(
                self._handle, ctypes.byref(max_size),
                ctypes.byref(next_size), ctypes.byref(msg_count),
                ctypes.byref(read_timeout),
            )
            if not ok:
                err = ctypes.get_last_error()
                raise ResponseReadError(f"GetMailslotInfo 失败: WinError {err}")
            if next_size.value != 0xFFFFFFFF and next_size.value > 0:
                buf = (ctypes.c_char * next_size.value)()
                bytes_read = wintypes.DWORD(0)
                ok = kernel32.ReadFile(
                    self._handle, buf, next_size.value,
                    ctypes.byref(bytes_read), None,
                )
                if not ok:
                    err = ctypes.get_last_error()
                    raise ResponseReadError(f"ReadFile 失败: WinError {err}")
                return bytes(buf[:bytes_read.value])
            if deadline is not None and time.time() >= deadline:
                return None
            time.sleep(0.01)

    def __enter__(self) -> "ResponseReader":
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:
        return (
            f"<ResponseReader mailslot={self.mailslot_name!r} "
            f"open={self._handle is not None}>"
        )
