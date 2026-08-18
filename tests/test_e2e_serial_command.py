"""test_e2e_serial_command — Serial/Command 双信道 E2E 集成测试 (回环伪服务器 + 真机门控).

覆盖用户视角的完整闭环: 先 CommandClient 登录 + 心跳, 再 SerialClient 下发 CI-V 并读应答.

- 回环 E2E: 用本文件内 `E2EFakeServer` 忠实实现已确证的 wire 格式 (见
  re/protocols/serial_channel.md / command_channel_cmd.md / capture_todo.md),
  驱动真实 `CommandClient` / `SerialClient` 走全链路。
- 真机 E2E: 受环境变量 `RSBA1_E2E_HOST` 门控, 设置后才连真实 RS-BA1 服务器
  (IC-705 需在上电 + 服务器运行 + 会话可建立时手动执行), 默认跳过。

运行方式:
    python tests\\test_e2e_serial_command.py            # 回环 E2E (真机项跳过)
    $env:RSBA1_E2E_HOST="192.168.0.23"; python tests\\test_e2e_serial_command.py

依赖:
    - 仅 Python 标准库 (unittest + socket)
    - 被测代码 rsba1.serial.command_client / serial_client / serial_codec
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
    CMD_HEADER_SIZE,
    CMD_CONNECT,
    CMD_KEEPALIVE,
    CommandClient,
    build_command_header,
    parse_command_header,
)
from rsba1.serial.serial_codec import (  # noqa: E402
    WIRE_HEADER_SIZE,
    UDP2_PKT_TYPE_DATA,
    UDP2_PKT_TYPE_PKT3,
    UDP2_PKT_TYPE_PKT4,
    UDP2_PKT_TYPE_PKT6,
    parse_wire_header,
    parse_serial_frame,
    build_wire_header,
    build_serial_frame,
)
from rsba1.serial.serial_client import (  # noqa: E402
    SerialClient,
    SerialTimeoutError,
)

# 服务器回环地址
LOOPBACK = "127.0.0.1"

# 读频率 CI-V 应答示例 (与 unit 测试一致; 频率值非语义断言)
CIV_READ_FREQ_RESP = bytes.fromhex("fe fee0a42600050001fd")


def _bind_udp() -> socket.socket:
    """创建并绑定回环 UDP socket."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((LOOPBACK, 0))
    s.settimeout(3.0)
    return s


def _local_sid(port: int, ip: str = LOOPBACK) -> int:
    """计算 127.0.0.1:<port> 的 localSID ((IP末两字节<<16)|port), 与 SerialClient 对齐."""
    ip_val = struct.unpack(">I", socket.inet_aton(ip))[0]
    return ((ip_val & 0xFFFF) << 16) | (port & 0xFFFF)


