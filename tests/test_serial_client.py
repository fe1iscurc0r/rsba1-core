"""test_serial_client — Serial 信道 UDP 客户端单元测试 (本地回环, 不依赖硬件).

测试范围:
    1. 发送测序: wire 头 seq / Serial 帧 sseq 递增
    2. 发送字节布局: 与线上样本一致 (wire 头 + Serial 帧 + CI-V)
    3. read_civ_response: 跳 keepalive, 提取 CI-V 响应帧
    4. read_civ_response: 超时抛 SerialTimeoutError
    5. 高层命令: send_read_freq / send_ptt_on 等

实现:
    用本地 UDP socket 作为"伪服务器", 绑定临时端口, 接收客户端包并回包,
    验证 SerialClient 的收发闭环。全程不回环到真实 RS-BA1。

运行方式:
    python tests\\test_serial_client.py

依赖:
    - 仅 Python 标准库 (unittest + socket)
    - 被测代码 rsba1.serial.serial_client + rsba1.serial.serial_codec
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import unittest

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from rsba1.serial.serial_codec import (  # noqa: E402
    WIRE_HEADER_SIZE,
    UDP2_PKT_TYPE_DATA,
    UDP2_PKT_TYPE_PKT3,
    UDP2_PKT_TYPE_PKT4,
    UDP2_PKT_TYPE_PKT6,
    UDP2_PKT_TYPE_KEEPALIVE,
    build_wire_header,
    parse_wire_header,
    parse_serial_frame,
)

from rsba1.serial.serial_client import (  # noqa: E402
    SerialClient,
    SerialClientError,
    SerialTimeoutError,
)

# 伪服务器本地会话标识; pkt4/pkt6 回传给客户端, 作为客户端 remoteSID (field_C)
FAKE_SERVER_SID = 0x1234ABCD


def _bind_server() -> socket.socket:
    """创建并绑定本地 UDP 服务器 socket, 返回 (sock, port)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(3.0)
    return s


