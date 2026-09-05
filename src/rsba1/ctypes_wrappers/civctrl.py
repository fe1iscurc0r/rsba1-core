"""ctypes 包装层: CivCtrl.dll (Icom RS-BA1 V2 CI-V 串口控制 DLL).

依据:
    - re/civctrl/exports.md         (18 个 __stdcall 导出函数 C 原型)
    - re/civctrl/structures.h       (CIVDriver 0xAC9 字节对象布局)
    - re/civctrl/call_sequence.md   (Open->Send->Recv->Close 调用链)
    - phase2-re/notes/pe_analysis/CivCtrl_deep_analysis.md (CI-V 帧构造)

调用约定:
    所有 civXXX 导出均为 __stdcall (Borland C++ 2005, 通过 .def 导出, 名字无修饰),
    使用 ctypes.WinDLL 加载。第 18 个导出 ___CPPdebugHook 位于 .data 段,
    属于 Borland 调试符号占位 (非可调用代码), 本包装不绑定该符号。

句柄模型:
    civOpen 的第一个参数 hSlot 是调用方选取的"对象 ID" (整数即可), DLL 内部
    HandleResolver 维护 hSlot -> CIVDriver* 映射表。后续 civXXX 调用均以该 hSlot
    作为 opaque handle。civOpen 返回值为 int (非 0 = 成功), 不是对象指针。
    本包装在高层 civOpen() 中自动分配 hSlot ID 并将其作为 handle 返回给调用方,
    同时记录到 self._handle 供上下文管理器自动 civClose。
"""

from __future__ import annotations

import os
import sys
import time
import ctypes
from ctypes import (
    c_void_p,
    c_int,
    c_uint8,
    c_uint16,
    c_char_p,
    POINTER,
    byref,
    create_string_buffer,
)

# WinDLL 是 Windows-only 的 ctypes 加载器; 非 Windows 平台导入安全,
# 但任何真正的 DLL 加载都会抛出明确的 CivCtrlLoadError。
# (rsba1 主路径 radio_link/mcp 均为纯 Python UDP, 不依赖本模块。)
if sys.platform == "win32":
    from ctypes import WinDLL
else:
    WinDLL = None  # type: ignore[assignment,misc]

__all__ = [
    "CivCtrlError",
    "CivCtrlLoadError",
    "CivCtrlHandleError",
    "CivCtrlStateError",
    "CivCtrlTimeoutError",
    "CivCtrlDLL",
    "DEFAULT_CIVCTRL_DLL_PATH",
]

# CivCtrl.dll 默认路径 (RS-BA1 V2 安装目录下)
DEFAULT_CIVCTRL_DLL_PATH = r"d:\my git\RS-BA1\RemoteController\CivCtrl.dll"


# ============================================================
# 异常类型
# ============================================================

class CivCtrlError(Exception):
    """CivCtrl 包装层基础异常。"""


class CivCtrlLoadError(CivCtrlError):
    """DLL 加载失败 (文件不存在 / WinDLL 加载错误)。"""


class CivCtrlHandleError(CivCtrlError):
    """句柄无效 (None 或未打开)。"""


class CivCtrlStateError(CivCtrlError):
    """状态机非 IDLE 时尝试发送 (civIsSendEnable 返回 0)。"""


class CivCtrlTimeoutError(CivCtrlError):
    """接收轮询超时 (send_and_wait 在 timeout_ms 内未收到应答)。"""


# ============================================================
# CivCtrlDLL — 单例 ctypes 包装
# ============================================================

