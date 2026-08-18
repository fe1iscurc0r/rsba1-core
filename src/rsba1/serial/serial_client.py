"""serial_client — Serial 信道 (UDP 50002) 客户端: 发送/接收/解析 CI-V 响应.

基于 kappanhang (nonoo, 真机验证) 与 serial_channel.md 的协议向服务器
(RemoteCom 端, 即运行 RemoteUty.exe 的机器) 的 UDP 50002 端口发送 CI-V 命令并读取应答。

关键协议要点 (详见 re/protocols/serial_channel.md §5.10+):
    - UDP 包 = UDP2 wire 头 (0x10) + Serial 帧 (5 + CI-V 数据)
    - wire 头: totalLen(LE) + type(LE) + seq(LE) + field_8(LE) + field_C(LE)
    - field_8 = 本地会话标识 localSID = (本地IP末两字节 << 16) | 本地端口
    - field_C = 对端会话标识 remoteSID = 握手 pkt4 应答回传的服务器 localSID
    - 会话握手: pkt3(0x03) → pkt4(0x04) → pkt6(0x06), 各发两遍 (kappanhang streamCommon.start)
    - seq 是 wire 头 LE uint16; sseq 是 Serial 层 BE uint16, 双工独立递增。
    - 服务器应答 payload 的 Serial 帧 [5..] 即 CI-V 响应帧。

用法:
    with SerialClient(host="192.168.1.10", ...) as c:
        c.send_read_freq()
        resp = c.read_civ_response(timeout=2.0)
        print(resp.hex())

参考:
    - re/protocols/serial_channel.md
    - src/rsba1/serial/serial_codec.py (编解码)
    - src/rsba1/ctypes_wrappers/civ_commands.py (CI-V 帧构造)
"""
from __future__ import annotations

import queue
import socket
import struct
import threading
import time
from typing import Optional, Tuple

from rsba1.serial.serial_codec import (
    WIRE_HEADER_SIZE,
    SERIAL_FRAME_HEADER_SIZE,
    UDP2_PKT_TYPE_DATA,
    UDP2_PKT_TYPE_RETRANSMIT,
    UDP2_PKT_TYPE_PKT3,
    UDP2_PKT_TYPE_PKT4,
    UDP2_PKT_TYPE_PKT6,
    UDP2_PKT_TYPE_KEEPALIVE,
    UDP2WireHeader,
    SerialFrame,
    build_wire_header,
    parse_wire_header,
    build_serial_frame,
    parse_serial_frame,
)
from rsba1.ctypes_wrappers import civ_commands as civcmd

__all__ = [
    "DEFAULT_SERIAL_PORT",
    "DEFAULT_SESSION_F8",
    "DEFAULT_SESSION_FC",
    "SerialClientError",
    "SerialTimeoutError",
    "SerialClient",
]

# 默认 Serial 信道端口 (线上确证)
DEFAULT_SERIAL_PORT = 50002

# 临时调试开关: 打印后台 reader 线程收到的每个包 (排查真机收包问题)
_DEBUG_READER = bool(int(__import__("os").getenv("RSBA1_DEBUG_READER", "0")))

# 历史默认会话 id 保留 (verify_serial_loop/e2e 等脚本显式传入时兼容).
# 新实现默认由 open() 依据本地 IP+端口自动计算 localSID (见 _compute_local_sid).
DEFAULT_SESSION_F8 = 0x2A94BC02
DEFAULT_SESSION_FC = 0x19F8B4F7


class SerialClientError(Exception):
    """Serial 信道客户端基础异常."""


class SerialTimeoutError(SerialClientError):
    """等待 CI-V 响应超时."""


