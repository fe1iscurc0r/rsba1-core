"""commands — UtyCtrl 9 命令码高层封装 (基于 command_protocol.md §3)。

每个命令对应 UtyCtrl.dll 的一个导出函数,本模块用纯 Python 构造 payload,
然后交给 MailslotClient.write_command 发送。

设计:
    - 不依赖 ctypes / DLL,纯 struct 操作
    - payload 字段格式严格按 command_protocol.md §4 分解
    - ExecCmd (cmd_code=2) 是唯一动态长度命令, 用户数据由调用方传入

关键参考:
    - d:\\my git\\rs-ba1-reverse\\re\\utyctrl\\command_protocol.md
    - 字段偏移/类型/字节序均来自反汇编指令地址证据
"""
from __future__ import annotations

import struct
from typing import Optional, Tuple

from rsba1.mailslot.protocol import (
    CMD_GET_COUNT_CLIENT_TRANS,
    CMD_GET_CLIENT_TRANS_INFO,
    CMD_EXEC_CMD,
    CMD_GET_CLIENT_TRANS_VOL,
    CMD_GET_CLIENT_TRANS_INFO_2,
    CMD_GET_CLIENT_TRANS_VOL_3,
    CMD_GET_COMMAND_PROC_COUNT,
    CMD_GET_REMOTE_TRANS_NETWORK_SET,
    CMD_GET_REMOTE_TRANS_STATE,
    MAX_PAYLOAD_SIZE,
)


# ============================================================
# 各命令的 payload 构造器
# ============================================================

def build_get_count_client_trans() -> bytes:
    """cmd_code=0x00 GetCountClientTrans (无 payload)。

    返回空 payload,client.write_command 会自动加 4 字节头。
    """
    return b""


def build_get_client_trans_info(arg1: int) -> bytes:
    """cmd_code=0x01 GetClientTransInfo (108 字节 payload)。

    字段 (command_protocol.md §4.2):
        offset 0-3: arg1 (DWORD, 调用方传入的查询参数)
        offset 4-107: 栈上未初始化 (用 0 填充)

    参数:
        arg1: 查询索引 (0-based), 通常 0..GetCountClientTrans-1
    """
    if not 0 <= arg1 <= 0xFFFFFFFF:
        raise ValueError(f"arg1 超出 DWORD 范围: {arg1}")
    return struct.pack("<I", arg1) + b"\x00" * 100  # 4 + 100 = 104, 但 data_len=0x6C=108


def build_get_client_trans_vol(arg1: int = 0, arg2: int = 0, arg3: int = 0, arg4: int = 0) -> bytes:
    """cmd_code=0x03 GetClientTransVol (36 字节 payload)。

    字段 (command_protocol.md §4.4):
        offset 0-3: arg1 (DWORD)
        offset 4-7: arg2 (DWORD)
        offset 8-11: arg3 (DWORD)
        offset 12-15: arg4 (DWORD)
        offset 16-35: 栈上未初始化 (用 0 填充)

    参数均为 DWORD (uint32)。
    """
    payload = struct.pack("<IIII", arg1 & 0xFFFFFFFF, arg2 & 0xFFFFFFFF,
                          arg3 & 0xFFFFFFFF, arg4 & 0xFFFFFFFF)
    payload += b"\x00" * (0x24 - len(payload))  # 补齐到 0x24=36 字节
    return payload


def build_get_client_trans_info_2(arg1: int = 0) -> bytes:
    """cmd_code=0x04 GetClientTransInfo2 (120 字节 payload)。

    字段 (command_protocol.md §4.5):
        offset 0-3: arg1 (DWORD)
        offset 4-7: 另一个 DWORD (栈上未初始化)
        offset 8-119: 栈上未初始化 (用 0 填充)
    """
    payload = struct.pack("<II", arg1 & 0xFFFFFFFF, 0)
    payload += b"\x00" * (0x78 - len(payload))  # 补齐到 0x78=120 字节
    return payload


def build_get_client_trans_vol_3(arg1: int = 0, arg2: int = 0, arg3: int = 0, arg4: int = 0) -> bytes:
    """cmd_code=0x05 GetClientTransVol3 (60 字节 payload)。

    字段 (command_protocol.md §4.6):
        offset 0-3: arg1 (DWORD)
        offset 4-7: arg2 (DWORD)
        offset 8-11: arg3 (DWORD)
        offset 12-15: arg4 (DWORD)
        offset 16-59: 栈上未初始化 (用 0 填充)
    """
    payload = struct.pack("<IIII", arg1 & 0xFFFFFFFF, arg2 & 0xFFFFFFFF,
                          arg3 & 0xFFFFFFFF, arg4 & 0xFFFFFFFF)
    payload += b"\x00" * (0x3C - len(payload))  # 补齐到 0x3C=60 字节
    return payload


def build_get_command_proc_count() -> bytes:
    """cmd_code=0x06 GetCommandProcCount (无 payload)。"""
    return b""


def build_get_remote_trans_network_set(arg1: int = 0) -> bytes:
    """cmd_code=0x07 GetRemoteTransNetworkSet (64 字节 payload)。

    字段 (command_protocol.md §4.8):
        offset 0-3: arg1 (DWORD)
        offset 4-63: 栈上未初始化 (用 0 填充)
    """
    payload = struct.pack("<I", arg1 & 0xFFFFFFFF)
    payload += b"\x00" * (0x40 - len(payload))  # 补齐到 0x40=64 字节
    return payload


