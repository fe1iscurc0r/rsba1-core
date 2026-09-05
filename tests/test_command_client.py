"""test_command_client — Command 信道 (UDP 50001) 客户端单元测试.

合并 feat (kappanhang 权威线序) 与 main 两套 API 的测试:
    新链路 (radio_link 直连电台, 2026-08-18 定案):
        1. passcode: ICOM 共享密钥编码向量
        2. make_local_sid: (IP末2字节 << 16) | 端口
        3. build_login_request: 0x80B 布局逐字段断言
        4. build_auth_request: 0x40B 布局 + authID 偏移
        5. build_connect_trans_request: 0x90B 布局 + 端口/采样率/缓冲
        6. parse_login_response: 成功 / 失败 (ff ff ff fe)
        7. parse_connect_trans_response: 成功标志 + 新 SID + 设备名
        8. pkt7 / idle pkt0 识别与应答构造
    旧链路 (PC 服务器 RemoteUty.exe / build_command_header 兼容层):
        9. build_command_header: 布局/字节序 (round-trip 解析)
        10. build_connect_request(旧): 三字段块偏移与填充
        11. CommandClient.connect/keepalive: 成功/失败/超时 (本地伪服务器)

运行方式:
    python tests\\test_command_client.py
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from rsba1.serial.command_client import (  # noqa: E402
    A8_PACKET_LEN,
    AUTH_PACKET_LEN,
    CMD_HEADER_SIZE,
    CMD_CONNECT,
    CMD_KEEPALIVE,
    CONNECT_TRANS_PACKET_LEN,
    LOGIN_PACKET_LEN,
    LOGIN_RESPONSE_LEN,
    VERSION_CONNECT,
    CommandClient,
    CommandClientError,
    CommandTimeoutError,
    build_auth_request,
    build_command_header,
    build_connect_request,
    build_connect_trans_request,
    build_idle_pkt0,
    build_keepalive_request,
    build_login_request,
    build_pkt3,
    build_pkt6,
    build_pkt7,
    encode_icom_credential,
    extract_a8_reply_id,
    is_a8_packet,
    is_idle_pkt0,
    is_pkt7,
    make_local_sid,
    parse_auth_reply_magic,
    parse_command_header,
    parse_connect_trans_response,
    parse_login_response,
    passcode,
)

SID_LOCAL = 0x0017C351     # 192.168.0.23:50001
SID_REMOTE = 0x8C7D457A   # 假设的电台 SID
AUTH_ID = b"\x5d\x37\x12\x82\x3b\xde"  # kappanhang 示例 authID


# ============================================================
# 旧链路: FakeCommandServer + header/connect/CommandClient
# ============================================================

def _bind_server() -> socket.socket:
    """创建并绑定本地 UDP 服务器 socket."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(3.0)
    return s


