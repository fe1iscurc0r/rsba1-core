"""test_mailslot_mock — Mailslot 协议 + 客户端 mock 测试.

测试范围 (不依赖真实 Mailslot / 硬件):
    1. serialize_command / deserialize_command 往返一致性
    2. payload 边界 (0 / 255 / 256 字节)
    3. cmd_code 边界 (0 / 255 / 256 / 负数)
    4. 9 个已知命令码常量值与映射表
    5. MailslotClient.write_command 用 mock 验证 win32file.WriteFile 调用参数
    6. 异常路径: CreateFile 失败 -> MailslotNotFoundError;
                WriteFile 失败/超时 -> MailslotWriteError/MailslotTimeoutError

运行方式:
    cd d:\\my git\\rs-ba1-reverse
    d:\\my git\\scratchpad\\.venv\\Scripts\\python.exe tests\\test_mailslot_mock.py

依赖:
    - 仅依赖 Python 标准库 (unittest + unittest.mock)
    - 被测代码 rsba1.mailslot.* (src/rsba1/mailslot/)
    - pywin32 已安装 (在 scratchpad/.venv), 但 mock 测试不实际调用 Win32 API
"""

from __future__ import annotations

import os
import sys
import struct
import unittest
from unittest import mock

# 把 src/ 加到 sys.path, 让 rsba1 包可被 import (本仓库无 pyproject.toml)
_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# 延迟导入 client 模块, 便于在测试中 mock win32file (client 模块顶层已 import)
from rsba1.mailslot import protocol as P  # noqa: E402
from rsba1.mailslot import client as C   # noqa: E402
from rsba1.mailslot.client import (       # noqa: E402
    MailslotClient,
    MailslotError,
    MailslotNotFoundError,
    MailslotWriteError,
    MailslotTimeoutError,
    DEFAULT_MAILSLOT_NAME,
)


# ============================================================
# 1. 协议常量与命令码表
# ============================================================

class TestProtocolConstants(unittest.TestCase):
    """协议常量正确性 (与任务规格 / 静态反汇编结果一致)。"""

    def test_header_format_is_little_endian_bbh(self):
        self.assertEqual(P.COMMAND_HEADER_FORMAT, "<BBH")

    def test_header_size_is_4(self):
        self.assertEqual(P.COMMAND_HEADER_SIZE, 4)
        self.assertEqual(struct.calcsize(P.COMMAND_HEADER_FORMAT), 4)

    def test_max_payload_size_is_255(self):
        self.assertEqual(P.MAX_PAYLOAD_SIZE, 255)

    def test_max_packet_size_is_header_plus_max_payload(self):
        self.assertEqual(P.MAX_PACKET_SIZE, P.COMMAND_HEADER_SIZE + P.MAX_PAYLOAD_SIZE)
        self.assertEqual(P.MAX_PACKET_SIZE, 4 + 255)

    def test_nine_command_codes_unique_and_in_range(self):
        codes = [
            P.CMD_GET_COUNT_CLIENT_TRANS,
            P.CMD_GET_CLIENT_TRANS_INFO,
            P.CMD_EXEC_CMD,
            P.CMD_GET_CLIENT_TRANS_VOL,
            P.CMD_GET_CLIENT_TRANS_INFO_2,
            P.CMD_GET_CLIENT_TRANS_VOL_3,
            P.CMD_GET_COMMAND_PROC_COUNT,
            P.CMD_GET_REMOTE_TRANS_NETWORK_SET,
            P.CMD_GET_REMOTE_TRANS_STATE,
        ]
        for c in codes:
            self.assertGreaterEqual(c, 0)
            self.assertLessEqual(c, 255)
        self.assertEqual(len(set(codes)), 9, "9 个 cmd_code 必须两两不同")
        self.assertEqual(len(P.CMD_CODES), 9)
        for c in codes:
            self.assertIn(c, P.CMD_CODES)

    def test_known_cmd_code_values_match_reverse_analysis(self):
        self.assertEqual(P.CMD_GET_COUNT_CLIENT_TRANS, 0x00)
        self.assertEqual(P.CMD_GET_CLIENT_TRANS_INFO, 0x01)
        self.assertEqual(P.CMD_EXEC_CMD, 0x02)
        self.assertEqual(P.CMD_GET_CLIENT_TRANS_VOL, 0x03)
        self.assertEqual(P.CMD_GET_CLIENT_TRANS_INFO_2, 0x04)
        self.assertEqual(P.CMD_GET_CLIENT_TRANS_VOL_3, 0x05)
        self.assertEqual(P.CMD_GET_COMMAND_PROC_COUNT, 0x06)
        self.assertEqual(P.CMD_GET_REMOTE_TRANS_NETWORK_SET, 0x07)
        self.assertEqual(P.CMD_GET_REMOTE_TRANS_STATE, 0x08)

    def test_cmd_name_reverse_mapping(self):
        for code, name in P.CMD_CODES.items():
            self.assertEqual(P.CMD_NAME[name], code)

    def test_expected_data_len_known_codes(self):
        self.assertEqual(P.EXPECTED_DATA_LEN[P.CMD_GET_COUNT_CLIENT_TRANS], 0)
        self.assertEqual(P.EXPECTED_DATA_LEN[P.CMD_GET_CLIENT_TRANS_INFO], 0x6C)
        self.assertEqual(P.EXPECTED_DATA_LEN[P.CMD_GET_CLIENT_TRANS_VOL], 0x24)
        self.assertEqual(P.EXPECTED_DATA_LEN[P.CMD_GET_CLIENT_TRANS_INFO_2], 0x78)
        self.assertEqual(P.EXPECTED_DATA_LEN[P.CMD_GET_CLIENT_TRANS_VOL_3], 0x3C)
        self.assertEqual(P.EXPECTED_DATA_LEN[P.CMD_GET_COMMAND_PROC_COUNT], 0)
        self.assertEqual(P.EXPECTED_DATA_LEN[P.CMD_GET_REMOTE_TRANS_NETWORK_SET], 0x40)
        self.assertEqual(P.EXPECTED_DATA_LEN[P.CMD_GET_REMOTE_TRANS_STATE], 0x1C)
        self.assertIsNone(P.EXPECTED_DATA_LEN[P.CMD_EXEC_CMD])


