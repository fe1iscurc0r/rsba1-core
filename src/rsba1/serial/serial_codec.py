"""serial_codec — Serial 信道 (UDP 50002) 的 wire 头 + Serial 帧编解码 (纯代码层).

对齐 main 权威 (a854a0d 线上验证) 与 Command 信道 (aa50911) 统一为 LE 字节序,
详见 re/protocols/serial_channel.md §5.10 (2026-08-12 定案)。

UDP 包 = UDP2 wire 头 (0x10 字节) + payload (Serial 帧):

    +--------+------+------+----------+------------------------------+
    | offset | size | LE/BE| 字段      | 语义                          |
    +--------+------+------+----------+------------------------------+
    | 0x00   | 4    | LE   | totalLen | = 0x10 + payload 长度         |
    | 0x04   | 2    | LE   | type     | 0x00/0x01/0x03/0x04/0x06/0x07 |
    | 0x06   | 2    | LE   | seq      | CUDPCtrl2 双工独立序号         |
    | 0x08   | 4    | LE   | field_8  | 本地会话标识 localSID         |
    | 0x0C   | 4    | LE   | field_C  | 对端会话标识 remoteSID        |
    | 0x10   | N    | -    | payload  | Serial 帧                    |
    +--------+------+------+----------+------------------------------+

⚠️ 字节序 (2026-08-12 定案): field_8/field_C 统一为 LE, 与 totalLen/type/seq 一致
(整头 "<IHHII"), 对齐 main 权威与 Command 信道。此前曾依据 kappanhang 判为 BE,
现回退; 存疑说明见 serial_channel.md §5.10。

Serial 帧 (5 + 数据):

    +--------+------+------+----------+----------------------+
    | offset | size | LE/BE| 字段      | 语义                  |
    +--------+------+------+----------+----------------------+
    | 0x00   | 1    | -    | flags    | 0xC0 | bit0            |
    |        |      |      |          | bit0=1 批量 / 0 单字节 |
    | 0x01   | 2    | LE   | frameLen | 载荷字节数 (=total-5)  |
    | 0x03   | 2    | BE   | sseq     | Serial 层递增序号      |
    | 0x05   | N    | -    | payload  | CI-V 帧 / 单字节控制   |
    +--------+------+------+----------+----------------------+

参考:
    - re/protocols/serial_channel.md §2.2 / §2.3 / §5
    - src/rsba1/ctypes_wrappers/civ_commands.py (CI-V 帧构造)
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Tuple

__all__ = [
    # 结构常量
    "WIRE_HEADER_SIZE",
    "SERIAL_FRAME_HEADER_SIZE",
    "UDP2_PKT_TYPE_DATA",
    "UDP2_PKT_TYPE_RETRANSMIT",
    "UDP2_PKT_TYPE_PKT3",
    "UDP2_PKT_TYPE_PKT4",
    "UDP2_PKT_TYPE_PKT6",
    "UDP2_PKT_TYPE_KEEPALIVE",
    "SERIAL_FLAGS_BASE",
    "SERIAL_FLAGS_BULK",
    "SERIAL_FLAGS_SINGLE",
    # 类型
    "UDP2WireHeader",
    "SerialFrame",
    # 函数
    "build_wire_header",
    "parse_wire_header",
    "build_serial_frame",
    "parse_serial_frame",
    "build_udp_packet",
    "parse_udp_packet",
]


# ============================================================
# 结构常量 (线上确证)
# ============================================================

WIRE_HEADER_SIZE = 0x10          # UDP2 wire 头固定 16 字节
SERIAL_FRAME_HEADER_SIZE = 5     # Serial 帧头 5 字节 (flags+len+sseq)

# UDP2 包类型 (kappanhang 确证全集, 见 streamcommon.go / pkt0.go / pkt7.go)
# 此前"仅 2 种"的结论不完整: 除数据/心跳外, 还有传输层握手 pkt3/4/6 与重传请求。
UDP2_PKT_TYPE_DATA = 0x00        # 数据包 / pkt0 idle (CI-V 透传 / 空闲心跳)
UDP2_PKT_TYPE_RETRANSMIT = 0x01  # pkt1 重传请求 (单包 / 区间)
UDP2_PKT_TYPE_PKT3 = 0x03        # pkt3 会话握手 (发送方 localSID)
UDP2_PKT_TYPE_PKT4 = 0x04        # pkt4 会话握手应答 (回传 remoteSID)
UDP2_PKT_TYPE_PKT6 = 0x06        # pkt6 会话握手确认
UDP2_PKT_TYPE_KEEPALIVE = 0x07   # pkt7 keepalive 心跳

# Serial 帧 flags
SERIAL_FLAGS_BASE = 0xC0         # 基础帧头标志
SERIAL_FLAGS_BULK = 0x01         # bit0=1 → 批量 CI-V 数据
SERIAL_FLAGS_SINGLE = 0x00       # bit0=0 → 单字节控制/状态


# ============================================================
# 数据结构
# ============================================================

@dataclass(frozen=True)
class UDP2WireHeader:
    """UDP2 wire 头 (0x10 字节, kappanhang 确证布局).

    字段:
        type:    包类型 (UDP2_PKT_TYPE_*, 见模块常量)
        seq:     CUDPCtrl2 序号 (LE uint16, 双工独立递增)
        field_8: 本地会话标识 localSID (LE uint32, 整个会话恒定)
        field_C: 对端会话标识 remoteSID (LE uint32, 握手后确立)
    """
    type: int = UDP2_PKT_TYPE_DATA
    seq: int = 0
    field_8: int = 0
    field_C: int = 0

    def __post_init__(self) -> None:
        # 冻结 dataclass 仍需校验范围 (frozen 下用 object.__setattr__)
        for name, upper in (("type", 0xFFFF), ("seq", 0xFFFF),
                            ("field_8", 0xFFFFFFFF), ("field_C", 0xFFFFFFFF)):
            val = getattr(self, name)
            if not (0 <= val <= upper):
                raise ValueError(f"{name} 超出范围 (0x{0:X}..0x{upper:X}): 0x{val:X}")


@dataclass(frozen=True)
class SerialFrame:
    """Serial 帧 (5 + 数据).

    字段:
        bulk:    True=批量 CI-V 数据 (flags bit0=1), False=单字节控制
        sseq:    Serial 层递增序号 (BE uint16)
        payload: 载荷 bytes (CI-V 帧 / 单字节控制数据)
    """
    bulk: bool = True
    sseq: int = 0
    payload: bytes = b""

    def __post_init__(self) -> None:
        if not (0 <= self.sseq <= 0xFFFF):
            raise ValueError(f"sseq 超出范围 (0..0xFFFF): 0x{self.sseq:X}")
        if not isinstance(self.payload, (bytes, bytearray)):
            raise TypeError(f"payload 必须是 bytes, 实际 {type(self.payload).__name__}")
        object.__setattr__(self, "payload", bytes(self.payload))


# ============================================================
# wire 头编解码
# ============================================================

def build_wire_header(
    type: int = UDP2_PKT_TYPE_DATA,
    seq: int = 0,
    field_8: int = 0,
    field_C: int = 0,
    payload_len: int = 0,
) -> bytes:
    """构造 UDP2 wire 头 bytes (0x10 字节).

    参数:
        type:        包类型 (默认数据包)
        seq:         CUDP2 序号 (LE uint16)
        field_8:     本地会话标识 localSID (LE uint32)
        field_C:     对端会话标识 remoteSID (LE uint32)
        payload_len: 后续 payload 长度 → totalLen = 0x10 + payload_len

    返回:
        wire 头 bytes (16 字节)。
    """
    hdr = UDP2WireHeader(type=type, seq=seq, field_8=field_8, field_C=field_C)
    total_len = WIRE_HEADER_SIZE + int(payload_len)
    # 整头全 LE: totalLen(LE dword) type(LE word) seq(LE word) f8(LE dword) fc(LE dword)
    return struct.pack(
        "<IHHII",
        total_len,
        hdr.type,
        hdr.seq,
        hdr.field_8,
        hdr.field_C,
    )


def parse_wire_header(data: bytes) -> Tuple[UDP2WireHeader, int]:
    """解析 UDP2 wire 头.

    参数:
        data: 至少 0x10 字节的 UDP 包前部.

    返回:
        (UDP2WireHeader, total_len): 解析出的 wire 头与报文总长度.

    异常:
        ValueError - data 长度不足 0x10.
    """
    if len(data) < WIRE_HEADER_SIZE:
        raise ValueError(
            f"wire 头长度不足: 需要 {WIRE_HEADER_SIZE}, 实际 {len(data)}"
        )
    total_len, type_, seq, f8, fc = struct.unpack("<IHHII", data[:WIRE_HEADER_SIZE])
    return UDP2WireHeader(type=type_, seq=seq, field_8=f8, field_C=fc), total_len


# ============================================================
# Serial 帧编解码
# ============================================================

def build_serial_frame(
    payload: bytes,
    sseq: int = 0,
    bulk: bool = True,
) -> bytes:
    """构造 Serial 帧 bytes (5 + len(payload)).

    参数:
        payload: 载荷 (CI-V 帧 / 单字节控制数据)
        sseq:    Serial 层递增序号 (BE uint16)
        bulk:    True=批量 (flags bit0=1), False=单字节 (bit0=0)

    返回:
        Serial 帧 bytes.
    """
    frame = SerialFrame(bulk=bulk, sseq=sseq, payload=payload)
    flags = SERIAL_FLAGS_BASE | (SERIAL_FLAGS_BULK if frame.bulk else SERIAL_FLAGS_SINGLE)
    frame_len = len(frame.payload)
    # flags(1) + frameLen(LE word) + sseq(BE word) + payload
    head = struct.pack("<BH", flags, frame_len) + struct.pack(">H", frame.sseq)
    return head + frame.payload


def parse_serial_frame(data: bytes) -> SerialFrame:
    """解析 Serial 帧.

    参数:
        data: 至少 5 字节的 Serial 帧 (flags+len+sseq+payload).

    返回:
        SerialFrame.

    异常:
        ValueError - data 长度不足 5 或 frameLen 超出实际长度.
    """
    if len(data) < SERIAL_FRAME_HEADER_SIZE:
        raise ValueError(
            f"Serial 帧长度不足: 需要 {SERIAL_FRAME_HEADER_SIZE}, 实际 {len(data)}"
        )
    flags = data[0]
    frame_len = struct.unpack("<H", data[1:3])[0]
    sseq = struct.unpack(">H", data[3:5])[0]
    payload = data[SERIAL_FRAME_HEADER_SIZE:]
    if frame_len != len(payload):
        raise ValueError(
            f"frameLen 与 payload 长度不符: 声明 {frame_len}, 实际 {len(payload)}"
        )
    bulk = bool(flags & SERIAL_FLAGS_BULK)
    return SerialFrame(bulk=bulk, sseq=sseq, payload=payload)


# ============================================================
# 完整 UDP 包编解码 (wire 头 + Serial 帧)
# ============================================================

def build_udp_packet(
    payload: bytes,
    sseq: int = 0,
    bulk: bool = True,
    seq: int = 0,
    field_8: int = 0,
    field_C: int = 0,
    type: int = UDP2_PKT_TYPE_DATA,
) -> bytes:
    """构造完整 UDP 包 (wire 头 + Serial 帧).

    参数:
        payload: CI-V 帧 / 单字节控制数据 (Serial 帧载荷)
        sseq:    Serial 层递增序号 (BE uint16)
        bulk:    True=批量 CI-V, False=单字节控制
        seq:     CUDP2 序号 (LE uint16)
        field_8: 本地会话标识 localSID (LE uint32)
        field_C: 对端会话标识 remoteSID (LE uint32)
        type:    包类型 (默认数据包)

    返回:
        完整 UDP 包 bytes.
    """
    frame = build_serial_frame(payload, sseq=sseq, bulk=bulk)
    wire = build_wire_header(
        type=type, seq=seq, field_8=field_8, field_C=field_C,
        payload_len=len(frame),
    )
    return wire + frame


def parse_udp_packet(data: bytes) -> Tuple[UDP2WireHeader, SerialFrame]:
    """解析完整 UDP 包 (wire 头 + Serial 帧).

    参数:
        data: 完整 UDP 包 bytes.

    返回:
        (UDP2WireHeader, SerialFrame).

    异常:
        ValueError - 长度不足 / 解析失败.
    """
    wire, total_len = parse_wire_header(data)
    if total_len > len(data):
        raise ValueError(
            f"totalLen 声明 {total_len} 超过实际 {len(data)}"
        )
    frame = parse_serial_frame(data[WIRE_HEADER_SIZE:])
    return wire, frame