"""protocol — UtyCtrl <-> RemoteUtility Mailslot 命令包序列化.

依据:
    - phase2-re/notes/pe_analysis/UtyCtrl_deep_analysis.md (4.1 命令包结构)
    - UtyCtrl.dll 反汇编: 写入长度 = data_len + 4 (见 0x10001113 处 add eax, 4)

命令包格式 (4 字节定长头 + 变长 payload, 小端):

    偏移  长度  字段        说明
    0     1     cmd_code    命令码 (见 CMD_* 常量表)
    1     1     data_len    payload 字节数 (不含头部 4 字节)
    2     2     reserved    保留 (实测为栈上残留, 通常填 0)
    4     N     payload     N = data_len, 最大 MAX_PAYLOAD_SIZE (255, 1 字节上限)

    总写入字节数 = COMMAND_HEADER_SIZE + data_len = data_len + 4
    (与 UtyCtrl.dll 中 WriteFile 第三参数公式一致)

注意:
    本模块不依赖任何 Windows API, 纯 Python struct 操作, 可跨平台导入
    (用于测试 / 模型验证)。实际 Mailslot 写入见 client.MailslotClient。

命令码常量来源:
    - 0..8 取自 UtyCtrl_deep_analysis.md 4.2 命令码映射表 (静态反汇编)
    - 待沈遥动态逆向最终确认后可覆盖; 当前值已与 9 个导出函数一一对应。
"""

from __future__ import annotations

import struct
from typing import Tuple

__all__ = [
    "COMMAND_HEADER_FORMAT",
    "COMMAND_HEADER_SIZE",
    "MAX_PAYLOAD_SIZE",
    "MAX_PACKET_SIZE",
    "CMD_GET_COUNT_CLIENT_TRANS",
    "CMD_GET_CLIENT_TRANS_INFO",
    "CMD_EXEC_CMD",
    "CMD_GET_CLIENT_TRANS_VOL",
    "CMD_GET_CLIENT_TRANS_INFO_2",
    "CMD_GET_CLIENT_TRANS_VOL_3",
    "CMD_GET_COMMAND_PROC_COUNT",
    "CMD_GET_REMOTE_TRANS_NETWORK_SET",
    "CMD_GET_REMOTE_TRANS_STATE",
    "CMD_CODES",
    "CMD_NAME",
    "EXPECTED_DATA_LEN",
    "serialize_command",
    "deserialize_command",
    "ProtocolError",
    "PayloadTooLargeError",
    "InvalidCommandCodeError",
]

# ============================================================
# 常量
# ============================================================

# struct 格式: < (小端) B (uint8 cmd_code) B (uint8 data_len) H (uint16 reserved)
COMMAND_HEADER_FORMAT = "<BBH"
COMMAND_HEADER_SIZE = struct.calcsize(COMMAND_HEADER_FORMAT)  # = 4

# data_len 字段为单字节, 故 payload 上限 255
MAX_PAYLOAD_SIZE = 255
# 完整包最大字节数 = 头部 4 + payload 255 = 259
MAX_PACKET_SIZE = COMMAND_HEADER_SIZE + MAX_PAYLOAD_SIZE


# ============================================================
# 9 个命令码常量 (UtyCtrl.dll 静态反汇编结果, 待沈遥动态确认)
# ============================================================
# 对应 UtyCtrl.dll 9 个导出函数:
#   # | 名称                       | Ordinal | VA          | cmd_code | data_len
#   1 | GetCountClientTrans        | 7       | 0x100011C0  | 0        | 0
#   2 | GetClientTransInfo         | 3       | 0x10001240  | 1        | 0x6C (108)
#   3 | GetClientTransInfo2         | 2       | 0x100012C0  | 4        | 0x78 (120)
#   4 | GetClientTransVol           | 5       | 0x10001370  | 3        | 0x24 (36)
#   5 | GetClientTransVol3          | 4       | 0x10001430  | 5        | 0x3C (60)
#   6 | GetCommandProcCount         | 6       | 0x100014E0  | 6        | 0
#   7 | GetRemoteTransNetworkSet    | 8       | 0x10001540  | 7        | 0x40 (64)
#   8 | GetRemoteTransState         | 9       | 0x100015F0  | 8        | 0x1C (28)
#   9 | ExecCmd                     | 1       | 0x100016A0  | 2        | 动态 = arg5+0x14
# 注: ExecCmd 是唯一走 fire-and-forget 单向写入路径的命令。