# ============================================================
# 2. serialize_command / deserialize_command 往返
# ============================================================

class TestSerializeRoundtrip(unittest.TestCase):
    """serialize -> deserialize 一致性。"""

    def _roundtrip(self, cmd_code, payload, reserved=0):
        pkt = P.serialize_command(cmd_code, payload, reserved=reserved)
        self.assertEqual(len(pkt), 4 + len(payload))
        cmd, dlen, res, pl = P.deserialize_command(pkt)
        self.assertEqual(cmd, cmd_code)
        self.assertEqual(dlen, len(payload))
        self.assertEqual(res, reserved & 0xFFFF)
        self.assertEqual(pl, payload)
        return pkt

    def test_roundtrip_empty_payload(self):
        self._roundtrip(0x00, b"")

    def test_roundtrip_small_payload(self):
        self._roundtrip(0x02, b"\x01\x02\x03")

    def test_roundtrip_max_payload_255(self):
        big = b"\xAB" * 255
        self._roundtrip(0x05, big)

    def test_roundtrip_preserves_reserved_nonzero(self):
        self._roundtrip(0x07, b"\x00" * 0x40, reserved=0x1234)

    def test_roundtrip_all_known_cmd_codes(self):
        self._roundtrip(P.CMD_GET_COUNT_CLIENT_TRANS, b"")
        self._roundtrip(P.CMD_GET_CLIENT_TRANS_INFO, b"")
        self._roundtrip(P.CMD_EXEC_CMD, b"\x00" * 32)
        self._roundtrip(P.CMD_GET_CLIENT_TRANS_VOL, b"")
        self._roundtrip(P.CMD_GET_CLIENT_TRANS_INFO_2, b"")
        self._roundtrip(P.CMD_GET_CLIENT_TRANS_VOL_3, b"")
        self._roundtrip(P.CMD_GET_COMMAND_PROC_COUNT, b"")
        self._roundtrip(P.CMD_GET_REMOTE_TRANS_NETWORK_SET, b"")
        self._roundtrip(P.CMD_GET_REMOTE_TRANS_STATE, b"")

    def test_header_byte_layout(self):
        pkt = P.serialize_command(0x42, b"\xAA", reserved=0xBEEF)
        self.assertEqual(pkt[0], 0x42)
        self.assertEqual(pkt[1], 0x01)
        self.assertEqual(pkt[2], 0xEF)
        self.assertEqual(pkt[3], 0xBE)
        self.assertEqual(pkt[4:], b"\xAA")