class CivCtrlDLL:
    """CivCtrl.dll 的 ctypes 包装 (单例)。

    提供 17 个可调用导出函数的高层 Python 方法 + send_and_wait 便捷方法 +
    上下文管理器支持。

    用法 (典型):
        with CivCtrlDLL() as civ:                  # 加载 DLL
            h = civ.civOpen(3, 19200, 0, 0)         # COM3 @ 19200 8N1
            civ.civSetAddress(h, 0xA4, 0x00)        # IC-705 / 控制器 0x00
            payload, flag = civ.send_and_wait(h, b"\\xa4\\x00\\x03", 500)
            civ.civClose(h)                         # 显式关闭 (可选)

    上下文退出时会自动关闭 civOpen 记录的 handle (self._handle)。
    """

    _instance = None  # 单例缓存

    # 17 个可调用导出函数名 (按 exports.md 顺序; ___CPPdebugHook 为数据符号, 不在此列)
    _CALLABLE_EXPORTS = (
        "civOpen", "civSetAddress", "civSetAddPreamble", "civSetCivTot",
        "civClose", "civSend", "civGetRecvSize", "civRecv",
        "civIsSendEnable", "civSetRetryFA", "civSetWaitTime",
        "civResetOthAnsCount", "civGetOthAnsCount",
        "civResetRxByteCount", "civGetRxByteCount",
        "civSetConType", "civGetConType",
    )

    def __init__(self, dll_path=None, *, dll=None):
        """加载 CivCtrl.dll 并配置 17 个函数原型。

        参数:
            dll_path: DLL 文件路径; None 时使用 DEFAULT_CIVCTRL_DLL_PATH。
            dll:      已加载的 WinDLL/Mock 对象; 不为 None 时跳过文件加载
                      (供单元测试注入 mock)。
        """
        if dll is not None:
            self.dll = dll
        else:
            if WinDLL is None:
                raise CivCtrlLoadError(
                    "CivCtrl.dll 仅支持 Windows (ctypes.WinDLL 在本平台不可用), "
                    "Linux/macOS 请使用纯 Python 的 radio-link 后端"
                )
            path = dll_path or DEFAULT_CIVCTRL_DLL_PATH
            if not os.path.exists(path):
                raise CivCtrlLoadError(f"CivCtrl.dll 不存在: {path}")
            try:
                self.dll = WinDLL(path)
            except OSError as e:
                raise CivCtrlLoadError(f"加载 CivCtrl.dll 失败: {e}") from e
        self._configure_prototypes()
        self._handle = None        # civOpen 记录的 handle, 供上下文管理器自动关闭
        self._next_slot_id = 1     # hSlot ID 分配计数器

    # ---------- 单例 ----------

    @classmethod
    def instance(cls, dll_path=None):
        """返回单例 (首次调用时加载 DLL)。"""
        if cls._instance is None:
            cls._instance = cls(dll_path)
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """清除单例缓存 (测试用)。"""
        cls._instance = None

    # ---------- 原型配置 ----------

    def _configure_prototypes(self):
        """严格按 exports.md 为 17 个导出函数设置 restype / argtypes。

        第 18 个导出 ___CPPdebugHook 位于 .data 段 (VA 0x41C0F8), 是 Borland
        调试符号占位, 非可调用代码, 故不绑定。
        """
        d = self.dll

        # 1. int civOpen(void* hSlot, int comPort, int baudRate, BYTE dtr, BYTE rts)
        d.civOpen.restype = c_int
        d.civOpen.argtypes = [c_void_p, c_int, c_int, c_uint8, c_uint8]

        # 2. void civSetAddress(void* h, BYTE toAddr, BYTE fromAddr)
        d.civSetAddress.restype = None
        d.civSetAddress.argtypes = [c_void_p, c_uint8, c_uint8]

        # 3. void civSetAddPreamble(void* h, WORD count)
        d.civSetAddPreamble.restype = None
        d.civSetAddPreamble.argtypes = [c_void_p, c_uint16]

        # 4. void civSetCivTot(void* h, WORD tot)
        d.civSetCivTot.restype = None
        d.civSetCivTot.argtypes = [c_void_p, c_uint16]

        # 5. void civClose(void* h)
        d.civClose.restype = None
        d.civClose.argtypes = [c_void_p]

        # 6. void civSend(void* h, const void* data, int len, BYTE flag)
        #    data 按 task 要求用 c_char_p 接收 bytes
        d.civSend.restype = None
        d.civSend.argtypes = [c_void_p, c_char_p, c_int, c_uint8]

        # 7. int civGetRecvSize(void* h)
        d.civGetRecvSize.restype = c_int
        d.civGetRecvSize.argtypes = [c_void_p]

        # 8. int civRecv(void* h, void* buf, int* size, BYTE* flag)
        d.civRecv.restype = c_int
        d.civRecv.argtypes = [c_void_p, c_void_p, POINTER(c_int), POINTER(c_uint8)]

        # 9. int civIsSendEnable(void* h)
        d.civIsSendEnable.restype = c_int
        d.civIsSendEnable.argtypes = [c_void_p]

        # 10. void civSetRetryFA(void* h, BYTE flag)
        d.civSetRetryFA.restype = None
        d.civSetRetryFA.argtypes = [c_void_p, c_uint8]

        # 11. void civSetWaitTime(void* h, int ms)
        d.civSetWaitTime.restype = None
        d.civSetWaitTime.argtypes = [c_void_p, c_int]

        # 12. void civResetOthAnsCount(void* h)
        d.civResetOthAnsCount.restype = None
        d.civResetOthAnsCount.argtypes = [c_void_p]

        # 13. int civGetOthAnsCount(void* h)
        d.civGetOthAnsCount.restype = c_int
        d.civGetOthAnsCount.argtypes = [c_void_p]

        # 14. void civResetRxByteCount(void* h)
        d.civResetRxByteCount.restype = None
        d.civResetRxByteCount.argtypes = [c_void_p]

        # 15. int civGetRxByteCount(void* h)
        d.civGetRxByteCount.restype = c_int
        d.civGetRxByteCount.argtypes = [c_void_p]

        # 16. void civSetConType(void* h, BYTE type)
        d.civSetConType.restype = None
        d.civSetConType.argtypes = [c_void_p, c_uint8]

        # 17. int civGetConType(void* h)
        d.civGetConType.restype = c_int
        d.civGetConType.argtypes = [c_void_p]

    # ---------- 内部辅助 ----------

    def _require_handle(self, handle):
        """校验 handle 非 None (避免把 None 传给 DLL)。"""
        if handle is None:
            raise CivCtrlHandleError("handle is None (未 civOpen 或已关闭)")

    def _alloc_slot(self):
        """分配一个新的 hSlot ID (调用方选取的对象 ID)。"""
        slot = self._next_slot_id
        self._next_slot_id += 1
        return slot

    # ============================================================
    # 高层 Python 包装方法 (17 个导出 + 便捷方法)
    # ============================================================

    # --- 1. civOpen ---
    def civOpen(self, comPort, baud, dtr=0, rts=0):
        """打开 CI-V 设备: 分配对象 + 打开 COM + 启动 SendRecvThread。

        参数:
            comPort: COM 端口号 (1 = COM1, 3 = COM3, ...)
            baud:    波特率 (9600 / 19200 / ...)
            dtr:     DTR 模式 (0=DISABLE, 1=ENABLE, 2=HANDSHAKE)
            rts:     RTS 模式 (0=DISABLE, 1=ENABLE, 2=HANDSHAKE, 3=TOGGLE)

        返回:
            opaque handle (即内部 hSlot ID), 供后续 civXXX 调用使用。

        异常:
            CivCtrlError - DLL 返回 0 (打开失败)。
        """
        hSlot = self._alloc_slot()
        ok = self.dll.civOpen(hSlot, int(comPort), int(baud), dtr & 0xFF, rts & 0xFF)
        if not ok:
            raise CivCtrlError(
                f"civOpen 失败 (返回 {ok}): COM{comPort} @ {baud} baud"
            )
        self._handle = hSlot  # 记录供上下文管理器自动关闭
        return hSlot

    # --- 2. civSetAddress ---
    def civSetAddress(self, handle, toAddr, fromAddr):
        """设置 CI-V 帧目标/源地址 (写入 CIVDriver+0x10/+0x11)。

        参数:
            toAddr:   目标电台地址 (如 IC-705=0xA4, IC-7300=0x04)
            fromAddr: 源控制器地址 (通常 0xE0; RS-BA1 中常用 0x00)
        """
        self._require_handle(handle)
        self.dll.civSetAddress(handle, toAddr & 0xFF, fromAddr & 0xFF)

    # --- 3. civSetAddPreamble ---
    def civSetAddPreamble(self, handle, count):
        """设置额外前导 0xFE 字节数 (实际帧前导 = count + 2)。"""
        self._require_handle(handle)
        self.dll.civSetAddPreamble(handle, count & 0xFFFF)

    # --- 4. civSetCivTot ---
    def civSetCivTot(self, handle, tot):
        """设置 CIVTOT 总超时 (毫秒, 默认 15000)。"""
        self._require_handle(handle)
        self.dll.civSetCivTot(handle, tot & 0xFFFF)

    # --- 5. civClose ---
    def civClose(self, handle):
        """关闭设备: 停线程 + 关串口/mailslot + 释放对象。"""
        if handle is None:
            return
        self.dll.civClose(handle)
        if handle == self._handle:
            self._handle = None

    # --- 6. civSend ---
    def civSend(self, handle, data, flag=0):
        """发送 CI-V 命令 (异步: 写 mailslot -> 后台线程组帧 + 写串口)。

        参数:
            data: 命令体 bytes (不含 FE 前导 / FD 尾; DLL 自动包装)。
                  内容通常为 [toAddr, fromAddr, cmd, ...]。会被转成 c_char_p。
            flag: 命令标志 (0=普通, 透传至消息)。
        """
        self._require_handle(handle)
        if data is None:
            data_bytes = b""
        elif isinstance(data, (bytes, bytearray)):
            data_bytes = bytes(data)
        else:
            raise TypeError(f"data 必须是 bytes/bytearray, 实际 {type(data).__name__}")
        # 把 data 转 c_char_p (task 要求)
        data_p = c_char_p(data_bytes)
        self.dll.civSend(handle, data_p, len(data_bytes), flag & 0xFF)

    # --- 7. civGetRecvSize ---
    def civGetRecvSize(self, handle):
        """返回接收缓冲区待读数据长度 (0 = 无数据)。"""
        self._require_handle(handle)
        return int(self.dll.civGetRecvSize(handle))

    # --- 8. civRecv ---
    def civRecv(self, handle, buf_size=1024):
        """读取一条接收消息 (从 civrecv mailslot)。

        参数:
            buf_size: 用户缓冲区容量 (至少 4 字节存放 msgId)。

        返回:
            (payload_bytes, flag) -
                payload_bytes: CI-V 应答数据 (buf[4:4+dataLen], 不含 msgId/FE/FD)。
                flag:          消息关联标志 (透传自 civSend)。

        异常:
            CivCtrlError - DLL 返回负值 (读取失败)。
        """
        self._require_handle(handle)
        if buf_size < 4:
            buf_size = 4
        buf = create_string_buffer(buf_size)
        size = c_int(buf_size)
        flag = c_uint8(0)
        data_len = self.dll.civRecv(handle, buf, byref(size), byref(flag))
        if data_len < 0:
            raise CivCtrlError(f"civRecv 失败 (返回 {data_len})")
        # buf 布局: [msgId(4)][data(data_len)]; 防御性截断
        end = min(4 + data_len, buf_size)
        payload = bytes(buf[4:end])
        return payload, int(flag.value)

    # --- 9. civIsSendEnable ---
    def civIsSendEnable(self, handle):
        """查询发送是否可用 (状态机 == IDLE)。"""
        self._require_handle(handle)
        return bool(self.dll.civIsSendEnable(handle))

    # --- 10. civSetRetryFA ---
    def civSetRetryFA(self, handle, flag):
        """设置重试失败允许标志 (控制 JAM->civRetry 行为)。"""
        self._require_handle(handle)
        self.dll.civSetRetryFA(handle, flag & 0xFF)

    # --- 11. civSetWaitTime ---
    def civSetWaitTime(self, handle, ms):
        """设置 ECHO/ANSWER 等待时间 (毫秒, 覆盖默认 500ms)。"""
        self._require_handle(handle)
        self.dll.civSetWaitTime(handle, int(ms))

    # --- 12. civResetOthAnsCount ---
    def civResetOthAnsCount(self, handle):
        """清零"其他应答计数器" (transceive 模式旁路应答数)。"""
        self._require_handle(handle)
        self.dll.civResetOthAnsCount(handle)

    # --- 13. civGetOthAnsCount ---
    def civGetOthAnsCount(self, handle):
        """获取"其他应答计数器"值。"""
        self._require_handle(handle)
        return int(self.dll.civGetOthAnsCount(handle))

    # --- 14. civResetRxByteCount ---
    def civResetRxByteCount(self, handle):
        """清零接收字节累计计数器。"""
        self._require_handle(handle)
        self.dll.civResetRxByteCount(handle)

    # --- 15. civGetRxByteCount ---
    def civGetRxByteCount(self, handle):
        """获取接收字节累计计数器值。"""
        self._require_handle(handle)
        return int(self.dll.civGetRxByteCount(handle))

    # --- 16. civSetConType ---
    def civSetConType(self, handle, type_):
        """设置连接类型 (0=本地串口 / 1=远程 / 2=USB?)。"""
        self._require_handle(handle)
        self.dll.civSetConType(handle, type_ & 0xFF)

    # --- 17. civGetConType ---
    def civGetConType(self, handle):
        """获取当前连接类型。"""
        self._require_handle(handle)
        return int(self.dll.civGetConType(handle))

    # ============================================================
    # 便捷方法
    # ============================================================

    def send_and_wait(self, handle, data, timeout_ms=500, flag=0):
        """发送 CI-V 命令并轮询等待应答 (简化同步语义)。

        流程:
            1. 检查 civIsSendEnable(handle) - 非 IDLE 抛 CivCtrlStateError。
            2. civSend(handle, data, flag) - 异步写入 mailslot。
            3. 轮询 civGetRecvSize(handle) 直到 > 0 或超过 timeout_ms。
            4. 有数据则 civRecv(handle) 返回 (payload, flag)。
            5. 超时则抛 CivCtrlTimeoutError。

        参数:
            data:       命令体 bytes (不含 FE/FD 包装)。
            timeout_ms: 等待应答超时 (毫秒)。
            flag:       透传给 civSend 的命令标志。

        返回:
            (payload_bytes, recv_flag) - civRecv 的返回值。

        异常:
            CivCtrlStateError    - 状态机非 IDLE。
            CivCtrlTimeoutError  - 超时未收到应答。
        """
        self._require_handle(handle)
        if not self.civIsSendEnable(handle):
            raise CivCtrlStateError("无法发送: 状态机非 IDLE (civIsSendEnable=0)")
        self.civSend(handle, data, flag)
        deadline = time.perf_counter() + timeout_ms / 1000.0
        while time.perf_counter() < deadline:
            if self.civGetRecvSize(handle) > 0:
                return self.civRecv(handle)
            time.sleep(0.001)  # 1ms 轮询间隔, 降低 CPU 占用
        raise CivCtrlTimeoutError(f"在 {timeout_ms}ms 内未收到应答")

    def close(self):
        """关闭 civOpen 记录的 handle (供上下文管理器自动调用)。"""
        if self._handle is not None:
            try:
                self.dll.civClose(self._handle)
            finally:
                self._handle = None

    # ---------- 上下文管理器 ----------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False  # 不吞异常

    def __repr__(self):
        return f"<CivCtrlDLL handle={self._handle!r}>"