"""test_civ_via_execcmd_mock — civ_via_execcmd payload 构造单元测试.

测试范围 (不依赖真实 Mailslot / 硬件):
    1. build_exec_cmd_civ: ExecCmd payload 结构 (20 字节固定头 + user_data)
    2. build_*_payload: 各 CI-V 命令 (read_freq/mode, set_freq, ptt, smeter) 的 user_data
    3. data_len 计算正确性 (user_len + 0x14)
    4. sub_cmd 字节位于 payload[16]
    5. BCD 频率编码 (set_freq)
    6. 边界/异常: 过长 civ_frame / 非法类型

运行方式:
    cd d:\\my git\\rs-ba1-reverse
    d:\\my git\\scratchpad\\.venv\\Scripts\\python.exe tests\\test_civ_via_execcmd_mock.py

依赖:
    - 仅 Python 标准库 (unittest)
    - 被测代码 rsba1.mailslot.civ_via_execcmd + rsba1.mailslot.commands
"""
from __future__ import annotations

import os
import sys
import struct
import unittest

# 把 src/ 加到 sys.path
_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from rsba1.mailslot.civ_via_execcmd import (  # noqa: E402
    DEFAULT_TO_ADDR,
    DEFAULT_FROM_ADDR,
    DEFAULT_SUB_CMD,
    DEFAULT_ARG3,
    DEFAULT_ARG6,
    RESPONSE_MAILSLOT_NAME,
    build_exec_cmd_civ,
    build_read_freq_payload,
    build_read_mode_payload,
    build_set_freq_payload,
    build_ptt_on_payload,
    build_ptt_off_payload,
    build_read_smeter_payload,
    build_raw_civ_payload,
)
from rsba1.mailslot.protocol import MAX_PAYLOAD_SIZE, CMD_EXEC_CMD  # noqa: E402
from rsba1.ctypes_wrappers import civ_commands as civcmd  # noqa: E402


# ============================================================
# 1. build_exec_cmd_civ — ExecCmd payload 结构
# ============================================================

class TestBuildExecCmdCivStructure(unittest.TestCase):
    """验证 ExecCmd payload 的 20 字节固定头 + user_data 布局。"""

    def test_payload_length_is_user_len_plus_20(self):
        for user_len in [1, 3, 5, 8, 100, 235]:
            civ_frame = b"\x00" * user_len
            payload, data_len = build_exec_cmd_civ(civ_frame)
            self.assertEqual(len(payload), user_len + 20,
                             f"user_len={user_len}")
            self.assertEqual(data_len, user_len + 20)

    def test_fixed_header_bytes_0_to_3_are_zero(self):
        payload, _ = build_exec_cmd_civ(b"\xAB\xCD")
        self.assertEqual(payload[0:4], b"\x00\x00\x00\x00")

    def test_arg3_at_offset_4(self):
        payload, _ = build_exec_cmd_civ(b"\x00", arg3=0x12345678)
        self.assertEqual(payload[4:8], struct.pack("<I", 0x12345678))

    def test_arg5_at_offset_8_equals_user_len(self):
        civ_frame = b"\x01\x02\x03\x04\x05"
        payload, _ = build_exec_cmd_civ(civ_frame)
        self.assertEqual(payload[8:12], struct.pack("<I", len(civ_frame)))

    def test_arg6_at_offset_12(self):
        payload, _ = build_exec_cmd_civ(b"\x00", arg6=0xAB)
        self.assertEqual(payload[12], 0xAB)

    def test_bytes_13_to_15_are_zero(self):
        payload, _ = build_exec_cmd_civ(b"\x00")
        self.assertEqual(payload[13:16], b"\x00\x00\x00")

    def test_sub_cmd_at_offset_16(self):
        for sub_cmd in [0, 1, 2, 3, 4, 5]:
            payload, _ = build_exec_cmd_civ(b"\x00", sub_cmd=sub_cmd)
            self.assertEqual(payload[16], sub_cmd,
                             f"sub_cmd={sub_cmd}")

    def test_bytes_17_to_19_are_zero(self):
        payload, _ = build_exec_cmd_civ(b"\x00")
        self.assertEqual(payload[17:20], b"\x00\x00\x00")

    def test_user_data_starts_at_offset_20(self):
        civ_frame = b"\xA4\x00\x03"
        payload, _ = build_exec_cmd_civ(civ_frame)
        self.assertEqual(payload[20:], civ_frame)

    def test_default_arg3_arg6_sub_cmd_are_zero(self):
        payload, _ = build_exec_cmd_civ(b"\x00")
        self.assertEqual(payload[4:8], b"\x00\x00\x00\x00")  # arg3
        self.assertEqual(payload[12], 0x00)                    # arg6
        self.assertEqual(payload[16], 0x00)                    # sub_cmd