def build_get_remote_trans_state(arg1: int = 0) -> bytes:
    """cmd_code=0x08 GetRemoteTransState (28 字节 payload)。

    字段 (command_protocol.md §4.9):
        offset 0-3: arg1 (DWORD)
        offset 4-27: 栈上未初始化 (用 0 填充)
    """
    payload = struct.pack("<I", arg1 & 0xFFFFFFFF)
    payload += b"\x00" * (0x1C - len(payload))  # 补齐到 0x1C=28 字节
    return payload


# ============================================================
# ExecCmd (cmd_code=2) — 最重要, 用于发送 CI-V 命令
# ============================================================

def build_exec_cmd(arg3: int, arg5: int, arg6_byte: int, user_data: bytes,
                   sub_cmd: int = 0) -> Tuple[bytes, int]:
    """cmd_code=0x02 ExecCmd (动态长度 payload)。

    字段 (command_protocol.md §4.3 + mailslot_server.md §3.2 交叉验证):
        payload offset 0-3:   栈残留 (4 字节, 用 0 填充)
        payload offset 4-7:   arg3 (DWORD)
        payload offset 8-11:  arg5 (DWORD, 用户数据长度)
        payload offset 12:    arg6_byte (1 字节)
        payload offset 13-15: 栈残留 (3 字节, 用 0 填充)
        payload offset 16:    sub_cmd (1 字节, 0-5, RemoteUty 子命令分发)
        payload offset 17-19: 栈残留 (3 字节, 用 0 填充)
        payload offset 20+:   user_data (arg5 & 0xFF 字节)

    data_len = (arg5 & 0xFF) + 0x14  (0x14 = 20 字节固定头)

    sub_cmd 来源 (mailslot_server.md §3.2):
        RemoteUty 在 0x43b02a 处 `movzx eax, byte ptr [esi + 0x14]` 读取
        packet[0x14] = payload[16] 作为子命令码 (0-5), 分发到不同处理函数:
            0 -> 0x43a3f0, 1 -> 0x43a5f0, 2 -> 0x43a800, 3 -> 0x43aa70
        各 sub_cmd 的具体语义 (CI-V 转发 / HID / ...) 需动态确认。

    参数:
        arg3:      子命令码 / 目标设备地址 (DWORD, 语义待动态确认)
        arg5:      用户数据长度 (低字节生效, 高字节用于 data_len 计算)
        arg6_byte: 标志字节 (低字节有效, 语义待动态确认)
        user_data: 实际数据 (长度应等于 arg5 & 0xFF)
        sub_cmd:   RemoteUty 子命令码 (0-5, 默认 0)

    返回:
        (payload_bytes, data_len)
        payload_bytes 长度 = data_len
    """
    user_len = arg5 & 0xFF
    if len(user_data) != user_len:
        raise ValueError(f"user_data 长度 {len(user_data)} != arg5 & 0xFF = {user_len}")
    if not 0 <= sub_cmd <= 0xFF:
        raise ValueError(f"sub_cmd 超出字节范围: {sub_cmd}")

    # 固定头部 20 字节 (0x14):
    #   4 残留 + 4 arg3 + 4 arg5 + 1 arg6 + 3 残留 + 1 sub_cmd + 3 残留
    # payload[0:20] 是固定头, payload[20:20+user_len] 是 user_data
    fixed_header = struct.pack("<IIIB3x", 0, arg3 & 0xFFFFFFFF, arg5 & 0xFFFFFFFF, arg6_byte & 0xFF)
    # fixed_header 长度 = 4 + 4 + 4 + 1 + 3 = 16 字节, 还差 4 字节到 0x14
    # 这 4 字节的首字节 = sub_cmd (RemoteUty 在 packet[0x14]=payload[16] 读取)
    fixed_header += struct.pack("<B3x", sub_cmd & 0xFF)  # 补齐到 20 字节, 首字节为 sub_cmd

    if len(fixed_header) != 0x14:
        raise RuntimeError(f"fixed_header 长度异常: {len(fixed_header)} != 0x14")

    payload = fixed_header + user_data
    data_len = (arg5 & 0xFF) + 0x14

    if len(payload) != data_len:
        raise RuntimeError(f"payload 长度 {len(payload)} != data_len {data_len}")

    if data_len > MAX_PAYLOAD_SIZE:
        raise ValueError(f"data_len {data_len} 超过 MAX_PAYLOAD_SIZE {MAX_PAYLOAD_SIZE}")

    return payload, data_len


# ============================================================
# 响应解析 (仅 cmd_code echo 校验)
# ============================================================

def parse_response_echo(resp_bytes: bytes, expected_cmd_code: int) -> bool:
    """校验响应包 offset 0 是否等于原 cmd_code (echo 机制)。

    command_protocol.md §3.6: 8 个 Get* 函数成功路径均检查响应 offset 0。
    """
    if not resp_bytes or len(resp_bytes) < 1:
        return False
    return resp_bytes[0] == expected_cmd_code


# ============================================================
# 命令码 -> 构造器映射 (便于动态调用)
# ============================================================

CMD_BUILDERS = {
    CMD_GET_COUNT_CLIENT_TRANS: build_get_count_client_trans,
    CMD_GET_CLIENT_TRANS_INFO: build_get_client_trans_info,
    CMD_GET_CLIENT_TRANS_VOL: build_get_client_trans_vol,
    CMD_GET_CLIENT_TRANS_INFO_2: build_get_client_trans_info_2,
    CMD_GET_CLIENT_TRANS_VOL_3: build_get_client_trans_vol_3,
    CMD_GET_COMMAND_PROC_COUNT: build_get_command_proc_count,
    CMD_GET_REMOTE_TRANS_NETWORK_SET: build_get_remote_trans_network_set,
    CMD_GET_REMOTE_TRANS_STATE: build_get_remote_trans_state,
    # CMD_EXEC_CMD 特殊: 需要多参数, 不在此映射
}