class SerialClient:
    """Serial 信道 (UDP 50002) 客户端.

    封装 socket, 提供 CI-V 命令发送与响应接收. open() 时自动完成
    pkt3/pkt4/pkt6 会话握手并计算 localSID (field_8)。

    参数:
        host:     服务器 IP (运行 RemoteUty.exe 的机器)
        port:     UDP 端口 (默认 50002)
        field_8:  本地会话标识 localSID (默认 None → open() 自动计算)
        field_C:  对端会话标识 remoteSID (默认 None → 握手后由 pkt4 应答确立)
        to_addr:  目标电台 CI-V 地址 (默认 0xA4 = IC-705)
        from_addr:源控制器 CI-V 地址 (默认 0x00)
        timeout:  socket 默认超时 (秒), 用于读响应
        bind_port:可选, 绑定本地源端口 (线上确证: 服务器按源端口识别会话,
                  真实客户端源端口 = 50002, 见 serial_channel.md §5.4)。
                  为 None 时用系统随机临时端口。
        bind_ip:  绑定源 IP (仅 bind_port 非 None 时生效)。默认 127.0.0.1;
                  需经 LAN IP 访问时填本机局域网地址 (如 192.168.0.23)。
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_SERIAL_PORT,
        *,
        field_8: Optional[int] = None,
        field_C: Optional[int] = None,
        to_addr: int = civcmd.IC705_TO_ADDR,
        from_addr: int = civcmd.DEFAULT_FROM_ADDR,
        timeout: float = 2.0,
        bind_port: Optional[int] = None,
        bind_ip: str = "127.0.0.1",
    ):
        self.host = host
        self.port = port
        # None → open() 时自动计算 localSID / 握手确立 remoteSID
        self._explicit_f8 = field_8 is not None
        self._explicit_fc = field_C is not None
        self.field_8 = 0 if field_8 is None else field_8 & 0xFFFFFFFF
        self.field_C = 0 if field_C is None else field_C & 0xFFFFFFFF
        self.to_addr = to_addr & 0xFF
        self.from_addr = from_addr & 0xFF
        self.timeout = timeout
        self.bind_port = bind_port
        self.bind_ip = bind_ip
        self._sock: Optional[socket.socket] = None
        self._seq = 0          # UDP2 wire 头 seq (LE uint16, 发送侧递增)
        self._sseq = 0         # Serial 帧 sseq (BE uint16, 发送侧递增)
        self._sent_seqs: set = set()  # 已发送的 wire seq (绑定源端口时用于过滤回环包)
        self._tx_buf: dict = {}  # {wire seq: 完整已发数据包 bytes}, 用于响应服务器 RETRANSMIT 重传请求
        self._handshake_done = False
        # 后台单消费者读取线程: 回传服务器空探测包, 并把含数据的包置入队列
        self._resp_queue: "queue.Queue[Tuple[Optional[UDP2WireHeader], Optional[SerialFrame]]]" = queue.Queue()
        self._reader_stop = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._echo_seqs: set = set()  # 已回传的空探测 seq (回环模式下防死循环)

    # ============================================================
    # 连接管理
    # ============================================================

    def open(self) -> None:
        """创建并绑定 UDP socket, 计算 localSID, 完成 pkt3/4/6 会话握手. 幂等."""
        if self._sock is not None:
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 为稳定计算 localSID 需先在绑定后方可读本地端口; 未指定 bind_port 时绑随机端口。
        if self.bind_port is not None:
            # 服务器按源端口识别会话, 需绑定本机标准端口 (50002).
            # RemoteUty 已绑定 0.0.0.0:50002, 需 SO_REUSEADDR 才能共享绑定。
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.bind_ip, self.bind_port))
        else:
            self._sock.bind((self.bind_ip, 0))
        self._sock.settimeout(self.timeout)
        # localSID (field_8) 依据绑定后的本地 IP+端口自动计算 (kappanhang streamCommon.init)
        if not self._explicit_f8:
            self.field_8 = self._compute_local_sid()
        # pkt3 → pkt4 → pkt6 会话握手, 由 pkt4 应答确立 remoteSID (field_C)
        if not self._explicit_fc:
            self._handshake()
        # 握手完成后启动后台读取线程 (回传空探测包 / 收集 CI-V 应答)
        self._start_reader()

    def _compute_local_sid(self) -> int:
        """localSID = (本地IP末两字节 << 16) | 本地端口 (数值; 落 wire 头时按 LE pack).

        对应 kappanhang streamCommon.init:
            localSID = (IP末两字节 << 16) | port
        因 (x<<16) uint32 溢出仅保留低 16 位, 等价于 (IP & 0xFFFF) << 16 | port。
        IP 用 network-byte-order 读取 (inet_aton 即 BE), 与 wire 头字段端序无关。
        """
        laddr = self._sock.getsockname()
        ip_val = struct.unpack(">I", socket.inet_aton(laddr[0]))[0]
        return ((ip_val & 0xFFFF) << 16) | (laddr[1] & 0xFFFF)

    def _send_ctrl(self, type_: int, seq: int) -> None:
        """发送一个传输层控制包 (pkt3/4/6), 连发两遍 (kappanhang 冗余策略)."""
        wire = build_wire_header(
            type=type_, seq=seq,
            field_8=self.field_8, field_C=self.field_C, payload_len=0,
        )
        self._sock.sendto(wire, (self.host, self.port))
        self._sock.sendto(wire, (self.host, self.port))

    def _recv_ctrl(self, type_: int, timeout: float) -> bytes:
        """等待并返回指定 type 的控制包原始 bytes.

        异常:
            SerialTimeoutError - 超时未收到该 type 的包。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            data = self.recv_udp(remaining)
            try:
                wire, _ = parse_wire_header(data)
            except (ValueError, struct.error):
                continue
            if wire.type == type_:
                return data
        raise SerialTimeoutError(
            f"等待 pkt{type_} 应答超时 ({timeout} s)"
        )

    def _handshake(self) -> None:
        """pkt3 → pkt4 → pkt6 会话握手 (kappanhang streamCommon.start).

        pkt4 应答的 field_8 (bytes[8:12], LE) 即服务器 localSID, 作为本端 remoteSID。
        """
        self._send_ctrl(UDP2_PKT_TYPE_PKT3, 0)
        r = self._recv_ctrl(UDP2_PKT_TYPE_PKT4, self.timeout)
        self.field_C = struct.unpack("<I", r[8:12])[0] & 0xFFFFFFFF
        self._send_ctrl(UDP2_PKT_TYPE_PKT6, 1)
        self._recv_ctrl(UDP2_PKT_TYPE_PKT6, self.timeout)
        self._handshake_done = True

    def close(self) -> None:
        """关闭 socket, 停止后台读取线程. 幂等."""
        if self._reader_thread is not None:
            self._reader_stop.set()
            # 优先关 socket, 解除 reader 线程 recvfrom 阻塞
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                finally:
                    self._sock = None
            self._reader_thread.join(timeout=0.5)
            self._reader_thread = None
            self._reader_stop.clear()
        elif self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            finally:
                self._sock = None

    def __enter__(self) -> "SerialClient":
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    # ============================================================
    # 后台读取线程 (单消费者): 空探测回传 + CI-V 应答收集
    # ============================================================

    def _start_reader(self) -> None:
        """启动单消费者后台读取线程 (幂等)."""
        if self._reader_thread is not None and self._reader_thread.is_alive():
            return
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_run, daemon=True, name="rsba1-serial-reader"
        )
        self._reader_thread.start()

    def _stop_reader(self) -> None:
        """停止后台读取线程."""
        if self._reader_thread is not None:
            self._reader_stop.set()
            self._reader_thread.join(timeout=0.5)
            self._reader_thread = None

    def _reader_run(self) -> None:
        """后台读取主循环: 逐包处理.

        协议要点 (serial_channel.md §5.4): 服务器会向客户端源端口主动发
        空 payload 探测包, 客户端**必须把 field_8/field_C 对调回传**,
        服务器才会推送 CI-V 应答 ("客户端不能只发不回")。
        """
        while not self._reader_stop.is_set():
            try:
                data, addr = self._sock.recvfrom(0x1000)
            except socket.timeout:
                continue
            except OSError:  # socket 已关闭
                break
            if addr[:2] != (self.host, self.port):
                continue
            wire, frame = self._try_parse(data)
            if _DEBUG_READER:
                if wire is None:
                    print(f"  [reader] {len(data)}B NO-PARSE: {data.hex()}")
                else:
                    print(f"  [reader] {len(data)}B sq={wire.seq} type={wire.type} "
                          f"pay={(frame.payload.hex() if frame and frame.payload else '')}")
                    if wire.type == UDP2_PKT_TYPE_RETRANSMIT:
                        print(f"           RETRANSMIT req:{data[WIRE_HEADER_SIZE:].hex()}")
            if wire is None:
                # 控制包 (如 RETRANSMIT) 载荷不是合法 Serial 帧, 帧层解析会失败;
                # 但 wire 头仍有效, 尝试仅解 wire 头以识别控制包类型。
                try:
                    w, _ = parse_wire_header(data)
                except (ValueError, struct.error):
                    continue
                if w.type == UDP2_PKT_TYPE_RETRANSMIT:
                    self._handle_retransmit(data)
                continue
            # 本地回环: 丢弃自己发出的数据包 / 已回传的空探测包
            if wire.type == UDP2_PKT_TYPE_DATA and wire.seq in self._sent_seqs:
                continue
            if wire.seq in self._echo_seqs:
                continue
            if wire.type == UDP2_PKT_TYPE_KEEPALIVE:
                continue
            if wire.type == UDP2_PKT_TYPE_RETRANSMIT:
                # 服务器 pkt1 重传请求 → 重发缓存的对应数据包
                self._handle_retransmit(data)
                continue
            if frame is not None and frame.payload:
                # 服务器推送的 CI-V 应答 → 置入队列供 read_civ_response 取回
                self._resp_queue.put((wire, frame))
            elif wire.type == UDP2_PKT_TYPE_DATA:
                # 服务器空探测包 → 对调 field_8/field_C 原样回传
                self._echo_probe(wire)

    def _echo_probe(self, wire: UDP2WireHeader) -> None:
        """把服务器空探测包按对调 field_8/field_C 回传 (客户端存活确认)."""
        echo = build_wire_header(
            type=UDP2_PKT_TYPE_DATA,
            seq=(wire.seq + 1) & 0xFFFF,
            field_8=wire.field_C & 0xFFFFFFFF,
            field_C=wire.field_8 & 0xFFFFFFFF,
            payload_len=0,
        )
        self._echo_seqs.add(wire.seq)
        try:
            self._sock.sendto(echo, (self.host, self.port))
        except OSError:
            pass

    def _handle_retransmit(self, data: bytes) -> None:
        """响应服务器 pkt1 重传请求: 重发本端已缓存的数据包.

        协议 (kappanhang pkt1.go): RETRANSMIT 包载荷为 1+ 个 LE uint16 序号,
        指示请求重传这些 wire seq 的数据包。仅重发本端确实发出并缓存过的包;
        旧会话/未知 seq 直接忽略。
        """
        if not self._tx_buf:
            return
        payload = data[WIRE_HEADER_SIZE:]
        n = len(payload) // 2
        for i in range(n):
            seq = struct.unpack("<H", payload[i * 2:i * 2 + 2])[0]
            if seq not in self._tx_buf:
                continue
            try:
                self._sock.sendto(self._tx_buf[seq], (self.host, self.port))
            except OSError:
                pass

    # ============================================================
    # 内部: 包构造与序号
    # ============================================================

    def _next_seq(self) -> int:
        """UDP2 wire 头 seq (LE uint16 递增)."""
        s = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        return s

    def _next_sseq(self) -> int:
        """Serial 帧 sseq (BE uint16 递增)."""
        s = self._sseq
        self._sseq = (self._sseq + 1) & 0xFFFF
        return s

    def _build_civ_packet(self, civ_frame: bytes) -> bytes:
        """构造数据包 (wire 头 + Serial 帧), 内含 CI-V 帧."""
        payload = civ_frame
        frame = build_serial_frame(
            payload, sseq=self._next_sseq(), bulk=True,
        )
        seq = self._next_seq()
        self._sent_seqs.add(seq)
        wire = build_wire_header(
            type=UDP2_PKT_TYPE_DATA, seq=seq,
            field_8=self.field_8, field_C=self.field_C,
            payload_len=len(frame),
        )
        pkt = wire + frame
        self._tx_buf[seq] = pkt  # 缓存完整包, 供服务器 RETRANSMIT 时重发
        return pkt

    # ============================================================
    # 发送
    # ============================================================

    def send_civ(self, civ_frame: bytes) -> int:
        """发送原始 CI-V 帧 (透传), 返回发送字节数.

        会话建立已在 open() 的 pkt3/4/6 握手中完成, 无需再补发注册包
        (旧 send_registration 型 type=0 注册包已被握手取代, 见 serial_channel.md §5.12)。

        参数:
            civ_frame: 完整 CI-V 帧 bytes (含 FE 前导 / FD 尾).
        """
        if self._sock is None:
            raise SerialClientError("SerialClient 未 open")
        if not isinstance(civ_frame, (bytes, bytearray)):
            raise TypeError(f"civ_frame 必须是 bytes, 实际 {type(civ_frame).__name__}")
        pkt = self._build_civ_packet(bytes(civ_frame))
        return self._sock.sendto(pkt, (self.host, self.port))

    def send_civ_body(self, body: bytes) -> int:
        """发送 CI-V 命令体 [to, from, cmd...] (自动加 FE/FD 定界).

        参数:
            body: [to, from, cmd...] 命令体 (civSend 约定, 不含 FE/FD).
        """
        frame = civcmd.build_frame(self.to_addr, self.from_addr, body)
        return self.send_civ(frame)

    def send_read_freq(self) -> int:
        """读频率 (cmd=0x03)."""
        return self.send_civ_body(civcmd.read_freq_bytes())

    def send_read_mode(self) -> int:
        """读模式 (cmd=0x04)."""
        return self.send_civ_body(bytes([civcmd.CMD_READ_MODE]))

    def send_set_freq(self, hz: int) -> int:
        """设频率 (cmd=0x06 + BCD)."""
        return self.send_civ_body(civcmd.set_freq_bytes(hz))

    def send_ptt_on(self) -> int:
        """PTT ON (TX)."""
        return self.send_civ_body(civcmd.ptt_on_bytes())

    def send_ptt_off(self) -> int:
        """PTT OFF (RX)."""
        return self.send_civ_body(civcmd.ptt_off_bytes())

    def send_read_smeter(self) -> int:
        """读 S-meter (cmd=0x1A 0x03)."""
        return self.send_civ_body(bytes([civcmd.CMD_READ_SMETER, 0x03]))

    # ============================================================
    # 接收 & 解析
    # ============================================================

    def recv_udp(self, timeout: Optional[float] = None) -> bytes:
        """接收一个 UDP 数据报 (原始 bytes).

        参数:
            timeout: 覆盖 socket 默认超时 (秒); None 用 self.timeout.

        返回:
            UDP 数据报 bytes.

        异常:
            SerialTimeoutError - 超时无数据.
        """
        if self._sock is None:
            raise SerialClientError("SerialClient 未 open")
        old = None
        if timeout is not None:
            old = self._sock.gettimeout()
            self._sock.settimeout(timeout)
        try:
            try:
                data, _ = self._sock.recvfrom(0x1000)
                return data
            except socket.timeout:
                raise SerialTimeoutError(
                    f"接收 UDP 数据超时 ({timeout if timeout is not None else self.timeout} s)"
                )
        finally:
            if old is not None:
                self._sock.settimeout(old)

    def read_civ_response(
        self, timeout: Optional[float] = None, max_packets: int = 16
    ) -> bytes:
        """读取并返回一条 CI-V 响应帧 (从后台读取线程的队列取).

        后台 _reader_run 已负责: 跳过 keepalive、回传服务器空探测包、过滤本地
        回环包, 并把服务器推送的含数据包置入 self._resp_queue。本方法只需等待。
        存活回传详见 serial_channel.md §5.4 ("客户端不能只发不回")。

        参数:
            timeout:    覆盖 socket 默认超时 (秒)
            max_packets:最多尝试解析的包数, 防死循环.

        返回:
            完整 CI-V 响应帧 bytes (含 FE 前导 / FD 尾).

        异常:
            SerialTimeoutError - 超时仍未收到 CI-V 响应.
        """
        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        seen = 0
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                _wire, frame = self._resp_queue.get(timeout=remaining)
            except queue.Empty:  # 超时无数据
                break
            if frame is not None and frame.payload:
                return frame.payload
            seen += 1
            if seen >= max_packets:
                break
        raise SerialTimeoutError(
            f"读取 CI-V 响应超时 ({max_packets} 包内未含有效数据)"
        )

    @staticmethod
    def _try_parse(data: bytes) -> Tuple[Optional[UDP2WireHeader], Optional[SerialFrame]]:
        """安全解析一个 UDP 包, 失败返回 (None, None)."""
        try:
            wire, total_len = parse_wire_header(data)
            if total_len > len(data) or total_len < WIRE_HEADER_SIZE + SERIAL_FRAME_HEADER_SIZE:
                return wire, None
            frame = parse_serial_frame(data[WIRE_HEADER_SIZE:])
            return wire, frame
        except (ValueError, struct.error):
            return None, None

    def __repr__(self) -> str:
        return (
            f"<SerialClient {self.host}:{self.port} "
            f"f8=0x{self.field_8:08X} fc=0x{self.field_C:08X} "
            f"open={self._sock is not None}>"
        )