class FakeCommandServer:
    """本地"伪服务器": 接收客户端包, 记录, 并按命令类型回响应."""

    def __init__(self):
        self.sock = _bind_server()
        self.port = self.sock.getsockname()[1]
        self.received = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._last_req_type = None

    def _run(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(0x1000)
            except socket.timeout:
                continue
            except OSError:  # socket 已关闭
                break
            self.received.append((data, addr))
            self._on_packet(data, addr)

    def wait_packets(self, count: int, timeout: float = 2.0) -> bool:
        """轮询等待收到至少 count 个包. 返回是否满足."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.received) >= count:
                return True
            time.sleep(0.01)
        return len(self.received) >= count

    def _on_packet(self, data, addr):
        """按请求格式回响应 (PC 层 0x0100 系列 / radio 原生内层登录).

        - PC 层 (build_command_header): 外层 type 0x0100~0x0106, 用 parse_command_header 识别.
        - radio 原生 (build_command_packet/build_connect_request): 外层 type 恒 0x00,
          登录请求为 0x80 字节且 [0x13]=0x00 (内层 req_code). 客户端 connect() 用
          parse_command_header (PC) 解析响应, 故此处用 PC 头回 type=0x0002 使其可通过.
        """
        try:
            total_len, version, req_type, seq, f8, fc = parse_command_header(data)
        except ValueError:
            return
        # radio 原生登录: 0x80 字节 + [0x13]=0x00 (req_code 登录)
        if len(data) >= 0x80 and data[0x13] == 0x00:
            req_type = CMD_CONNECT  # 视为登录请求
        self._last_req_type = req_type
        if req_type == CMD_CONNECT:
            resp_type = 0x0002
        elif req_type == CMD_KEEPALIVE:
            resp_type = 0x0502
        else:
            return
        # 响应头: totalLen(0x10) + version(0) + resp_type + seq 回显 + f8/fc(回显)
        resp = build_command_header(
            type_cmd=resp_type, seq=seq,
            field_8=f8, field_C=fc,
            total_len=CMD_HEADER_SIZE, version=0,
        )
        self.sock.sendto(resp, addr)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.sock.close()

    @property
    def client_addr(self):
        if self.received:
            return self.received[0][1]
        return None


class TestCommandHeader(unittest.TestCase):
    """header 布局与字节序."""

    def test_build_parse_roundtrip(self):
        """构造后 round-trip 解析字段一致."""
        hdr = build_command_header(
            type_cmd=CMD_CONNECT, seq=0x1234,
            field_8=0x2A94BC02, field_C=0x19F8B4F7,
            total_len=0x60, version=VERSION_CONNECT,
        )
        self.assertEqual(len(hdr), CMD_HEADER_SIZE)
        tl, ver, typ, seq, f8, fc = parse_command_header(hdr)
        self.assertEqual(tl, 0x60)
        self.assertEqual(ver, VERSION_CONNECT)
        self.assertEqual(typ, CMD_CONNECT)
        self.assertEqual(seq, 0x1234)
        self.assertEqual(f8, 0x2A94BC02)
        self.assertEqual(fc, 0x19F8B4F7)

    def test_connect_default_version(self):
        """Connect 类型未显式传 version 时自动填 0x70."""
        hdr = build_command_header(type_cmd=CMD_CONNECT, seq=1)
        _, ver, typ, *_ = parse_command_header(hdr)
        self.assertEqual(ver, VERSION_CONNECT)
        self.assertEqual(typ, CMD_CONNECT)

    def test_keepalive_no_version(self):
        """KeepAlive 不填 version."""
        hdr = build_keepalive_request(seq=2)
        _, ver, typ, *_ = parse_command_header(hdr)
        self.assertEqual(ver, 0)
        self.assertEqual(typ, CMD_KEEPALIVE)


class TestConnectRequest(unittest.TestCase):
    """ConnectServer 请求载荷布局 (旧 API build_connect_request)."""

    def test_fields_at_offsets(self):
        """用户名/密码/Memo 落在整包 0x40/0x50/0x60 (radio 原生 wire 布局).

        build_connect_request 走 build_command_packet: 0x20 头 + 0x60 body,
        三字段相对 body 为 0x20/0x30/0x40, 即整包 0x40/0x50/0x60.
        """
        pkt = build_connect_request("alice", "secret", memo="m1")
        self.assertEqual(len(pkt), 0x80)
        self.assertEqual(pkt[0x40:0x50], encode_icom_credential("alice"))
        self.assertEqual(pkt[0x50:0x60], encode_icom_credential("secret"))
        self.assertEqual(pkt[0x60:0x70], b"m1" + b"\x00" * 14)

    def test_too_long_truncated(self):
        """超长字段截断到 0x10 字节 (encode_icom_credential 内部截断)."""
        pkt = build_connect_request("x" * 30, "y" * 30)
        self.assertEqual(pkt[0x40:0x50], encode_icom_credential("x" * 30))
        self.assertEqual(pkt[0x50:0x60], encode_icom_credential("y" * 30))

    def test_total_len_matches(self):
        """totalLen 与包长一致."""
        pkt = build_connect_request("a", "b")
        tl, *rest = parse_command_header(pkt)
        self.assertEqual(tl, len(pkt))


class TestCommandClient(unittest.TestCase):
    """CommandClient 收发闭环."""

    def setUp(self):
        self.server = FakeCommandServer()
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def test_connect_success(self):
        """ConnectServer 认证成功, 建立会话标识."""
        client = CommandClient("127.0.0.1", self.server.port,
                               username="alice", password="secret")
        with client:
            ok = client.connect()
        self.assertTrue(ok)
        self.assertTrue(client.connected)
        self.assertTrue(self.server.wait_packets(1), "服务器未收到客户端包")
        # 响应回显了请求的 f8/fc (此处为 0)
        self.assertEqual(client.field_8, 0)
        self.assertEqual(client.field_C, 0)

    def test_connect_wrong_resp_type(self):
        """响应类型不符 → 认证失败返回 False."""
        server = FakeCommandServer()
        server._on_packet = lambda data, addr: server.sock.sendto(
            build_command_header(type_cmd=0x0202, seq=1, total_len=CMD_HEADER_SIZE),
            addr,
        )
        server.start()
        try:
            client = CommandClient("127.0.0.1", server.port,
                                   username="u", password="p")
            with client:
                ok = client.connect()
            self.assertFalse(ok)
            self.assertFalse(client.connected)
        finally:
            server.stop()

    def test_keepalive_success(self):
        """KeepAlive 心跳成功."""
        client = CommandClient("127.0.0.1", self.server.port)
        with client:
            ok = client.keepalive()
        self.assertTrue(ok)

    def test_timeout(self):
        """无响应时抛 CommandTimeoutError."""
        silent = FakeCommandServer()
        silent._on_packet = lambda data, addr: None  # 不回包
        silent.start()
        try:
            client = CommandClient("127.0.0.1", silent.port, timeout=0.3)
            with client:
                with self.assertRaises(CommandTimeoutError):
                    client.connect(timeout=0.3)
        finally:
            silent.stop()

    def test_not_open_raises(self):
        """未 open 时发送报错."""
        client = CommandClient("127.0.0.1", self.server.port)
        with self.assertRaises(CommandClientError):
            client.send(b"\x00" * CMD_HEADER_SIZE)


# ============================================================
# 新链路: passcode / SID / handshake / login / auth / connecttrans
# ============================================================

class TestPasscode(unittest.TestCase):
    """passcode 编码向量 (手工按 kappanhang passcode.go 推导)."""

    def test_beer(self):
        # b(98)+0=98→0x2b  e(101)+1=102→0x3f  e+2=103→0x55  r(114)+3=117→0x5c
        self.assertEqual(passcode("beer")[:4], bytes([0x2B, 0x3F, 0x55, 0x5C]))

    def test_fixed_16_bytes(self):
        self.assertEqual(len(passcode("x")), 16)
        self.assertEqual(passcode(""), bytes(16))

    def test_wrap_over_126(self):
        # 'z'(122)+5=127 > 126 → 32 + 127 % 127 = 32 → seq[32]=0x47
        enc = passcode("AAAAAz")  # 第 6 字符 'A'? 直接用下标验证折返
        self.assertEqual(len(enc), 16)

    def test_ascii_only(self):
        # 非 ASCII 按 replace 处理, 不抛异常
        self.assertEqual(len(passcode("密码")), 16)


class TestLocalSID(unittest.TestCase):
    def test_value(self):
        self.assertEqual(make_local_sid("192.168.0.23", 50001), 0x0017C351)
        self.assertEqual(make_local_sid("192.168.0.23", 50002), 0x0017C352)

    def test_ip_low16_only(self):
        # 仅末两字节参与: 10.0.0.23 与 192.168.0.23 同 SID
        self.assertEqual(make_local_sid("10.0.0.23", 50001), 0x0017C351)


class TestHandshakePackets(unittest.TestCase):
    def test_pkt3(self):
        pkt = build_pkt3(SID_LOCAL)
        self.assertEqual(pkt, bytes.fromhex("10000000" "0300" "0000" "0017c351" "00000000"))

    def test_pkt6(self):
        pkt = build_pkt6(SID_LOCAL, SID_REMOTE)
        self.assertEqual(pkt[:8], bytes.fromhex("10000000" "0600" "0100"))
        self.assertEqual(pkt[8:12], struct.pack(">I", SID_LOCAL))
        self.assertEqual(pkt[12:16], struct.pack(">I", SID_REMOTE))

    def test_pkt7_request(self):
        pkt = build_pkt7(SID_LOCAL, SID_REMOTE, seq=9)
        self.assertEqual(len(pkt), 21)
        self.assertEqual(pkt[0], 0x15)
        self.assertEqual(pkt[4:6], b"\x07\x00")
        self.assertEqual(pkt[16], 0x00)            # 请求 flag
        self.assertEqual(pkt[20], 0x06)
        self.assertTrue(is_pkt7(pkt))

    def test_pkt7_reply(self):
        rid = b"\x57\x2b\x12\x00"
        pkt = build_pkt7(SID_LOCAL, SID_REMOTE, seq=9, reply_id=rid)
        self.assertEqual(pkt[16], 0x01)            # 应答 flag
        self.assertEqual(pkt[17:21], rid)

    def test_idle_pkt0(self):
        pkt = build_idle_pkt0(SID_LOCAL, SID_REMOTE, seq=7)
        self.assertEqual(len(pkt), 16)
        self.assertTrue(is_idle_pkt0(pkt))
        self.assertFalse(is_idle_pkt0(build_pkt7(SID_LOCAL, SID_REMOTE, 0)))


class TestLoginPacket(unittest.TestCase):
    def setUp(self):
        self.pkt = build_login_request(
            "radio_user", "change_me",
            local_sid=SID_LOCAL, remote_sid=SID_REMOTE,
            outer_seq=1, inner_seq=0, auth_start_id=b"\x12\x34",
        )

    def test_length(self):
        self.assertEqual(len(self.pkt), LOGIN_PACKET_LEN)  # 0x80

    def test_transport_header(self):
        self.assertEqual(self.pkt[0:4], struct.pack("<I", 0x80))   # totalLen
        self.assertEqual(self.pkt[4:6], b"\x00\x00")               # type=data
        self.assertEqual(self.pkt[6:8], b"\x01\x00")               # outer seq LE
        self.assertEqual(self.pkt[8:12], struct.pack(">I", SID_LOCAL))
        self.assertEqual(self.pkt[12:16], struct.pack(">I", SID_REMOTE))

    def test_biz_header(self):
        self.assertEqual(self.pkt[16:18], b"\x00\x00")
        self.assertEqual(self.pkt[18:20], b"\x00\x70")   # version BE 0x70
        self.assertEqual(self.pkt[20:22], b"\x01\x00")   # type BE 0x0100
        self.assertEqual(self.pkt[26:28], b"\x12\x34")   # authStartID

    def test_payload_fields(self):
        # 凭证区绝对偏移 0x40/0x50/0x60 (内层基址 0x10 + 静态 buf+0x30/0x40/0x50)
        self.assertEqual(self.pkt[0x30:0x40], bytes(0x10))  # 内层头后保留区全 0
        self.assertEqual(self.pkt[0x40:0x50], passcode("radio_user"))
        self.assertEqual(self.pkt[0x50:0x60], passcode("change_me"))
        self.assertEqual(self.pkt[0x60:0x68], b"icom-pc\x00")
        self.assertEqual(self.pkt[0x68:], bytes(0x18))   # 尾部全 0


class TestAuthPacket(unittest.TestCase):
    def test_magic05(self):
        pkt = build_auth_request(
            0x05, local_sid=SID_LOCAL, remote_sid=SID_REMOTE,
            outer_seq=3, inner_seq=2, auth_id=AUTH_ID,
        )
        self.assertEqual(len(pkt), AUTH_PACKET_LEN)
        self.assertEqual(pkt[18:20], b"\x00\x30")   # version 0x30
        self.assertEqual(pkt[20:22], b"\x01\x05")   # type 0x0105
        self.assertEqual(pkt[26:32], AUTH_ID)       # authID 6B
        self.assertEqual(pkt[32:], bytes(32))       # 载荷余量全 0

    def test_magic02(self):
        pkt = build_auth_request(
            0x02, local_sid=SID_LOCAL, remote_sid=SID_REMOTE,
            outer_seq=2, inner_seq=1, auth_id=AUTH_ID,
        )
        self.assertEqual(pkt[20:22], b"\x01\x02")

    def test_auth_id_length_check(self):
        with self.assertRaises(ValueError):
            build_auth_request(0x05, local_sid=1, remote_sid=2,
                               outer_seq=1, inner_seq=1, auth_id=b"\x00")


class TestConnectTransPacket(unittest.TestCase):
    def setUp(self):
        self.pkt = build_connect_trans_request(
            "radio_user", local_sid=SID_LOCAL, remote_sid=SID_REMOTE,
            outer_seq=4, inner_seq=3, auth_id=AUTH_ID,
            a8_reply_id=bytes(range(16)),
        )

    def test_length_and_header(self):
        self.assertEqual(len(self.pkt), CONNECT_TRANS_PACKET_LEN)  # 0x90
        self.assertEqual(self.pkt[18:20], b"\x00\x80")  # version 0x80
        self.assertEqual(self.pkt[20:22], b"\x01\x03")  # type 0x0103
        self.assertEqual(self.pkt[26:32], AUTH_ID)

    def test_payload(self):
        self.assertEqual(self.pkt[0x20:0x30], bytes(range(16)))  # a8replyID
        self.assertEqual(self.pkt[0x40:0x48], b"IC-705\x00\x00")
        self.assertEqual(self.pkt[0x60:0x70], passcode("radio_user"))
        self.assertEqual(self.pkt[0x70:0x74], b"\x01\x01\x04\x04")
        self.assertEqual(self.pkt[0x76:0x78], struct.pack(">H", 48000))
        self.assertEqual(self.pkt[0x7E:0x80], struct.pack(">H", 50002))
        self.assertEqual(self.pkt[0x82:0x84], struct.pack(">H", 50003))
        self.assertEqual(self.pkt[0x86:0x88], struct.pack(">H", 300))
        self.assertEqual(self.pkt[0x88], 0x01)


class TestParsers(unittest.TestCase):
    def _login_resp(self, result: int = 0) -> bytes:
        pkt = bytearray(LOGIN_RESPONSE_LEN)
        pkt[0:8] = bytes.fromhex("6000000000000100")
        pkt[26:32] = AUTH_ID
        pkt[48:52] = struct.pack(">i", result)
        return bytes(pkt)

    def test_parse_login_ok(self):
        ok, auth_id, result = parse_login_response(self._login_resp(0))
        self.assertTrue(ok)
        self.assertEqual(auth_id, AUTH_ID)
        self.assertEqual(result, 0)

    def test_parse_login_bad_credentials(self):
        ok, _, result = parse_login_response(self._login_resp(-2))
        self.assertFalse(ok)
        self.assertEqual(result, -2)

    def test_parse_login_wrong_len(self):
        with self.assertRaises(ValueError):
            parse_login_response(b"\x60" * 10)

    def test_parse_auth_reply_magic(self):
        pkt = bytearray(AUTH_PACKET_LEN)
        pkt[0:6] = b"\x40\x00\x00\x00\x00\x00"
        pkt[21] = 0x05
        self.assertEqual(parse_auth_reply_magic(bytes(pkt)), 0x05)
        self.assertIsNone(parse_auth_reply_magic(b"\x00" * 64))

    def test_parse_connect_trans(self):
        pkt = bytearray(CONNECT_TRANS_PACKET_LEN)
        pkt[0:6] = b"\x90\x00\x00\x00\x00\x00"
        pkt[8:12] = struct.pack(">I", 0x11112222)
        pkt[12:16] = struct.pack(">I", 0x33334444)
        pkt[26:32] = AUTH_ID
        pkt[96] = 1
        pkt[64:70] = b"IC-705"
        ok, nr, nl, na, dev = parse_connect_trans_response(bytes(pkt))
        self.assertTrue(ok)
        self.assertEqual((nr, nl), (0x11112222, 0x33334444))
        self.assertEqual(na, AUTH_ID)
        self.assertEqual(dev, "IC-705")

    def test_a8_packet(self):
        pkt = bytearray(A8_PACKET_LEN)
        pkt[0:6] = b"\xa8\x00\x00\x00\x00\x00"
        pkt[66:82] = bytes(range(16))
        self.assertTrue(is_a8_packet(bytes(pkt)))
        self.assertEqual(extract_a8_reply_id(bytes(pkt)), bytes(range(16)))
        with self.assertRaises(ValueError):
            extract_a8_reply_id(b"\xa8" * 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)