# ============================================================
# 3. payload 边界 (0 / 255 / 256)
# ============================================================

class TestPayloadBoundaries(unittest.TestCase):

    def test_payload_zero_bytes_ok(self):
        pkt = P.serialize_command(0x01, b"")
        self.assertEqual(len(pkt), 4)
        cmd, dlen, res, pl = P.deserialize_command(pkt)
        self.assertEqual(dlen, 0)
        self.assertEqual(pl, b"")

    def test_payload_255_bytes_ok(self):
        payload = b"\x55" * 255
        pkt = P.serialize_command(0x01, payload)
        self.assertEqual(len(pkt), 4 + 255)
        cmd, dlen, res, pl = P.deserialize_command(pkt)
        self.assertEqual(dlen, 255)
        self.assertEqual(pl, payload)

    def test_payload_256_bytes_raises(self):
        too_big = b"\x00" * 256
        with self.assertRaises(P.PayloadTooLargeError):
            P.serialize_command(0x01, too_big)

    def test_payload_far_too_large_raises(self):
        with self.assertRaises(P.PayloadTooLargeError):
            P.serialize_command(0x01, b"\x00" * 1024)

    def test_payload_bytearray_accepted(self):
        pkt = P.serialize_command(0x01, bytearray(b"\xAA\xBB"))
        self.assertEqual(pkt[4:], b"\xAA\xBB")

    def test_payload_none_treated_as_empty(self):
        pkt = P.serialize_command(0x01, None)
        self.assertEqual(len(pkt), 4)

    def test_payload_wrong_type_raises_typeerror(self):
        with self.assertRaises(TypeError):
            P.serialize_command(0x01, "not bytes")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            P.serialize_command(0x01, 12345)         # type: ignore[arg-type]


# ============================================================
# 4. cmd_code 边界 (0 / 255 / 256 / 负数)
# ============================================================

class TestCmdCodeBoundaries(unittest.TestCase):

    def test_cmd_code_zero_ok(self):
        pkt = P.serialize_command(0, b"")
        self.assertEqual(pkt[0], 0)

    def test_cmd_code_255_ok(self):
        pkt = P.serialize_command(255, b"")
        self.assertEqual(pkt[0], 255)

    def test_cmd_code_256_raises(self):
        with self.assertRaises(P.InvalidCommandCodeError):
            P.serialize_command(256, b"")

    def test_cmd_code_negative_raises(self):
        with self.assertRaises(P.InvalidCommandCodeError):
            P.serialize_command(-1, b"")

    def test_cmd_code_far_over_raises(self):
        with self.assertRaises(P.InvalidCommandCodeError):
            P.serialize_command(0x10000, b"")

    def test_cmd_code_wrong_type_raises_typeerror(self):
        with self.assertRaises(TypeError):
            P.serialize_command("0x01", b"")  # type: ignore[arg-type]


# ============================================================
# 5. deserialize 容错
# ============================================================

class TestDeserializeEdgeCases(unittest.TestCase):

    def test_deserialize_too_short_raises(self):
        with self.assertRaises(P.ProtocolError):
            P.deserialize_command(b"\x01\x02")

    def test_deserialize_empty_raises(self):
        with self.assertRaises(P.ProtocolError):
            P.deserialize_command(b"")

    def test_deserialize_truncated_payload(self):
        pkt = struct.pack("<BBH", 0x05, 10, 0) + b"\xAA\xBB"
        cmd, dlen, res, pl = P.deserialize_command(pkt)
        self.assertEqual(cmd, 0x05)
        self.assertEqual(dlen, 10)
        self.assertEqual(len(pl), 2)
        self.assertEqual(pl, b"\xAA\xBB")

    def test_deserialize_extra_bytes_after_payload(self):
        pkt = P.serialize_command(0x01, b"\xCC") + b"\xDD\xEE"
        cmd, dlen, res, pl = P.deserialize_command(pkt)
        self.assertEqual(dlen, 1)
        self.assertEqual(pl, b"\xCC")


