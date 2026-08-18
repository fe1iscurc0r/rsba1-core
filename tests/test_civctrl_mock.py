"""CivCtrl wrapper 单元测试 (不依赖真实硬件)。

使用 unittest.mock 模拟 ctypes 调用, 测试:
    - CI-V 帧构造/解析 (build_frame / parse_frame)
    - 频率 BCD 编解码 (bytes_to_freq / freq_to_bytes)
    - send_and_wait 超时逻辑与状态检查
    - 句柄校验 / civOpen 失败 / 上下文管理器自动关闭

运行:
    set PYTHONPATH=...\src && python -m unittest tests.test_civctrl_mock
    或:  python tests\test_civctrl_mock.py
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# 确保 src 在 sys.path (直接运行脚本时)
_SRC = os.path.join(os.path.dirname(__file__), os.pardir, "src")
_SRC = os.path.abspath(_SRC)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from rsba1.ctypes_wrappers import civctrl
from rsba1.ctypes_wrappers.civctrl import (
    CivCtrlDLL,
    CivCtrlError,
    CivCtrlHandleError,
    CivCtrlStateError,
    CivCtrlTimeoutError,
)
from rsba1.ctypes_wrappers import civ_commands as civcmd


# ============================================================
# CI-V 帧构造 / 解析 测试
# ============================================================

class TestBuildFrame(unittest.TestCase):
    def test_simple_cmd(self):
        # 读频率: to=0xA4, from=0x00, cmd=0x03
        frame = civcmd.build_frame(0xA4, 0x00, b"\x03")
        self.assertEqual(frame, b"\xfe\xfe\xa4\x00\x03\xfd")

    def test_multibyte_cmd(self):
        # PTT ON: cmd body = 0x1C 0x00 0x01
        frame = civcmd.build_frame(0xA4, 0x00, b"\x1c\x00\x01")
        self.assertEqual(frame, b"\xfe\xfe\xa4\x00\x1c\x00\x01\xfd")

    def test_custom_preamble(self):
        frame = civcmd.build_frame(0x04, 0xE0, b"\x03", preamble_count=3)
        self.assertEqual(frame, b"\xfe\xfe\xfe\x04\xe0\x03\xfd")

    def test_invalid_addr(self):
        with self.assertRaises(ValueError):
            civcmd.build_frame(0x100, 0x00, b"\x03")
        with self.assertRaises(ValueError):
            civcmd.build_frame(0xA4, -1, b"\x03")

    def test_invalid_cmd_type(self):
        with self.assertRaises(TypeError):
            civcmd.build_frame(0xA4, 0x00, "not-bytes")


class TestParseFrame(unittest.TestCase):
    def test_simple(self):
        to, frm, cmd, payload = civcmd.parse_frame(b"\xfe\xfe\xa4\x00\x03\xfd")
        self.assertEqual(to, 0xA4)
        self.assertEqual(frm, 0x00)
        self.assertEqual(cmd, 0x03)
        self.assertEqual(payload, b"")

    def test_with_payload(self):
        to, frm, cmd, payload = civcmd.parse_frame(b"\xfe\xfe\xa4\x00\x1c\x00\x01\xfd")
        self.assertEqual(to, 0xA4)
        self.assertEqual(cmd, 0x1C)
        self.assertEqual(payload, b"\x00\x01")

    def test_extra_preamble(self):
        # 3 个 0xFE 前导也应正确解析
        to, frm, cmd, payload = civcmd.parse_frame(b"\xfe\xfe\xfe\xa4\x00\x03\xfd")
        self.assertEqual(to, 0xA4)
        self.assertEqual(frm, 0x00)
        self.assertEqual(cmd, 0x03)

    def test_freq_response(self):
        # 模拟读频率应答: FE FE E0 04 03 <5 BCD> FD (14.270 MHz)
        frame = b"\xfe\xfe\xe0\x04\x03" + b"\x00\x00\x27\x14\x00" + b"\xfd"
        to, frm, cmd, payload = civcmd.parse_frame(frame)
        self.assertEqual(to, 0xE0)
        self.assertEqual(frm, 0x04)
        self.assertEqual(cmd, 0x03)
        self.assertEqual(payload, b"\x00\x00\x27\x14\x00")
        self.assertEqual(civcmd.bytes_to_freq(payload), 14270000)

    def test_invalid_no_preamble(self):
        with self.assertRaises(ValueError):
            civcmd.parse_frame(b"\x00\x00\x03\xfd")

    def test_invalid_no_tail(self):
        with self.assertRaises(ValueError):
            civcmd.parse_frame(b"\xfe\xfe\xa4\x00\x03\x00")

    def test_invalid_too_short(self):
        with self.assertRaises(ValueError):
            civcmd.parse_frame(b"\xfe\xfd")

    def test_invalid_type(self):
        with self.assertRaises(TypeError):
            civcmd.parse_frame("not-bytes")


class TestBuildParseRoundtrip(unittest.TestCase):
    def test_roundtrip(self):
        cmds = [
            b"\x03",                       # 读频率
            b"\x1c\x00\x01",               # PTT ON
            b"\x05\x00\x00\x27\x14\x00",  # 0x05=设频率 (2026-08-18 真机修正)   # 设频率 14.270 MHz
            b"\x04",                       # 读模式
        ]
        for cmd in cmds:
            frame = civcmd.build_frame(0xA4, 0x00, cmd)
            to, frm, c, payload = civcmd.parse_frame(frame)
            self.assertEqual(to, 0xA4)
            self.assertEqual(frm, 0x00)
            self.assertEqual(bytes([c]) + payload, cmd,
                             msg=f"roundtrip 失败: cmd={cmd.hex()}")


# ============================================================
# 频率 BCD 编解码 测试
# ============================================================

class TestFreqEncoding(unittest.TestCase):
    def test_freq_to_bytes_14_270(self):
        # 14.270 MHz = 14270000 Hz -> LSB-first BCD
        self.assertEqual(civcmd.freq_to_bytes(14270000), b"\x00\x00\x27\x14\x00")

    def test_freq_to_bytes_7(self):
        # 7.000 MHz = 7000000 Hz
        self.assertEqual(civcmd.freq_to_bytes(7000000), b"\x00\x00\x00\x07\x00")

    def test_freq_to_bytes_zero(self):
        self.assertEqual(civcmd.freq_to_bytes(0), b"\x00\x00\x00\x00\x00")

    def test_bytes_to_freq_14_270(self):
        self.assertEqual(civcmd.bytes_to_freq(b"\x00\x00\x27\x14\x00"), 14270000)

    def test_bytes_to_freq_7(self):
        self.assertEqual(civcmd.bytes_to_freq(b"\x00\x00\x00\x07\x00"), 7000000)

    def test_roundtrip(self):
        for hz in [0, 1, 7000000, 14270000, 50123456, 28500000]:
            encoded = civcmd.freq_to_bytes(hz)
            decoded = civcmd.bytes_to_freq(encoded)
            self.assertEqual(decoded, hz, msg=f"roundtrip 失败: {hz} Hz")

    def test_freq_to_bytes_negative(self):
        with self.assertRaises(ValueError):
            civcmd.freq_to_bytes(-1)

    def test_freq_to_bytes_overflow(self):
        with self.assertRaises(ValueError):
            civcmd.freq_to_bytes(10 ** 10)  # 超出 5 字节 BCD 范围

    def test_bytes_to_freq_empty(self):
        with self.assertRaises(ValueError):
            civcmd.bytes_to_freq(b"")

    def test_bytes_to_freq_invalid_bcd(self):
        with self.assertRaises(ValueError):
            civcmd.bytes_to_freq(b"\xab\x00\x00\x00\x00")  # 0xAB 含非 BCD 半字节


# ============================================================
# 命令常量与辅助函数测试
# ============================================================

class TestCommandConstants(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(civcmd.CMD_READ_FREQ, 0x03)
        self.assertEqual(civcmd.CMD_READ_MODE, 0x04)
        self.assertEqual(civcmd.CMD_SET_FREQ, 0x05)
        self.assertEqual(civcmd.CMD_PTT, 0x1C)
        self.assertEqual(civcmd.CMD_PTT_ON, 0x1C00)
        self.assertEqual(civcmd.CMD_PTT_OFF, 0x1C01)
        self.assertEqual(civcmd.IC705_TO_ADDR, 0xA4)
        self.assertEqual(civcmd.FROM_ADDR, 0x00)
        self.assertEqual(civcmd.PREAMBLE, 0xFE)
        self.assertEqual(civcmd.POSTAMBLE, 0xFD)

    def test_cmd_const_to_bytes(self):
        self.assertEqual(civcmd.cmd_const_to_bytes(0x1C00), b"\x1c\x00")
        self.assertEqual(civcmd.cmd_const_to_bytes(0x1C01), b"\x1c\x01")

    def test_ptt_on_bytes(self):
        self.assertEqual(civcmd.ptt_on_bytes(), b"\x1c\x00\x01")

    def test_ptt_off_bytes(self):
        self.assertEqual(civcmd.ptt_off_bytes(), b"\x1c\x00\x00")

    def test_set_freq_bytes(self):
        self.assertEqual(
            civcmd.set_freq_bytes(14270000),
            b"\x05\x00\x00\x27\x14\x00",  # 0x05=设频率 (2026-08-18 真机修正)
        )


# ============================================================
# 频段白名单 (安全约束)
# ============================================================

class TestFreqWhitelist(unittest.TestCase):
    """业余频段白名单: 放行合法频率, 拦截越界频率。"""

    def test_allowed_bands(self):
        """白名单内频率放行 (160m-10m, 6m, 2m)。"""
        for hz in [1_800_000, 14_270_000, 29_999_999,
                   50_000_000, 53_999_999, 144_000_000, 147_999_999]:
            self.assertTrue(civcmd.is_allowed_freq(hz), hz)
            civcmd.assert_allowed_freq(hz)  # 不抛异常

    def test_disallowed_bands(self):
        """白名单外频率拒绝。"""
        for hz in [0, 1_000_000, 30_000_001, 37_000_000,
                   54_000_001, 100_000_000, 148_000_001]:
            self.assertFalse(civcmd.is_allowed_freq(hz), hz)
            with self.assertRaises(ValueError):
                civcmd.assert_allowed_freq(hz)

    def test_negative_freq_rejected(self):
        """负频率被白名单拦截。"""
        self.assertFalse(civcmd.is_allowed_freq(-1))
        with self.assertRaises(ValueError):
            civcmd.assert_allowed_freq(-1)


# ============================================================
# CivCtrlDLL mock 测试 (send_and_wait 超时逻辑等)
# ============================================================

class TestCivCtrlDLLMocked(unittest.TestCase):
    """用 MagicMock 替换真实 DLL, 测试高层方法逻辑。"""

    def _make_civ(self):
        mock_dll = MagicMock()
        civ = CivCtrlDLL(dll=mock_dll)
        return civ, mock_dll

    # --- 句柄校验 ---

    def test_handle_none_raises(self):
        civ, _ = self._make_civ()
        with self.assertRaises(CivCtrlHandleError):
            civ.civSend(None, b"\x03")
        with self.assertRaises(CivCtrlHandleError):
            civ.civGetRecvSize(None)
        with self.assertRaises(CivCtrlHandleError):
            civ.civIsSendEnable(None)
        with self.assertRaises(CivCtrlHandleError):
            civ.civRecv(None)

    # --- civOpen 成功/失败 ---

    def test_civOpen_success(self):
        civ, mock_dll = self._make_civ()
        mock_dll.civOpen.return_value = 1
        h = civ.civOpen(3, 19200, 0, 0)
        self.assertEqual(h, 1)
        self.assertEqual(civ._handle, 1)
        mock_dll.civOpen.assert_called_once_with(1, 3, 19200, 0, 0)

    def test_civOpen_failure_raises(self):
        civ, mock_dll = self._make_civ()
        mock_dll.civOpen.return_value = 0
        with self.assertRaises(CivCtrlError):
            civ.civOpen(3, 9600, 0, 0)

    # --- civSend 数据转换 ---

    def test_civSend_passes_c_char_p(self):
        civ, mock_dll = self._make_civ()
        civ.civSend(1, b"\xa4\x03\x05", flag=0)  # 无嵌入 NUL, 便于 .value 校验
        mock_dll.civSend.assert_called_once()
        args = mock_dll.civSend.call_args.args
        self.assertEqual(args[0], 1)            # handle
        self.assertEqual(args[2], 3)            # len
        self.assertEqual(args[3], 0)            # flag
        # data 应为 c_char_p (指向 bytes 内容)
        import ctypes as _ct
        self.assertIsInstance(args[1], _ct.c_char_p)  # data 转 c_char_p
        self.assertEqual(args[1].value, b"\xa4\x03\x05")  # 指向相同内容 (NUL-free)

    def test_civSend_bytearray(self):
        civ, mock_dll = self._make_civ()
        civ.civSend(1, bytearray(b"\x03"))
        self.assertEqual(mock_dll.civSend.call_args.args[2], 1)

    def test_civSend_invalid_type(self):
        civ, _ = self._make_civ()
        with self.assertRaises(TypeError):
            civ.civSend(1, "not-bytes")

    # --- civIsSendEnable 返回 bool ---

    def test_civIsSendEnable_true(self):
        civ, mock_dll = self._make_civ()
        mock_dll.civIsSendEnable.return_value = 1
        self.assertIs(civ.civIsSendEnable(1), True)

    def test_civIsSendEnable_false(self):
        civ, mock_dll = self._make_civ()
        mock_dll.civIsSendEnable.return_value = 0
        self.assertIs(civ.civIsSendEnable(1), False)

    # --- send_and_wait 超时 ---

    @patch.object(civctrl.time, "sleep", lambda *_a, **_k: None)
    def test_send_and_wait_timeout(self):
        civ, mock_dll = self._make_civ()
        mock_dll.civIsSendEnable.return_value = 1
        mock_dll.civGetRecvSize.return_value = 0  # 始终无数据 -> 超时
        with self.assertRaises(CivCtrlTimeoutError):
            civ.send_and_wait(1, b"\xa4\x00\x03", timeout_ms=10)
        # 验证 civSend 被调用过一次
        mock_dll.civSend.assert_called_once()
        # 验证 civGetRecvSize 被轮询过 (>=1 次)
        self.assertGreaterEqual(mock_dll.civGetRecvSize.call_count, 1)

    # --- send_and_wait 状态非 IDLE ---

    @patch.object(civctrl.time, "sleep", lambda *_a, **_k: None)
    def test_send_and_wait_state_error(self):
        civ, mock_dll = self._make_civ()
        mock_dll.civIsSendEnable.return_value = 0  # 非 IDLE
        with self.assertRaises(CivCtrlStateError):
            civ.send_and_wait(1, b"\xa4\x00\x03", timeout_ms=10)
        # 状态非 IDLE 时不应调用 civSend
        mock_dll.civSend.assert_not_called()

    # --- send_and_wait 成功 ---

    @patch.object(civctrl.time, "sleep", lambda *_a, **_k: None)
    def test_send_and_wait_success(self):
        civ, mock_dll = self._make_civ()
        mock_dll.civIsSendEnable.return_value = 1
        mock_dll.civGetRecvSize.return_value = 5  # 立即有数据
        expected = (b"\x03\x00\x27\x14\x00", 0)
        # mock 高层 civRecv, 避免构造 buf/指针交互
        with patch.object(civ, "civRecv", return_value=expected) as mock_recv:
            result = civ.send_and_wait(1, b"\xa4\x00\x03", timeout_ms=10)
        self.assertEqual(result, expected)
        mock_recv.assert_called_once_with(1)

    # --- send_and_wait handle=None ---

    def test_send_and_wait_none_handle(self):
        civ, _ = self._make_civ()
        with self.assertRaises(CivCtrlHandleError):
            civ.send_and_wait(None, b"\x03", timeout_ms=10)

    # --- 上下文管理器 ---

    def test_context_manager_closes_handle(self):
        civ, mock_dll = self._make_civ()
        mock_dll.civOpen.return_value = 1
        with civ:
            h = civ.civOpen(3, 9600, 0, 0)
            self.assertEqual(h, 1)
        # 退出时应自动 civClose(self._handle)
        mock_dll.civClose.assert_called_once_with(1)

    def test_context_manager_no_handle_no_close(self):
        # 未 civOpen 时退出, 不应调用 civClose
        civ, mock_dll = self._make_civ()
        with civ:
            pass
        mock_dll.civClose.assert_not_called()

    # --- civClose 清除 _handle ---

    def test_civClose_clears_handle(self):
        civ, mock_dll = self._make_civ()
        mock_dll.civOpen.return_value = 1
        h = civ.civOpen(3, 9600, 0, 0)
        civ.civClose(h)
        self.assertIsNone(civ._handle)
        mock_dll.civClose.assert_called_once_with(h)

    def test_civClose_none_is_noop(self):
        civ, mock_dll = self._make_civ()
        civ.civClose(None)  # 不应抛异常
        mock_dll.civClose.assert_not_called()


if __name__ == "__main__":
    unittest.main()