# ============================================================
# 2. 各 CI-V 命令的 user_data 内容
# ============================================================

class TestCivCommandPayloads(unittest.TestCase):
    """验证各 build_*_payload 构造的 CI-V 命令体正确性。"""

    def test_read_freq_user_data(self):
        """read_freq: [to, from, 0x03]"""
        payload, data_len = build_read_freq_payload()
        self.assertEqual(payload[20:], bytes([DEFAULT_TO_ADDR, DEFAULT_FROM_ADDR, 0x03]))
        self.assertEqual(data_len, 3 + 20)

    def test_read_mode_user_data(self):
        """read_mode: [to, from, 0x04]"""
        payload, data_len = build_read_mode_payload()
        self.assertEqual(payload[20:], bytes([DEFAULT_TO_ADDR, DEFAULT_FROM_ADDR, 0x04]))
        self.assertEqual(data_len, 3 + 20)

    def test_read_smeter_user_data(self):
        """read_smeter: [to, from, 0x1A, 0x03]"""
        payload, data_len = build_read_smeter_payload()
        self.assertEqual(payload[20:], bytes([DEFAULT_TO_ADDR, DEFAULT_FROM_ADDR, 0x1A, 0x03]))
        self.assertEqual(data_len, 4 + 20)

    def test_ptt_on_user_data(self):
        """ptt_on: [to, from, 0x1C, 0x00, 0x01]"""
        payload, data_len = build_ptt_on_payload()
        self.assertEqual(payload[20:], bytes([DEFAULT_TO_ADDR, DEFAULT_FROM_ADDR, 0x1C, 0x00, 0x01]))
        self.assertEqual(data_len, 5 + 20)

    def test_ptt_off_user_data(self):
        """ptt_off: [to, from, 0x1C, 0x00, 0x00]"""
        payload, data_len = build_ptt_off_payload()
        self.assertEqual(payload[20:], bytes([DEFAULT_TO_ADDR, DEFAULT_FROM_ADDR, 0x1C, 0x00, 0x00]))
        self.assertEqual(data_len, 5 + 20)

    def test_set_freq_user_data(self):
        """set_freq: [to, from, 0x05, BCD_freq(5)] (0x05=设频率, 2026-08-18 真机修正)"""
        payload, data_len = build_set_freq_payload(14270000)
        expected_bcd = civcmd.freq_to_bytes(14270000)
        self.assertEqual(payload[20:22], bytes([DEFAULT_TO_ADDR, DEFAULT_FROM_ADDR]))
        self.assertEqual(payload[22], 0x05)
        self.assertEqual(payload[23:28], expected_bcd)
        self.assertEqual(data_len, 8 + 20)

    def test_set_freq_bcd_encoding(self):
        """验证 BCD 频率编码: 14270000 Hz -> 00 00 27 14 00 (LSB-first)"""
        payload, _ = build_set_freq_payload(14270000)
        bcd = payload[23:28]
        # 14270000 -> MSB-first "0014270000" -> bytes 00 14 27 00 00
        # -> LSB-first reverse: 00 00 27 14 00
        self.assertEqual(bcd, b"\x00\x00\x27\x14\x00")

    def test_custom_addresses(self):
        """自定义 to/from 地址传递正确。"""
        payload, _ = build_read_freq_payload(to_addr=0x04, from_addr=0xE0)
        self.assertEqual(payload[20], 0x04)
        self.assertEqual(payload[21], 0xE0)

    def test_raw_civ_payload_passthrough(self):
        """build_raw_civ_payload 透传原始帧, 不加 to/from。"""
        frame = bytes([0xFE, 0xFE, 0xA4, 0x00, 0x03, 0xFD])
        payload, data_len = build_raw_civ_payload(frame)
        self.assertEqual(payload[20:], frame)
        self.assertEqual(data_len, len(frame) + 20)


