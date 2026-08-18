"""test_serial_codec — Serial 信道 codec 单元测试 (纯代码层, 不依赖硬件).

测试范围:
    1. wire 头构造/解析 (与线上抓包字节比对, 见 serial_channel.md §5)
    2. Serial 帧构造/解析 (flags/frameLen/sseq/payload)
    3. 完整 UDP 包编解码 (wire 头 + Serial 帧)
    4. 线上样本回放 (读频率与应答帧)
    5. 边界/异常 (长度不足 / 范围越界 / 类型错误)

运行方式:
    python tests\\test_serial_codec.py

依赖:
    - 仅 Python 标准库 (unittest)
    - 被测代码 rsba1.serial.serial_codec
"""
from __future__ import annotations

import os
import sys
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from rsba1.serial.serial_codec import (  # noqa: E402
    WIRE_HEADER_SIZE,
    SERIAL_FRAME_HEADER_SIZE,
    UDP2_PKT_TYPE_DATA,
    UDP2_PKT_TYPE_KEEPALIVE,
    UDP2WireHeader,
    SerialFrame,
    build_wire_header,
    parse_wire_header,
    build_serial_frame,
    parse_serial_frame,
    build_udp_packet,
    parse_udp_packet,
)


class TestWireHeader(unittest.TestCase):
    """wire 头构造/解析."""

    def test_build_matches_online_layout(self):
        """构造 wire 头, 字节布局与线上抓包一致.

        线上样本 (serial_channel.md §5.1), 16 字节无 payload 首包:
            10 00 00 00 | 00 00 | 6a c8 | 02 bc 94 2a | f7 b4 f8 19
        field_8/field_C 按 LE 解读 (main 权威定案): 02bc942a = 0x2A94BC02。
        """
        wire = build_wire_header(seq=0xC86A, field_8=0x2A94BC02, field_C=0x19F8B4F7)
        self.assertEqual(wire.hex(), "1000000000006ac802bc942af7b4f819")

    def test_total_len_includes_payload(self):
        """totalLen = 0x10 + payload_len."""
        wire = build_wire_header(payload_len=12)
        self.assertEqual(len(wire), WIRE_HEADER_SIZE)
        self.assertEqual(int.from_bytes(wire[0:4], "little"), 0x10 + 12)

    def test_parse_roundtrip(self):
        """解析 = 构造逆操作."""
        wire = build_wire_header(
            type=UDP2_PKT_TYPE_KEEPALIVE, seq=0x1234,
            field_8=0xDEADBEEF, field_C=0xCAFEBABE, payload_len=8,
        )
        hdr, total_len = parse_wire_header(wire)
        self.assertEqual(hdr.type, UDP2_PKT_TYPE_KEEPALIVE)
        self.assertEqual(hdr.seq, 0x1234)
        self.assertEqual(hdr.field_8, 0xDEADBEEF)
        self.assertEqual(hdr.field_C, 0xCAFEBABE)
        self.assertEqual(total_len, 0x10 + 8)

    def test_parse_short_data_raises(self):
        """不足 0x10 字节报错."""
        with self.assertRaises(ValueError):
            parse_wire_header(b"\x00" * (WIRE_HEADER_SIZE - 1))

    def test_dataclass_range_validation(self):
        """越界字段报错."""
        with self.assertRaises(ValueError):
            UDP2WireHeader(seq=0x10000)
        with self.assertRaises(ValueError):
            UDP2WireHeader(field_8=0x100000000)


