"""test_civ_response — civ_response 解析器 + query 闭环单元测试.

测试范围 (不依赖真实 Mailslot / 硬件):
    1. find_civ_frame: 从任意封装(裸帧 / Mailslot 命令包内嵌)提取 CI-V 帧
    2. parse_freq / parse_mode / parse_smeter / parse_ptt: 结构化解析
    3. parse_any: 按应答命令码自动分派
    4. query 闭环: CivViaExecCmdSender.query* 发送 + 读响应 + 解析 (mock reader)

运行方式:
    python tests\\test_civ_response.py

依赖:
    - 仅 Python 标准库 (unittest, unittest.mock)
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from rsba1.mailslot import civ_response as civresp  # noqa: E402
from rsba1.mailslot.civ_via_execcmd import (  # noqa: E402
    CivViaExecCmdSender,
    ResponseTimeoutError,
)
from rsba1.ctypes_wrappers import civ_commands as civcmd  # noqa: E402


# ============================================================
# 帮助: 构造 CI-V 应答帧
# ============================================================

def _frame(cmd: int, payload: bytes) -> bytes:
    """构造应答帧: FE FE <from=0xA4电台> <to=0x00控制器> <cmd> <payload> FD。"""
    return bytes([0xFE, 0xFE, 0xA4, 0x00, cmd]) + payload + bytes([0xFD])


def _freq_frame(hz: int) -> bytes:
    return _frame(0x03, civcmd.freq_to_bytes(hz))


def _mode_frame(mode: int, filt: int) -> bytes:
    return _frame(0x04, bytes([mode, filt]))


def _smeter_frame(data: int) -> bytes:
    return _frame(0x1A, bytes([0x03, data]))


def _ptt_frame(tx: bool) -> bytes:
    return _frame(0x14, bytes([0x0C, 1 if tx else 0]))


# ============================================================
# 1. find_civ_frame — 弹性帧提取
# ============================================================

class TestFindCivFrame(unittest.TestCase):
    def test_bare_frame(self):
        blob = _freq_frame(14270000)
        self.assertEqual(civresp.find_civ_frame(blob), blob)

    def test_frame_with_prefix_and_suffix(self):
        frame = _freq_frame(14270000)
        blob = b"\x00\x00" + frame + b"\xff\xff"
        self.assertEqual(civresp.find_civ_frame(blob), frame)

    def test_frame_embedded_in_mailslot_packet(self):
        """Mailslot 命令包 (4 字节头) 内嵌 CI-V 帧。"""
        frame = _freq_frame(7100000)
        blob = bytes([0x02, len(frame), 0x00, 0x00]) + frame
        self.assertEqual(civresp.find_civ_frame(blob), frame)

    def test_frame_embedded_in_exec_cmd_packet(self):
        """ExecCmd 风格 (20 字节头) 内嵌 CI-V 帧。"""
        frame = _mode_frame(0x02, 0x01)
        blob = b"\x00" * 20 + frame + b"\x00\x00"
        self.assertEqual(civresp.find_civ_frame(blob), frame)

    def test_no_frame_raises(self):
        with self.assertRaises(civresp.CivFrameNotFoundError):
            civresp.find_civ_frame(b"\x00\x01\x02\x03")

    def test_non_bytes_raises_type_error(self):
        with self.assertRaises(TypeError):
            civresp.find_civ_frame("not bytes")  # type: ignore


# ============================================================
# 2. 结构化解析
# ============================================================

class TestParseFreq(unittest.TestCase):
    def test_freq_14mhz(self):
        self.assertEqual(civresp.parse_freq(_freq_frame(14270000)), 14270000)

    def test_freq_7mhz(self):
        self.assertEqual(civresp.parse_freq(_freq_frame(7100000)), 7100000)

    def test_wrong_cmd_raises(self):
        with self.assertRaises(civresp.CivResponseError):
            civresp.parse_freq(_mode_frame(0x02, 0x01))

    def test_short_payload_raises(self):
        # cmd=0x03 但只有 3 字节 BCD
        blob = bytes([0xFE, 0xFE, 0xA4, 0x00, 0x03]) + b"\x00\x00\x01" + bytes([0xFD])
        with self.assertRaises(civresp.CivResponseError):
            civresp.parse_freq(blob)

    def test_no_frame_raises(self):
        with self.assertRaises(civresp.CivFrameNotFoundError):
            civresp.parse_freq(b"\x00\x01\x02")


class TestParseMode(unittest.TestCase):
    def test_usb(self):
        self.assertEqual(civresp.parse_mode(_mode_frame(0x02, 0x01)), (0x02, 0x01))

    def test_fm(self):
        self.assertEqual(civresp.parse_mode(_mode_frame(0x06, 0x00)), (0x06, 0x00))

    def test_wrong_cmd_raises(self):
        with self.assertRaises(civresp.CivResponseError):
            civresp.parse_mode(_freq_frame(14270000))

    def test_short_payload_raises(self):
        blob = bytes([0xFE, 0xFE, 0xA4, 0x00, 0x04, 0x02, 0xFD])
        with self.assertRaises(civresp.CivResponseError):
            civresp.parse_mode(blob)


class TestParseSmeter(unittest.TestCase):
    def test_smeter_value(self):
        self.assertEqual(civresp.parse_smeter(_smeter_frame(0x42)), 0x42)

    def test_wrong_cmd_raises(self):
        with self.assertRaises(civresp.CivResponseError):
            civresp.parse_smeter(_freq_frame(14270000))

    def test_wrong_subcmd_raises(self):
        blob = bytes([0xFE, 0xFE, 0xA4, 0x00, 0x1A, 0xFF, 0x42, 0xFD])
        with self.assertRaises(civresp.CivResponseError):
            civresp.parse_smeter(blob)


class TestParsePtt(unittest.TestCase):
    def test_tx(self):
        self.assertTrue(civresp.parse_ptt(_ptt_frame(True)))

    def test_rx(self):
        self.assertFalse(civresp.parse_ptt(_ptt_frame(False)))

    def test_wrong_cmd_raises(self):
        with self.assertRaises(civresp.CivResponseError):
            civresp.parse_ptt(_freq_frame(14270000))


# ============================================================
# 3. parse_any — 自动分派
# ============================================================

class TestParseAny(unittest.TestCase):
    def test_freq(self):
        r = civresp.parse_any(_freq_frame(14270000))
        self.assertEqual(r["kind"], "freq")
        self.assertEqual(r["value"], 14270000)

    def test_mode(self):
        r = civresp.parse_any(_mode_frame(0x02, 0x01))
        self.assertEqual(r["kind"], "mode")
        self.assertEqual(r["value"], (0x02, 0x01))

    def test_smeter(self):
        r = civresp.parse_any(_smeter_frame(0x42))
        self.assertEqual(r["kind"], "smeter")
        self.assertEqual(r["value"], 0x42)

    def test_ptt(self):
        r = civresp.parse_any(_ptt_frame(True))
        self.assertEqual(r["kind"], "ptt")
        self.assertEqual(r["value"], True)

    def test_unknown(self):
        r = civresp.parse_any(_frame(0x99, b"\x01\x02"))
        self.assertEqual(r["kind"], "unknown")
        self.assertEqual(r["value"], b"\x01\x02")


# ============================================================
# 4. query 闭环 (mock reader)
# ============================================================

class FakeReader:
    """假的 ResponseReader: 依次吐出预设响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.opened = False
        self.closed = False
        self.read_calls = 0

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    def read(self, timeout_ms=200):
        self.read_calls += 1
        if self.responses:
            return self.responses.pop(0)
        return None