class E2EFakeServer:
    """忠实实现已确证 wire 格式的伪 RS-BA1 双信道服务器 (回环).

    - ctrl  socket: 处理 CommandClient (radio 原生登录 + PC 心跳), 按 connect() 的
      parse_command_header (PC) 解析方式回 type=0x0002 / 0x0502.
    - serial socket: 处理 SerialClient 的 pkt3/pkt4/pkt6 会话握手 (回 pkt4/pkt6)
      及数据包 (对任意 CI-V 请求回一条读频率应答).
    """

    def __init__(self):
        self.ctrl_sock = _bind_udp()
        self.serial_sock = _bind_udp()
        self.ctrl_port = self.ctrl_sock.getsockname()[1]
        self.serial_port = self.serial_sock.getsockname()[1]
        self.serial_local_sid = _local_sid(self.serial_port)  # 握手 pkt4/6 回传的服务器 localSID
        self._stop = threading.Event()
        self._threads = []
        self.ctrl_packets = []
        self.serial_packets = []

    # ---- 线程 ----
    def start(self):
        self._threads = [
            threading.Thread(target=self._ctrl_loop, daemon=True),
            threading.Thread(target=self._serial_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
        for s in (self.ctrl_sock, self.serial_sock):
            try:
                s.close()
            except OSError:
                pass

    def _recv(self, sock, store):
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(0x1000)
            except socket.timeout:
                continue
            except OSError:
                break
            store.append((data, addr))

    def _ctrl_loop(self):
        def handler(data, addr):
            self.ctrl_packets.append((data, addr))
            try:
                total_len, version, req_type, seq, f8, fc = parse_command_header(data)
            except ValueError:
                return
            # radio 原生登录: 0x80 字节 + [0x13]=0x00 (req_code 登录)
            if len(data) >= 0x80 and data[0x13] == 0x00:
                resp_type = 0x0002
            elif req_type == CMD_KEEPALIVE:
                resp_type = 0x0502
            else:
                return
            resp = build_command_header(
                type_cmd=resp_type, seq=seq, field_8=f8, field_C=fc,
                total_len=CMD_HEADER_SIZE, version=0,
            )
            self.ctrl_sock.sendto(resp, addr)
        self._loop(self.ctrl_sock, handler)

    def _serial_loop(self):
        def handler(data, addr):
            self.serial_packets.append((data, addr))
            try:
                wire, _ = parse_wire_header(data)
            except ValueError:
                return
            # 会话握手控制包: pkt3 → 回 pkt4 (field_8=服务器 localSID);
            # pkt6 → 回 pkt6 确认. 均回传服务器 localSID/对端回显.
            if wire.type == UDP2_PKT_TYPE_PKT3:
                resp = build_wire_header(
                    type=UDP2_PKT_TYPE_PKT4, seq=0,
                    field_8=self.serial_local_sid, field_C=wire.field_8, payload_len=0,
                )
                self.serial_sock.sendto(resp, addr)
                return
            if wire.type == UDP2_PKT_TYPE_PKT6:
                resp = build_wire_header(
                    type=UDP2_PKT_TYPE_PKT6, seq=wire.seq,
                    field_8=self.serial_local_sid, field_C=wire.field_8, payload_len=0,
                )
                self.serial_sock.sendto(resp, addr)
                return
            # 数据包: 回一条读频率应答: wire 头 + Serial 帧 + CI-V 应答
            frame = build_serial_frame(CIV_READ_FREQ_RESP, sseq=0x0001, bulk=True)
            resp = build_wire_header(
                type=UDP2_PKT_TYPE_DATA, seq=(wire.seq + 1) & 0xFFFF,
                field_8=wire.field_C, field_C=wire.field_8,
                payload_len=len(frame),
            ) + frame
            self.serial_sock.sendto(resp, addr)
        self._loop(self.serial_sock, handler)

    def _loop(self, sock, handler):
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(0x1000)
            except socket.timeout:
                continue
            except OSError:
                break
            handler(data, addr)


class E2ERoundtripTest(unittest.TestCase):
    """回环 E2E: CommandClient 登录/心跳 + SerialClient CI-V 收发."""

    def setUp(self):
        self.server = E2EFakeServer()
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def test_login_keepalive_then_read_freq(self):
        """全链路: 登录 → 心跳 → 读频率 → 收应答."""
        cmd = CommandClient(LOOPBACK, self.server.ctrl_port,
                            username="alice", password="secret")
        with cmd:
            self.assertTrue(cmd.connect(), "CommandClient 登录失败")
            self.assertTrue(cmd.connected)
            self.assertTrue(cmd.keepalive(), "CommandClient 心跳失败")

        ser = SerialClient(LOOPBACK, self.server.serial_port, timeout=2.0)
        with ser:
            ser.send_read_freq()
            resp = ser.read_civ_response(timeout=2.0)
        self.assertEqual(resp, CIV_READ_FREQ_RESP)

    def test_ci_v_transit_is_serial_packet(self):
        """读频率经 SerialClient 发出的包为 wire 头 + Serial 帧 + CI-V (跳过握手包)."""
        ser = SerialClient(LOOPBACK, self.server.serial_port)
        with ser:
            ser.send_read_freq()
        self.assertTrue(_wait(lambda: len(self.server.serial_packets) >= 1),
                        "服务器未收到 Serial 数据包")
        # open() 握手会先发 pkt3/pkt6, 数据包在后; 定位到 DATA 类型包再断言其组帧.
        wire = data = total_len = None
        for d, _ in self.server.serial_packets:
            w, tl = parse_wire_header(d)
            if w.type == UDP2_PKT_TYPE_DATA:
                wire, data, total_len = w, d, tl
                break
        self.assertIsNotNone(data, "服务器未收到 DATA 数据包 (仅收到握手包)")
        self.assertEqual(wire.type, UDP2_PKT_TYPE_DATA)
        self.assertEqual(total_len, len(data))
        frame = parse_serial_frame(data[WIRE_HEADER_SIZE:])
        self.assertTrue(frame.bulk)
        self.assertTrue(frame.payload.startswith(b"\xfe\xfe"))

    def test_serial_read_timeout_without_server(self):
        """无服务器应答时 SerialClient 抛 SerialTimeoutError (现于 open 握手阶段)."""
        silent = E2EFakeServer()
        silent.serial_sock.close()  # 关闭 serial 口, 不接收
        silent.serial_sock = _bind_udp()  # 重绑但不跑线程 → 不回包
        silent.serial_port = silent.serial_sock.getsockname()[1]
        silent.serial_sock.settimeout(3.0)
        try:
            # open() 会先做 pkt3/pkt4/pkt6 握手; 无服务器应答则在此抛出超时.
            with self.assertRaises(SerialTimeoutError):
                with SerialClient(LOOPBACK, silent.serial_port, timeout=0.3) as ser:
                    pass
        finally:
            silent.serial_sock.close()
            silent.ctrl_sock.close()


def _wait(pred, timeout: float = 2.0) -> bool:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ============================================================
# 真机 live E2E (环境变量门控, 默认跳过)
# ============================================================

_LIVE_HOST = os.environ.get("RSBA1_E2E_HOST", "").strip()


@unittest.skipUnless(_LIVE_HOST, "未设置 RSBA1_E2E_HOST, 跳过真机 E2E (IC-705 需在线)")
class LiveE2ETest(unittest.TestCase):
    """真机 E2E: 连真实 RS-BA1 服务器登录 + 读频率.

    需前置: IC-705 上电且 USB 串口已挂载; 服务器 RemoteUty 运行并监听控制端口;
    本机已建立可复用会话. 设 RSBA1_E2E_HOST=<服务器IP> 后运行.
    """

    def test_live_login_and_read_freq(self):
        """登录 + 读频率回程全链路 (真机)."""
        cmd = CommandClient(_LIVE_HOST, username="", password="")
        with cmd:
            self.assertTrue(cmd.connect(), "真机登录失败")
            self.assertTrue(cmd.connected)
            # 取登录后会话端口由服务器调度; Serial 信道默认 50002
            ser = SerialClient(_LIVE_HOST, timeout=3.0)
            with ser:
                ser.send_read_freq()
                resp = ser.read_civ_response(timeout=3.0)
            self.assertTrue(resp.startswith(b"\xfe\xfe"), f"非 CI-V 应答: {resp.hex()}")


if __name__ == "__main__":
    unittest.main(verbosity=2)