# ============================================================
# 3. 参数传递 (arg3 / arg6 / sub_cmd)
# ============================================================

class TestParameterPassThrough(unittest.TestCase):
    """验证 arg3 / arg6 / sub_cmd 参数正确传递到 payload。"""

    def test_arg3_passed_to_payload(self):
        payload, _ = build_read_freq_payload(arg3=0xDEADBEEF)
        self.assertEqual(payload[4:8], struct.pack("<I", 0xDEADBEEF))

    def test_arg6_passed_to_payload(self):
        payload, _ = build_read_freq_payload(arg6=0xFF)
        self.assertEqual(payload[12], 0xFF)

    def test_sub_cmd_passed_to_payload(self):
        payload, _ = build_read_freq_payload(sub_cmd=3)
        self.assertEqual(payload[16], 3)

    def test_all_params_combined(self):
        payload, _ = build_set_freq_payload(
            7100000, to_addr=0x04, from_addr=0xE0,
            arg3=0x11111111, arg6=0x42, sub_cmd=2,
        )
        self.assertEqual(payload[4:8], struct.pack("<I", 0x11111111))
        self.assertEqual(payload[12], 0x42)
        self.assertEqual(payload[16], 2)
        self.assertEqual(payload[20:22], bytes([0x04, 0xE0]))
        self.assertEqual(payload[22], 0x05)


# ============================================================
# 4. 边界与异常
# ============================================================

class TestEdgeCases(unittest.TestCase):
    """边界条件与异常路径。"""

    def test_empty_civ_frame(self):
        """空 civ_frame: data_len = 20, user_data 为空。"""
        payload, data_len = build_exec_cmd_civ(b"")
        self.assertEqual(len(payload), 20)
        self.assertEqual(data_len, 20)
        self.assertEqual(payload[8:12], struct.pack("<I", 0))  # arg5=0

    def test_max_civ_frame_235_bytes(self):
        """235 字节 civ_frame: data_len = 255 = MAX_PAYLOAD_SIZE (刚好不超)。"""
        civ_frame = b"\x00" * 235
        payload, data_len = build_exec_cmd_civ(civ_frame)
        self.assertEqual(data_len, 255)
        self.assertEqual(len(payload), 255)

    def test_oversize_civ_frame_raises(self):
        """236 字节 civ_frame: data_len = 256 > MAX_PAYLOAD_SIZE, 应抛 ValueError。"""
        civ_frame = b"\x00" * 236
        with self.assertRaises(ValueError):
            build_exec_cmd_civ(civ_frame)

    def test_non_bytes_raises_type_error(self):
        """非 bytes/bytearray 输入应抛 TypeError。"""
        with self.assertRaises(TypeError):
            build_exec_cmd_civ("not bytes")  # type: ignore
        with self.assertRaises(TypeError):
            build_exec_cmd_civ(123)  # type: ignore

    def test_bytearray_accepted(self):
        """bytearray 输入应被接受 (转为 bytes)。"""
        payload, data_len = build_exec_cmd_civ(bytearray(b"\xA4\x00\x03"))
        self.assertEqual(payload[20:], b"\xA4\x00\x03")

    def test_negative_freq_raises(self):
        """set_freq 负频率应抛 ValueError (来自 freq_to_bytes)。"""
        with self.assertRaises(ValueError):
            build_set_freq_payload(-1)


# ============================================================
# 5. 常量正确性
# ============================================================

class TestConstants(unittest.TestCase):
    """默认常量值正确性。"""

    def test_default_to_addr_is_ic705(self):
        self.assertEqual(DEFAULT_TO_ADDR, 0xA4)

    def test_default_from_addr_is_zero(self):
        self.assertEqual(DEFAULT_FROM_ADDR, 0x00)

    def test_default_sub_cmd_is_zero(self):
        self.assertEqual(DEFAULT_SUB_CMD, 0)

    def test_default_arg3_is_zero(self):
        self.assertEqual(DEFAULT_ARG3, 0)

    def test_default_arg6_is_zero(self):
        self.assertEqual(DEFAULT_ARG6, 0)

    def test_response_mailslot_name(self):
        self.assertEqual(RESPONSE_MAILSLOT_NAME, r"\\.\mailslot\RemoteUtyCtrlRes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