# ============================================================
# 6. MailslotClient 配置 / 默认值
# ============================================================

class TestMailslotClientConfig(unittest.TestCase):
    """客户端配置与默认值, 不调用任何 Win32 API。"""

    def test_default_mailslot_name(self):
        # 逆向确证: RemoteUtility 创建/读的命令 Mailslot 名为 RemoteUtyCtrlCmd
        # (响应 Mailslot 为 RemoteUtyCtrlRes), 见 client.py 文档与 credential_and_session.md。
        self.assertEqual(DEFAULT_MAILSLOT_NAME, r"\\.\mailslot\RemoteUtyCtrlCmd")

    def test_default_backend_is_pywin32_when_available(self):
        if C._HAS_PYWIN32:
            c = MailslotClient()
            self.assertEqual(c.backend, C.BACKEND_PYWIN32)
        else:  # pragma: no cover
            c = MailslotClient()
            self.assertEqual(c.backend, C.BACKEND_CTYPES)

    def test_mailslot_name_normalizes_slashes(self):
        c = MailslotClient(r"\\./mailslot/test")
        self.assertEqual(c.mailslot_name, r"\\.\mailslot\test")

    def test_invalid_backend_raises(self):
        with self.assertRaises(ValueError):
            MailslotClient(backend="nonexistent")

    def test_negative_timeout_raises(self):
        with self.assertRaises(ValueError):
            MailslotClient(write_timeout_ms=-1)

    def test_empty_mailslot_name_raises(self):
        with self.assertRaises(ValueError):
            MailslotClient("")
        with self.assertRaises(ValueError):
            MailslotClient(None)  # type: ignore[arg-type]

    def test_repr_does_not_open(self):
        c = MailslotClient()
        r = repr(c)
        self.assertIn("MailslotClient", r)
        self.assertIn("backend=", r)
        self.assertIsNone(c._handle)


# ============================================================
# 7. MailslotClient.write_command (mock win32file)
# ============================================================