class FakeServer:
    """本地"伪服务器": 接收客户端包, 记录, 并按需回 CI-V 响应."""

    def __init__(self):
        self.sock = _bind_server()
        self.port = self.sock.getsockname()[1]
        self.received = []
        self.respond_civ = True  # False → 不回应答 (测超时)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(0x1000)
            except socket.timeout:
                continue
            except OSError:  # socket 已关闭
                break
            try:
                wire, _ = parse_wire_header(data)
            except ValueError:
                continue
            # 握手/控制包 (pkt3/4/6) 不记录, 仅即时应答以完成会话握手
            if wire.type != UDP2_PKT_TYPE_DATA:
                self._on_ctrl(wire, addr)
                continue
            # 数据包: 记录并应答 CI-V (会话会话已由握手建立, 无需注册包先导)
            self.received.append((data, addr))
            self._on_data(wire, addr)

    def wait_packets(self, count: int, timeout: float = 2.0) -> bool:
        """轮询等待收到至少 count 个包. 返回是否满足."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.received) >= count:
                return True
            time.sleep(0.01)
        return len(self.received) >= count

    def _on_ctrl(self, wire, addr):
        """应答传输层握手包: pkt3 → pkt4, pkt6 → pkt6 (kappanhang streamCommon)."""
        if wire.type == UDP2_PKT_TYPE_PKT3:
            resp = build_wire_header(
                type=UDP2_PKT_TYPE_PKT4,
                field_8=FAKE_SERVER_SID, field_C=wire.field_8, payload_len=0,
            )
            self.sock.sendto(resp, addr)
        elif wire.type == UDP2_PKT_TYPE_PKT6:
            resp = build_wire_header(
                type=UDP2_PKT_TYPE_PKT6,
                field_8=FAKE_SERVER_SID, field_C=wire.field_8, payload_len=0,
            )
            self.sock.sendto(resp, addr)

    def _on_data(self, wire, addr):
        """对数据包回一条 CI-V 响应 (读频率应答); respond_civ=False 时静默."""
        if not self.respond_civ:
            return
        resp_civ = bytes.fromhex("fe fee0a42600050001fd")
        frame = b"\xc1" + (len(resp_civ)).to_bytes(2, "little") + (0x1EA3).to_bytes(2, "big") + resp_civ
        wire_resp = build_wire_header(
            type=UDP2_PKT_TYPE_DATA, seq=0x9821,
            field_8=wire.field_C, field_C=wire.field_8,  # 对调 field_8/field_C (握手交换)
            payload_len=len(frame),
        )
        self.sock.sendto(wire_resp + frame, addr)

    def send_keepalive(self, addr):
        """向客户端发一条 keepalive (type=7), 用于测试跳过逻辑."""
        ka = (
            (0x10).to_bytes(4, "little")
            + UDP2_PKT_TYPE_KEEPALIVE.to_bytes(2, "little")
            + (0xD9BE).to_bytes(2, "little")
            + (0x2A94BC02).to_bytes(4, "little")
            + (0x19F8B4F7).to_bytes(4, "little")
        )
        self.sock.sendto(ka, addr)

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


class TestSerialClientSend(unittest.TestCase):
    """发送侧: 包布局与序号递增."""

    def setUp(self):
        self.server = FakeServer()
        self.server.start()
        self.client = SerialClient("127.0.0.1", self.server.port)
        self.client.open()

    def tearDown(self):
        self.client.close()
        self.server.stop()

    def test_send_read_freq_layout(self):
        """发送读频率, 包布局 = wire 头 + Serial 帧 + CI-V."""
        n = self.client.send_read_freq()
        self.assertGreater(n, WIRE_HEADER_SIZE)
        self.assertTrue(self.server.wait_packets(1), "服务器未收到客户端包")
        self.assertEqual(len(self.server.received), 1)
        data, _ = self.server.received[0]
        wire, total_len = parse_wire_header(data)
        self.assertEqual(wire.type, UDP2_PKT_TYPE_DATA)
        self.assertEqual(total_len, len(data))
        frame = parse_serial_frame(data[WIRE_HEADER_SIZE:])
        self.assertTrue(frame.bulk)
        # CI-V 帧: FE FE A4 00 03 FD
        self.assertTrue(frame.payload.startswith(b"\xfe\xfe\xa4\x00\x03"))

    def test_seq_increments(self):
        """两次发送, wire 头 seq 与 Serial 帧 sseq 各 +1."""
        self.client.send_read_freq()
        self.client.send_read_freq()
        self.assertTrue(self.server.wait_packets(2), "服务器未收到足够的客户端包")
        self.assertEqual(len(self.server.received), 2)
        wires = []
        frames = []
        for data, _ in self.server.received:
            w, _ = parse_wire_header(data)
            f = parse_serial_frame(data[WIRE_HEADER_SIZE:])
            wires.append(w.seq)
            frames.append(f.sseq)
        self.assertEqual(wires[1], (wires[0] + 1) & 0xFFFF)
        self.assertEqual(frames[1], (frames[0] + 1) & 0xFFFF)

    def test_send_ptt_on_body(self):
        """send_ptt_on 发送 PTT ON 命令体."""
        self.client.send_ptt_on()
        self.assertTrue(self.server.wait_packets(1), "服务器未收到客户端包")
        data, _ = self.server.received[0]
        frame = parse_serial_frame(data[WIRE_HEADER_SIZE:])
        # PTT ON 完整帧: FE FE A4 00 1C 00 01 FD
        self.assertTrue(frame.payload.endswith(b"\x1c\x00\x01\xfd"))


class TestSerialClientRecv(unittest.TestCase):
    """接收侧: 响应解析与超时."""

    def setUp(self):
        self.server = FakeServer()
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def test_read_civ_response(self):
        """读取 CI-V 响应帧."""
        client = SerialClient("127.0.0.1", self.server.port, timeout=2.0)
        with client:
            client.send_read_freq()
            resp = client.read_civ_response(timeout=2.0)
        self.assertEqual(resp, bytes.fromhex("fe fee0a42600050001fd"))

    def test_read_skips_keepalive(self):
        """跳 keepalive 直到收到 CI-V 响应."""
        client = SerialClient("127.0.0.1", self.server.port, timeout=2.0)
        with client:
            client.send_read_freq()
            self.assertTrue(self.server.wait_packets(1), "服务器未收到客户端包")
            # 先发一条 keepalive, 再靠服务器默认回 CI-V 响应
            self.server.send_keepalive(self.server.client_addr)
            resp = client.read_civ_response(timeout=2.0)
        self.assertEqual(resp, bytes.fromhex("fe fee0a42600050001fd"))

    def test_read_timeout(self):
        """无响应时抛 SerialTimeoutError."""
        # 用不回应答 (respond_civ=False) 的服务器; 握手仍应答, 故 open() 正常
        silent = FakeServer()
        silent.respond_civ = False
        silent.start()
        try:
            client = SerialClient("127.0.0.1", silent.port, timeout=0.3)
            with client:
                client.send_read_freq()
                with self.assertRaises(SerialTimeoutError):
                    client.read_civ_response(timeout=0.3)
        finally:
            silent.stop()

    def test_not_open_raises(self):
        """未 open 时发送报错."""
        client = SerialClient("127.0.0.1", self.server.port)
        with self.assertRaises(SerialClientError):
            client.send_read_freq()


if __name__ == "__main__":
    unittest.main(verbosity=2)