class TestSerialFrame(unittest.TestCase):
    """Serial 帧构造/解析."""

    def test_build_bulk_default(self):
        """默认批量帧: c1 + frameLen(LE) + sseq(BE) + payload."""
        frame = build_serial_frame(b"\xfe\xfe\xa4\xe0", sseq=0x4B8F)
        self.assertEqual(frame.hex(), "c104004b8ffefea4e0")

    def test_parse_roundtrip(self):
        """批量/单字节两分支往返一致."""
        for bulk, payload in (
            (True, b"\xfe\xfe\xa4\xe0\x26\x00\xfd"),
            (False, b"\x01"),
        ):
            frame = parse_serial_frame(build_serial_frame(payload, sseq=5, bulk=bulk))
            self.assertEqual(frame.bulk, bulk)
            self.assertEqual(frame.sseq, 5)
            self.assertEqual(frame.payload, payload)

    def test_parse_online_sample(self):
        """线上应答帧样本 (serial_channel.md §5.3):
            c1 0a 00 1e a3 fe fe e0 a4 26 00 05 00 01 fd
        """
        data = bytes.fromhex("c10a001ea3fefee0a42600050001fd")
        frame = parse_serial_frame(data)
        self.assertTrue(frame.bulk)
        self.assertEqual(frame.sseq, 0x1EA3)
        self.assertEqual(frame.payload.hex(), "fefee0a42600050001fd")

    def test_parse_short_raises(self):
        """不足 5 字节报错."""
        with self.assertRaises(ValueError):
            parse_serial_frame(b"\xc1\x00")

    def test_frame_len_mismatch_raises(self):
        """frameLen 声明与实际 payload 不符报错."""
        with self.assertRaises(ValueError):
            parse_serial_frame(b"\xc1\x0a\x00\x1e\xa3\xfe\xfe")  # 声明 10, 实际 2


class TestUdpPacket(unittest.TestCase):
    """完整 UDP 包编解码."""

    def test_build_udp_packet_online_replay(self):
        """重放线上读频率样本 (serial_channel.md §5.3).

        本机发: wire头 + c1 07 00 8f 4b | fe fe a4 e0 26 00 fd
        civ 为读频率帧, totalLen = 0x10 + 5 + 7 = 0x1C.
        """
        civ = bytes.fromhex("fe fe a4 e0 26 00 fd")
        pkt = build_udp_packet(
            civ, sseq=0x4B8F, seq=0xC86A,
            field_8=0x2A94BC02, field_C=0x19F8B4F7,
        )
        # 首 4 字节 totalLen = 0x1C
        self.assertEqual(int.from_bytes(pkt[0:4], "little"), 0x1C)
        wire, frame = parse_udp_packet(pkt)
        self.assertEqual(wire.seq, 0xC86A)
        self.assertEqual(frame.sseq, 0x4B8F)
        self.assertEqual(frame.payload, civ)

    def test_parse_udp_online_sample(self):
        """解析线上数据包 wire 头 (serial_channel.md §5.2 样本):
            1c 00 00 00 00 00 6c c8 02 bc 94 2a f7 b4 f8 19 c1 07 00 8f 4b fe fe a4 e0 26 00 fd
        """
        data = bytes.fromhex(
            "1c00000000006cc802bc942af7b4f819"
            "c107008f4bfefea4e02600fd"
        )
        wire, frame = parse_udp_packet(data)
        self.assertEqual(wire.type, UDP2_PKT_TYPE_DATA)
        self.assertEqual(wire.seq, 0xC86C)
        # field_8/field_C 按 LE 解读 (main 权威定案): 02bc942a = 0x2A94BC02
        self.assertEqual(wire.field_8, 0x2A94BC02)
        self.assertEqual(wire.field_C, 0x19F8B4F7)
        self.assertTrue(frame.bulk)
        self.assertEqual(frame.payload.hex(), "fefea4e02600fd")

    def test_total_len_exceeds_data_raises(self):
        """totalLen 声明超过实际 UDP 包长度报错."""
        # 构造一个 wire 头声明 totalLen 很大, 但实际数据短
        pkt = build_wire_header(payload_len=100) + b"\xc1\x00\x00\x00\x00"
        with self.assertRaises(ValueError):
            parse_udp_packet(pkt)


if __name__ == "__main__":
    unittest.main(verbosity=2)