@unittest.skipIf(C.win32file is None, "需要 pywin32 (win32file)")
class TestWriteCommandMocked(unittest.TestCase):
    """mock win32file.CreateFile / WriteFile, 验证调用参数。"""

    def setUp(self):
        self.fake_handle = mock.MagicMock(name="fake_mailslot_handle")

    def test_write_command_calls_writefile_with_serialized_packet(self):
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=self.fake_handle) as mock_create:
            with mock.patch.object(C.win32file, "WriteFile",
                                   return_value=(0, 0)) as mock_write:
                expected_payload = b"\x10\x20\x30"
                expected_packet = P.serialize_command(0x07, expected_payload)
                mock_write.return_value = (0, len(expected_packet))

                c = MailslotClient(r"\\.\mailslot\test_cmd_args")
                n = c.write_command(0x07, expected_payload)

                self.assertEqual(n, len(expected_packet))
                mock_create.assert_called_once()
                mock_write.assert_called_once()
                call_args = mock_write.call_args
                args, kwargs = call_args
                self.assertIs(args[0], self.fake_handle)
                self.assertEqual(args[1], expected_packet)

    def test_write_command_empty_payload(self):
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=self.fake_handle):
            with mock.patch.object(C.win32file, "WriteFile",
                                   return_value=(0, 4)) as mock_write:
                c = MailslotClient()
                n = c.write_command(P.CMD_GET_COUNT_CLIENT_TRANS)
                self.assertEqual(n, 4)
                args = mock_write.call_args.args
                self.assertEqual(args[1], b"\x00\x00\x00\x00")

    def test_write_command_max_payload(self):
        big = b"\x77" * 255
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=self.fake_handle):
            with mock.patch.object(C.win32file, "WriteFile",
                                   return_value=(0, 259)) as mock_write:
                c = MailslotClient()
                n = c.write_command(0x01, big)
                self.assertEqual(n, 259)
                args = mock_write.call_args.args
                self.assertEqual(args[1][:4], b"\x01\xFF\x00\x00")
                self.assertEqual(args[1][4:], big)

    def test_write_command_oversize_payload_raises(self):
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=self.fake_handle):
            with mock.patch.object(C.win32file, "WriteFile",
                                   return_value=(0, 0)) as mock_write:
                c = MailslotClient()
                with self.assertRaises(P.PayloadTooLargeError):
                    c.write_command(0x01, b"\x00" * 256)
                mock_write.assert_not_called()

    def test_write_command_invalid_cmd_code_raises(self):
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=self.fake_handle):
            with mock.patch.object(C.win32file, "WriteFile",
                                   return_value=(0, 0)) as mock_write:
                c = MailslotClient()
                with self.assertRaises(P.InvalidCommandCodeError):
                    c.write_command(256, b"")
                mock_write.assert_not_called()

    def test_write_command_lazy_open_then_close(self):
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=self.fake_handle) as mock_create:
            with mock.patch.object(C.win32file, "WriteFile",
                                   return_value=(0, 4)) as mock_write:
                with mock.patch.object(C.win32file, "CloseHandle") as mock_close:
                    c = MailslotClient()
                    self.assertIsNone(c._handle)
                    n = c.write_command(0x00)
                    self.assertEqual(n, 4)
                    mock_create.assert_called_once()
                    mock_write.assert_called_once()
                    mock_close.assert_called_once()
                    self.assertIsNone(c._handle)

    def test_write_command_reuses_existing_handle(self):
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=self.fake_handle) as mock_create:
            with mock.patch.object(C.win32file, "WriteFile",
                                   return_value=(0, 4)) as mock_write:
                with mock.patch.object(C.win32file, "CloseHandle") as mock_close:
                    with MailslotClient() as c:
                        mock_create.assert_called_once()
                        n = c.write_command(0x00)
                        self.assertEqual(n, 4)
                        self.assertEqual(mock_create.call_count, 1)
                    mock_close.assert_called_once()

    def test_write_command_reserved_field_passed_through(self):
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=self.fake_handle):
            with mock.patch.object(C.win32file, "WriteFile",
                                   return_value=(0, 4)) as mock_write:
                c = MailslotClient()
                c.write_command(0x01, b"", reserved=0xDEAD)
                args = mock_write.call_args.args
                self.assertEqual(args[1], b"\x01\x00\xAD\xDE")

    def test_default_reserved_from_constructor(self):
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=self.fake_handle):
            with mock.patch.object(C.win32file, "WriteFile",
                                   return_value=(0, 4)) as mock_write:
                c = MailslotClient(reserved=0xCAFE)
                c.write_command(0x02, b"")
                args = mock_write.call_args.args
                self.assertEqual(args[1], b"\x02\x00\xFE\xCA")


# ============================================================
# 8. MailslotClient 异常路径 (mock win32file)
# ============================================================