CMD_GET_COUNT_CLIENT_TRANS = 0x00   # 查询客户端传输数量
CMD_GET_CLIENT_TRANS_INFO = 0x01    # 查询传输信息 v1 (resp 108B)
CMD_EXEC_CMD = 0x02                 # 执行控制命令 (单向, 动态长度)
CMD_GET_CLIENT_TRANS_VOL = 0x03     # 查询客户端音量 v1 (resp 36B)
CMD_GET_CLIENT_TRANS_INFO_2 = 0x04  # 查询传输信息 v2 (resp 120B, 扩展)
CMD_GET_CLIENT_TRANS_VOL_3 = 0x05  # 查询客户端音量 v3 (resp 60B)
CMD_GET_COMMAND_PROC_COUNT = 0x06   # 查询命令处理计数
CMD_GET_REMOTE_TRANS_NETWORK_SET = 0x07  # 查询远端网络配置 (含 WLAN 参数)
CMD_GET_REMOTE_TRANS_STATE = 0x08   # 查询远端传输状态 (resp 28B)

# cmd_code -> 函数名 (便于日志/探活报告)
CMD_CODES = {
    CMD_GET_COUNT_CLIENT_TRANS: "GetCountClientTrans",
    CMD_GET_CLIENT_TRANS_INFO: "GetClientTransInfo",
    CMD_EXEC_CMD: "ExecCmd",
    CMD_GET_CLIENT_TRANS_VOL: "GetClientTransVol",
    CMD_GET_CLIENT_TRANS_INFO_2: "GetClientTransInfo2",
    CMD_GET_CLIENT_TRANS_VOL_3: "GetClientTransVol3",
    CMD_GET_COMMAND_PROC_COUNT: "GetCommandProcCount",
    CMD_GET_REMOTE_TRANS_NETWORK_SET: "GetRemoteTransNetworkSet",
    CMD_GET_REMOTE_TRANS_STATE: "GetRemoteTransState",
}

# 反向映射 (函数名 -> cmd_code), 供客户端按名字构造命令
CMD_NAME = {v: k for k, v in CMD_CODES.items()}

# 静态分析推断的预期 data_len (resp payload 大小), 仅供探活/合理性检查参考。
# ExecCmd 长度动态, 故映射为 None; 其它 8 个为 Get* 的预期 resp payload 大小。
EXPECTED_DATA_LEN = {
    CMD_GET_COUNT_CLIENT_TRANS: 0,
    CMD_GET_CLIENT_TRANS_INFO: 0x6C,
    CMD_EXEC_CMD: None,
    CMD_GET_CLIENT_TRANS_VOL: 0x24,
    CMD_GET_CLIENT_TRANS_INFO_2: 0x78,
    CMD_GET_CLIENT_TRANS_VOL_3: 0x3C,
    CMD_GET_COMMAND_PROC_COUNT: 0,
    CMD_GET_REMOTE_TRANS_NETWORK_SET: 0x40,
    CMD_GET_REMOTE_TRANS_STATE: 0x1C,
}


# ============================================================
# 异常类型
# ============================================================

class ProtocolError(Exception):
    """协议层基础异常 (序列化/反序列化错误)。"""


class PayloadTooLargeError(ProtocolError):
    """payload 超过 MAX_PAYLOAD_SIZE (255, data_len 字段单字节上限)。"""


class InvalidCommandCodeError(ProtocolError):
    """cmd_code 超出单字节范围 [0, 255]。"""


# ============================================================
# 序列化 / 反序列化
# ============================================================