class TestQueryClosedLoop(unittest.TestCase):
    def setUp(self):
        self.sender = CivViaExecCmdSender()

    def test_query_freq(self):
        """query_freq 完整走通: 发送 + 读响 + 解析, 返回 Hz。"""
        fake = FakeReader([_freq_frame(14270000)])
        with mock.patch(
            "rsba1.mailslot.civ_via_execcmd.ResponseReader", return_value=fake
        ) as mk, mock.patch.object(self.sender, "send_read_freq") as send:
            freq = self.sender.query_freq(timeout_ms=1000)
        self.assertEqual(freq, 14270000)
        send.assert_called_once_with()
        mk.assert_called_once_with(read_timeout_ms=1000)

    def test_query_base_returns_parsed(self):
        """query() 用假 reader 返回解析结果。"""
        reader = FakeReader([_freq_frame(14270000)])
        sent = mock.Mock()
        result = self.sender.query(sent, civresp.parse_freq, timeout_ms=1000,
                                   reader=reader)
        self.assertEqual(result, 14270000)
        sent.assert_called_once_with()

    def test_query_base_reads_until_response(self):
        """前两次 read 空, 第三次返回响应。"""
        reader = FakeReader([None, None, _mode_frame(0x06, 0x00)])
        result = self.sender.query(mock.Mock(), civresp.parse_mode,
                                   timeout_ms=10000, reader=reader)
        self.assertEqual(result, (0x06, 0x00))
        self.assertEqual(reader.read_calls, 3)

    def test_query_timeout(self):
        """全部为空 -> 超时抛 ResponseTimeoutError。"""
        reader = FakeReader([None, None])
        # timeout_ms 设为 400ms, 内部每轮等 200ms -> 2 轮后超时
        with self.assertRaises(ResponseTimeoutError):
            self.sender.query(mock.Mock(), civresp.parse_freq,
                              timeout_ms=400, reader=reader)

    def test_query_creates_reader_when_none(self):
        """不传 reader 时内部创建并关闭。"""
        fake = FakeReader([_freq_frame(7100000)])
        with mock.patch("rsba1.mailslot.civ_via_execcmd.ResponseReader",
                        return_value=fake):
            result = self.sender.query(mock.Mock(), civresp.parse_freq,
                                       timeout_ms=1000)
        self.assertEqual(result, 7100000)
        self.assertTrue(fake.opened)
        self.assertTrue(fake.closed)

    def test_query_reuses_external_reader(self):
        """传外部 reader 时, query 不负责关闭。"""
        reader = FakeReader([_freq_frame(14270000)])
        self.sender.query(mock.Mock(), civresp.parse_freq,
                          timeout_ms=1000, reader=reader)
        self.assertTrue(reader.opened)
        self.assertFalse(reader.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)