@unittest.skipIf(C.win32file is None, "需要 pywin32 (win32file)")
class TestMailslotClientErrors(unittest.TestCase):
    """CreateFile / WriteFile 失败时的异常分类。"""

    def test_create_file_not_found_raises_mailslot_not_found(self):
        if not C._HAS_PYWIN32:  # pragma: no cover
            self.skipTest("需要 pywin32 才能构造 pywintypes.error")
        import pywintypes
        err = pywintypes.error(2, "CreateFile", "系统找不到指定的文件。")
        with mock.patch.object(C.win32file, "CreateFile", side_effect=err):
            c = MailslotClient(r"\\.\mailslot\nonexistent")
            with self.assertRaises(MailslotNotFoundError) as ctx:
                c.open()
            self.assertEqual(ctx.exception.win_error, 2)

    def test_create_file_access_denied_raises_mailslot_error(self):
        if not C._HAS_PYWIN32:  # pragma: no cover
            self.skipTest("需要 pywin32 才能构造 pywintypes.error")
        import pywintypes
        err = pywintypes.error(5, "CreateFile", "拒绝访问。")
        with mock.patch.object(C.win32file, "CreateFile", side_effect=err):
            c = MailslotClient()
            with self.assertRaises(MailslotError) as ctx:
                c.open()
            self.assertNotIsInstance(ctx.exception, MailslotNotFoundError)
            self.assertEqual(ctx.exception.win_error, 5)

    def test_write_file_failure_raises_write_error(self):
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=mock.MagicMock()):
            with mock.patch.object(C.win32file, "WriteFile",
                                   return_value=(5, 0)):
                c = MailslotClient()
                with self.assertRaises(MailslotWriteError) as ctx:
                    c.write_command(0x01, b"")
                self.assertEqual(ctx.exception.win_error, 5)

    def test_write_file_timeout_raises_timeout_error(self):
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=mock.MagicMock()):
            with mock.patch.object(C.win32file, "WriteFile",
                                   return_value=(C.ERROR_TIMEOUT, 0)):
                c = MailslotClient()
                with self.assertRaises(MailslotTimeoutError) as ctx:
                    c.write_command(0x01, b"")
                self.assertEqual(ctx.exception.win_error, C.ERROR_TIMEOUT)

    def test_write_file_pywintypes_error_raises_write_error(self):
        if not C._HAS_PYWIN32:  # pragma: no cover
            self.skipTest("需要 pywin32 才能构造 pywintypes.error")
        import pywintypes
        err = pywintypes.error(C.ERROR_TIMEOUT, "WriteFile", "超时")
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=mock.MagicMock()):
            with mock.patch.object(C.win32file, "WriteFile", side_effect=err):
                c = MailslotClient()
                with self.assertRaises(MailslotTimeoutError):
                    c.write_command(0x01, b"")

    def test_open_failure_propagates_from_write_command(self):
        if not C._HAS_PYWIN32:  # pragma: no cover
            self.skipTest("需要 pywin32 才能构造 pywintypes.error")
        import pywintypes
        err = pywintypes.error(2, "CreateFile", "not found")
        with mock.patch.object(C.win32file, "CreateFile", side_effect=err):
            with mock.patch.object(C.win32file, "WriteFile") as mock_write:
                c = MailslotClient(r"\\.\mailslot\nonexistent")
                with self.assertRaises(MailslotNotFoundError):
                    c.write_command(0x01, b"")
                mock_write.assert_not_called()

    def test_lazy_open_closed_after_write_failure(self):
        fake_handle = mock.MagicMock()
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=fake_handle):
            with mock.patch.object(C.win32file, "WriteFile",
                                   return_value=(C.ERROR_TIMEOUT, 0)):
                with mock.patch.object(C.win32file, "CloseHandle") as mock_close:
                    c = MailslotClient()
                    with self.assertRaises(MailslotTimeoutError):
                        c.write_command(0x01, b"")
                    mock_close.assert_called_once_with(fake_handle)
                    self.assertIsNone(c._handle)

    def test_close_is_idempotent(self):
        c = MailslotClient()
        c.close()
        c.close()


# ============================================================
# 9. CreateFile 调用参数验证 (mock)
# ============================================================

@unittest.skipIf(C.win32file is None, "需要 pywin32 (win32file)")
class TestCreateFileArguments(unittest.TestCase):
    """验证 MailslotClient 调 CreateFile 时传的参数与逆向结果一致。"""

    def test_create_file_uses_correct_arguments(self):
        # 与 UtyCtrl.dll 0x10001743..0x1000174E 反汇编一致:
        #   GENERIC_WRITE | FILE_SHARE_READ|FILE_SHARE_WRITE | OPEN_EXISTING
        fake_handle = mock.MagicMock()
        with mock.patch.object(C.win32file, "CreateFile",
                               return_value=fake_handle) as mock_create:
            c = MailslotClient(r"\\.\mailslot\test_args")
            c.open()
            mock_create.assert_called_once()
            args, kwargs = mock_create.call_args
            self.assertEqual(args[0], r"\\.\mailslot\test_args")
            self.assertEqual(args[1], C.GENERIC_WRITE)
            self.assertEqual(args[1], 0x40000000)
            self.assertEqual(args[2], C.FILE_SHARE_READ | C.FILE_SHARE_WRITE)
            self.assertEqual(args[2], 3)
            self.assertEqual(args[4], C.OPEN_EXISTING)
            self.assertEqual(args[4], 3)
            self.assertEqual(args[5], C.FILE_ATTRIBUTE_NORMAL)


# ============================================================
# 10. self-test 入口 (可直接 python tests/test_mailslot_mock.py 运行)
# ============================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