def serialize_command(cmd_code: int, payload: bytes = b"",
                      reserved: int = 0) -> bytes:
    """把 (cmd_code, payload) 序列化为 Mailslot 命令包字节流。

    参数:
        cmd_code: 命令码 (0..255)。MVP 不限定必须是已知 CMD_* 之一,
                  允许任意单字节值, 便于逆向探测未知命令。
        payload:  命令负载 (0..255 字节)。None 视作空 bytes。
        reserved:  保留字段 (2 字节, 默认 0)。逆向实测通常为 0,
                  但允许透传非 0 值以便后续动态调试。

    返回:
        bytes, 长度 = COMMAND_HEADER_SIZE + len(payload) = 4 + len(payload)

    异常:
        InvalidCommandCodeError - cmd_code 不在 [0, 255]。
        PayloadTooLargeError     - payload 长度超过 MAX_PAYLOAD_SIZE。
        TypeError                - payload 非 bytes/bytearray。

    依据:
        UtyCtrl.dll 0x10001113: add eax, 4  -> 写入长度 = data_len + 4
        UtyCtrl.dll 0x10001108: movzx eax, byte ptr [ecx+1]  -> data_len 取低字节
    """
    if not isinstance(cmd_code, int):
        raise TypeError(f"cmd_code 必须是 int, 实际 {type(cmd_code).__name__}")
    if cmd_code < 0 or cmd_code > 0xFF:
        raise InvalidCommandCodeError(
            f"cmd_code 必须在 [0, 255], 实际 {cmd_code}"
        )

    if payload is None:
        payload = b""
    elif isinstance(payload, bytearray):
        payload = bytes(payload)
    elif not isinstance(payload, bytes):
        raise TypeError(
            f"payload 必须是 bytes/bytearray, 实际 {type(payload).__name__}"
        )

    data_len = len(payload)
    if data_len > MAX_PAYLOAD_SIZE:
        raise PayloadTooLargeError(
            f"payload 长度 {data_len} 超过上限 {MAX_PAYLOAD_SIZE} "
            f"(data_len 字段为单字节, 受协议格式约束)"
        )

    if not isinstance(reserved, int):
        raise TypeError(f"reserved 必须是 int, 实际 {type(reserved).__name__}")
    reserved &= 0xFFFF

    header = struct.pack(COMMAND_HEADER_FORMAT, cmd_code, data_len, reserved)
    return header + payload


def deserialize_command(data: bytes) -> Tuple[int, int, int, bytes]:
    """反序列化 Mailslot 命令包 (与 serialize_command 互逆)。

    参数:
        data: 命令包字节流, 长度 >= COMMAND_HEADER_SIZE (4)。
              多余字节按 data_len 截断 payload; 若 data 短于 4+data_len,
              payload 取实际可用字节 (容错: 远端可能截断)。

    返回:
        (cmd_code, data_len, reserved, payload)
            cmd_code : int   0..255
            data_len : int   0..255 (头部声明的 payload 长度)
            reserved : int   0..65535
            payload  : bytes 长度 <= data_len (按 data_len 截断)

    异常:
        ProtocolError - data 长度不足 COMMAND_HEADER_SIZE 或非 bytes。

    用途:
        - 测试往返 (serialize -> deserialize 一致性)
        - 远端响应包解析 (响应包 offset 0 = echo cmd_code, 见 client)
        - 抓包/日志解包
    """
    if isinstance(data, bytearray):
        data = bytes(data)
    elif not isinstance(data, bytes):
        raise TypeError(f"data 必须是 bytes/bytearray, 实际 {type(data).__name__}")

    if len(data) < COMMAND_HEADER_SIZE:
        raise ProtocolError(
            f"data 长度 {len(data)} 不足最小包长 {COMMAND_HEADER_SIZE}"
        )

    cmd_code, data_len, reserved = struct.unpack(
        COMMAND_HEADER_FORMAT, data[:COMMAND_HEADER_SIZE]
    )

    payload_end = min(COMMAND_HEADER_SIZE + data_len, len(data))
    payload = data[COMMAND_HEADER_SIZE:payload_end]
    return cmd_code, data_len, reserved, payload
