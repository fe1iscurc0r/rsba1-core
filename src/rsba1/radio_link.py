"""radio_link — IC-705 RS-BA1 V2 全链路高层会话库 (2026-08-18 真机验证).

封装今晚定案的完整链路 (详见 re/protocols/command_channel_cmd.md §4.2):

    Command(50001): pkt3/4/6 → ConnectServer(login) → auth(0x02/0x05)
        → 0xA8 包 → ConnectTrans → 电台下发新 SID
    Serial(50002): 独立 pkt3/4/6 → open(magic=0x05) → CI-V 透传

关键工程约束 (全部 2026-08-18 真机踩坑得来):
    - 凭证必须 passcode() 编码, 绝对偏移 0x40/0x50/0x60 (错 0x10 → result=-2)
    - Serial 阶段控制信道必须持续维活 (应答 pkt7), 否则电台整会话拆除
    - 电台重传极持久: 收包必须 pending 缓存不乱丢; ConnectTrans 前 drain 清场
    - 退出必须 deauth(magic=0x01) + disconnect(type=0x05), 否则电台网络栈卡死
    - CI-V from 必须 0x00; 频率设置限业余段白名单 (civcmd.assert_allowed_freq)

线程模型:
    每个信道一个读者线程独占 socket.recvfrom —— pkt7 即时应答, idle 丢弃,
    其余进 inbox 队列; 业务线程只从 inbox 消费 (带 stash 回推, 等包不乱序)。

用法:
    with RadioLink("192.168.0.31", "linnan", "shenyaodiyi") as link:
        print(link.read_freq() / 1e6, "MHz")
        link.set_freq(145_000_000)   # 白名单内
        link.set_freq(orig_hz)       # 恢复

参考:
    - kappanhang controlstream/serialstream/streamcommon/pkt0/pkt7/passcode
    - src/rsba1/serial/command_client.py (包构造/解析)
    - src/rsba1/ctypes_wrappers/civ_commands.py (CI-V 帧)
"""
from __future__ import annotations

import collections
import queue
import socket
import struct
import threading
import time
from typing import Optional, Tuple

from rsba1.serial import command_client as cc
from rsba1.serial.serial_codec import build_serial_frame
from rsba1.ctypes_wrappers import civ_commands as civcmd

__all__ = [
    "RadioLinkError",
    "RadioAuthError",
    "RadioTimeoutError",
    "RadioLink",
]

CIV_IC705 = 0xA4   # IC-705 CI-V 地址
CIV_FROM = 0x00    # 控制器地址 (必须 0x00; 0xE0 电台沉默)


class RadioLinkError(Exception):
    """RadioLink 基础异常."""


class RadioAuthError(RadioLinkError):
    """认证/授权失败 (凭证错误 / ConnectTrans 被拒)."""


class RadioTimeoutError(RadioLinkError):
    """等待电台响应超时."""


def _log(chan: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {chan}: {msg}", flush=True)


# ============================================================
# 单信道 (读者线程 + inbox 队列)
# ============================================================

class _Chan:
    """一条 UDP 信道 (control 或 serial) 的收发与维活."""

    def __init__(self, host: str, port: int, bind_ip: str, name: str,
                 verbose: bool = False):
        self.host = host
        self.port = port
        self.name = name
        self.verbose = verbose
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((bind_ip, port))
        except OSError:
            self.sock.bind((bind_ip, 0))
        self.sock.settimeout(0.2)
        local = self.sock.getsockname()
        self.local_sid = cc.make_local_sid(local[0], local[1])
        self.remote_sid = 0
        self.tx_seq = 1                     # tracked 数据包序号 (从 1 起)
        self.inbox: "queue.Queue[bytes]" = queue.Queue()
        self.stash: "collections.deque[bytes]" = collections.deque()
        self._stop = threading.Event()
        self._reader: Optional[threading.Thread] = None
        if verbose:
            _log(name, f"本地 {local[0]}:{local[1]} localSID=0x{self.local_sid:08X}")

    # ---------------- 发送 ----------------

    def send(self, pkt: bytes, twice: bool = False) -> None:
        self.sock.sendto(pkt, (self.host, self.port))
        if twice:
            self.sock.sendto(pkt, (self.host, self.port))

    def send_data(self, pkt: bytes) -> None:
        """tracked 数据包: 覆写外层 seq 后发出."""
        pkt = bytearray(pkt)
        struct.pack_into("<H", pkt, 6, self.tx_seq & 0xFFFF)
        self.tx_seq = (self.tx_seq + 1) & 0xFFFF
        self.send(bytes(pkt))

    # ---------------- 接收 ----------------

    def start_reader(self) -> None:
        """启动读者线程 (幂等). pkt7 即时应答, idle 丢弃, 其余进 inbox."""
        if self._reader is not None:
            return

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    data, _ = self.sock.recvfrom(0x2000)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if cc.is_pkt7(data):
                    if data[16] == 0x00:      # 电台 keepalive 请求 → 应答
                        self.send(cc.build_pkt7(
                            self.local_sid, self.remote_sid,
                            struct.unpack("<H", data[6:8])[0],
                            reply_id=data[17:21]))
                    continue
                if cc.is_idle_pkt0(data):
                    continue
                self.inbox.put(data)

        self._reader = threading.Thread(
            target=_loop, name=f"{self.name}-reader", daemon=True)
        self._reader.start()

    def _take(self, timeout: float) -> Optional[bytes]:
        """从 stash/inbox 取一个报文."""
        if self.stash:
            return self.stash.popleft()
        try:
            return self.inbox.get(timeout=max(0.01, timeout))
        except queue.Empty:
            return None

    def wait_for(self, pred, timeout: float, label: str = "") -> Optional[bytes]:
        """等待满足 pred 的报文; 不匹配的**保留**供后续阶段消费."""
        deadline = time.time() + timeout
        held = []
        try:
            while True:
                data = self._take(max(0.01, deadline - time.time()))
                if data is None:
                    return None
                if pred(data):
                    return data
                if self.verbose:
                    _log(self.name, f"(暂存 {len(data)}B {data[:8].hex()}... {label})")
                held.append(data)
        finally:
            self.stash.extendleft(reversed(held))

    def drain(self, label: str = "") -> int:
        """清空 stash+inbox (丢弃过期重传包), 返回丢弃数."""
        n = len(self.stash)
        self.stash.clear()
        while True:
            try:
                self.inbox.get_nowait()
                n += 1
            except queue.Empty:
                break
        if n and self.verbose:
            _log(self.name, f"(清空 {n} 个过期包 {label})")
        return n

    # ---------------- 握手 ----------------

    def handshake(self, timeout: float = 3.0) -> bool:
        """pkt3×2 → 等 pkt4 → pkt6×2 → 等 pkt6 应答 (读者线程先行启动维活)."""
        self.start_reader()
        self.send(cc.build_pkt3(self.local_sid), twice=True)
        data = self.wait_for(
            lambda d: len(d) == 16 and d[4:6] == b"\x04\x00", timeout, "pkt4")
        if data is None:
            if self.verbose:
                _log(self.name, "!! 未收到 pkt4")
            return False
        self.remote_sid = struct.unpack(">I", data[8:12])[0]
        if self.verbose:
            _log(self.name, f"pkt4 ← remoteSID=0x{self.remote_sid:08X}")
        self.send(cc.build_pkt6(self.local_sid, self.remote_sid), twice=True)
        self.wait_for(lambda d: len(d) == 16 and d[4:6] == b"\x06\x00",
                      2.0, "pkt6应答")
        return True

    # ---------------- 关闭 ----------------

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None


# ============================================================
# RadioLink
# ============================================================

class RadioLink:
    """IC-705 RS-BA1 全链路会话 (Command 认证 + Serial CI-V 透传).

    参数:
        host:     电台 IP (RS-BA1 Server Function)
        username: 电台侧 RS-BA1 用户名 (菜单 WLAN Set → Remote Settings)
        password: 电台侧 RS-BA1 密码
        bind_ip:  本机 LAN IP (localSID 由它派生)
        verbose:  打印各阶段日志
    """

    def __init__(self, host: str, username: str, password: str, *,
                 bind_ip: str = "", verbose: bool = True):
        self.host = host
        self.username = username
        self.password = password
        self.bind_ip = bind_ip
        self.verbose = verbose
        self._reset_chans()
        self._opened = False

    def _reset_chans(self) -> None:
        """(重)建双信道与全部会话状态 (open 重试时复用)."""
        self.ctrl = _Chan(self.host, 50001, self.bind_ip, "control", self.verbose)
        self.ser = _Chan(self.host, 50002, self.bind_ip, "serial", self.verbose)
        self.auth_id: Optional[bytes] = None
        self._sseq = 0                       # Serial 层 BE 序号
        self._serial_opened = False          # serial open(magic=0x05) 是否已发
        self._reauth_stop = threading.Event()
        self._reauth_thread: Optional[threading.Thread] = None

    # ---------------- 生命周期 ----------------

    def open(self, retries: int = 3) -> None:
        """建立完整会话: 控制登录 → ConnectTrans → Serial 握手+open.

        电台 teardown 是异步的, 偶发 ConnectTrans 被拒/CI-V 首包丢失属正常
        (kappanhang main 同样带重试); 失败时整体关闭后间隔重试。
        """
        if self._opened:
            return
        last_err: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                if not self.ctrl.handshake():
                    raise RadioTimeoutError("control 信道握手失败 (无 pkt4)")
                self._login()
                if not self.ser.handshake():
                    raise RadioTimeoutError("serial 信道握手失败 (无 pkt4)")
                self._serial_open()
                time.sleep(0.3)   # open 后稍候, 电台透传闸门就绪
                self._start_reauth()
                self._opened = True
                return
            except (RadioLinkError, OSError) as e:
                last_err = e
                if self.verbose:
                    _log("link", f"第 {attempt}/{retries} 次建链失败: {e}")
                self.close()
                if attempt < retries:
                    time.sleep(1.5)
                    self._reset_chans()   # socket 已关, 重建信道再试
        raise last_err if last_err else RadioLinkError("建链失败")

    def close(self) -> None:
        """优雅退出: serial close → deauth → disconnect → 关 socket.

        四件套缺一不可 (kappanhang deinit 复刻):
          ① serial close 帧 (magic=0x00) — 漏发会让电台认为 serial/audio 流仍被
             占用, 下一次 ConnectTrans 直接被拒 (80B ff ff ff, 需重启电台);
          ② control deauth (auth magic=0x01);
          ③ 两信道传输层 disconnect (type=0x05);
          ④ 关 socket。
        """
        self._reauth_stop.set()
        if self._reauth_thread is not None:
            self._reauth_thread.join(timeout=1.5)
            self._reauth_thread = None
        if self._serial_opened:      # ① serial close (magic=0x00)
            try:
                frame = build_serial_frame(b"\x00", sseq=self._next_sseq(), bulk=False)
                pkt = cc.build_transport_header(
                    0x10 + len(frame), 0x00, self.ser.tx_seq,
                    self.ser.local_sid, self.ser.remote_sid) + frame
                self.ser.send_data(pkt)
                if self.verbose:
                    _log("serial", "→ close magic=0x00")
                time.sleep(0.3)
            except OSError:
                pass
            self._serial_opened = False
        if self.auth_id is not None:  # ② control deauth
            try:
                pkt = cc.build_auth_request(
                    0x01, local_sid=self.ctrl.local_sid,
                    remote_sid=self.ctrl.remote_sid,
                    outer_seq=self.ctrl.tx_seq, inner_seq=0x10,
                    auth_id=self.auth_id)
                self.ctrl.send_data(pkt)
                if self.verbose:
                    _log("control", "→ deauth (magic=0x01)")
                time.sleep(0.5)
            except OSError:
                pass
            self.auth_id = None
        for chan in (self.ser, self.ctrl):  # ③④ disconnect + close
            try:
                if chan.remote_sid:
                    chan.send(cc.build_disconnect_pkt(
                        chan.local_sid, chan.remote_sid), twice=True)
            except OSError:
                pass
            chan.close()
        self._opened = False

    def __enter__(self) -> "RadioLink":
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    # ---------------- 控制信道登录流程 ----------------

    def _login(self) -> None:
        """ConnectServer → auth×2 → 0xA8 → ConnectTrans (kappanhang controlstream.init)."""
        # -- ConnectServer --
        login = cc.build_login_request(
            self.username, self.password,
            local_sid=self.ctrl.local_sid, remote_sid=self.ctrl.remote_sid,
            outer_seq=self.ctrl.tx_seq, inner_seq=0, auth_start_id=None)
        if self.verbose:
            _log("control", f"→ ConnectServer 登录 ({len(login)}B)")
        self.ctrl.send_data(login)
        data = self.ctrl.wait_for(
            lambda d: len(d) == cc.LOGIN_RESPONSE_LEN and d[:1] == b"\x60",
            3.0, "等登录应答")
        if data is None:
            raise RadioTimeoutError("未收到 0x60 登录应答")
        ok, auth_id, result = cc.parse_login_response(data)
        if not ok:
            raise RadioAuthError(
                f"ConnectServer 认证失败 result={result} "
                f"(0xFFFFFFFE=用户名/密码错误, 注意凭证是相对电台侧 RS-BA1 用户列表)")
        self.auth_id = auth_id
        if self.verbose:
            _log("control", f"✓ 认证通过 authID={auth_id.hex()}")

        # -- 认证巩固 auth(0x02) / auth(0x05) --
        for i, magic in enumerate((0x02, 0x05)):
            pkt = cc.build_auth_request(
                magic, local_sid=self.ctrl.local_sid,
                remote_sid=self.ctrl.remote_sid,
                outer_seq=self.ctrl.tx_seq, inner_seq=1 + i, auth_id=auth_id)
            self.ctrl.send_data(pkt)
            data = self.ctrl.wait_for(
                lambda d: cc.parse_auth_reply_magic(d) is not None,
                2.0, "等auth应答")
            if data is not None and self.verbose:
                _log("control",
                     f"← auth 应答 magic=0x{cc.parse_auth_reply_magic(data):02X}")

        # -- 等 0xA8 取 a8replyID --
        data = self.ctrl.wait_for(cc.is_a8_packet, 5.0, "等0xA8")
        if data is None:
            raise RadioTimeoutError("未等到电台 0xA8 包 (a8replyID)")
        a8_id = cc.extract_a8_reply_id(data)
        if self.verbose:
            _log("control", f"← 0xA8 包, a8replyID={a8_id.hex()}")

        # -- ConnectTrans 申请 Serial/Audio (发前清场防过期重传串话) --
        ct = cc.build_connect_trans_request(
            self.username,
            local_sid=self.ctrl.local_sid, remote_sid=self.ctrl.remote_sid,
            outer_seq=self.ctrl.tx_seq, inner_seq=3,
            auth_id=auth_id, a8_reply_id=a8_id)
        if self.verbose:
            _log("control", f"→ ConnectTrans ({len(ct)}B)")
        self.ctrl.drain("ConnectTrans 前清场")
        self.ctrl.send_data(ct)
        data = self.ctrl.wait_for(
            lambda d: (len(d) == 80 and d[48:51] == b"\xff\xff\xff")
            or (len(d) == cc.CONNECT_TRANS_RESPONSE_LEN and d[:1] == b"\x90"),
            3.0, "等ConnectTrans应答")
        if data is None:
            raise RadioTimeoutError("未收到 ConnectTrans 应答")
        if len(data) == 80:
            raise RadioAuthError(
                "ConnectTrans 被拒 (80B ff ff ff) — 会话被挤占, 需重启电台")
        ok, new_remote, new_local, new_auth, dev = cc.parse_connect_trans_response(data)
        if not ok:
            raise RadioAuthError(f"ConnectTrans 应答 [96]!=1 (设备 {dev!r})")
        # 电台在 ConnectTrans 后切换控制会话 SID
        self.ctrl.local_sid, self.ctrl.remote_sid = new_local, new_remote
        self.auth_id = new_auth
        if self.verbose:
            _log("control", f"✓ ConnectTrans 通过 (设备 {dev!r})")

    # ---------------- Serial 信道 ----------------

    def _serial_open(self) -> None:
        """Serial open 包 (magic=0x05) — 透传闸门."""
        frame = build_serial_frame(b"\x05", sseq=self._next_sseq(), bulk=False)
        pkt = cc.build_transport_header(
            0x10 + len(frame), 0x00, self.ser.tx_seq,
            self.ser.local_sid, self.ser.remote_sid) + frame
        if self.verbose:
            _log("serial", f"→ open magic=0x05 ({len(pkt)}B)")
        self.ser.send_data(pkt)
        self.ser.drain("open 后清场")
        self._serial_opened = True

    def _next_sseq(self) -> int:
        s = self._sseq & 0xFFFF
        self._sseq = (self._sseq + 1) & 0xFFFF
        return s

    def _start_reauth(self) -> None:
        """周期 auth(0x05) (30s, 电台业务心跳 90s 超时的一半, 保长会话)."""
        def _loop() -> None:
            while not self._reauth_stop.wait(30.0):
                if self.auth_id is None:
                    continue
                try:
                    pkt = cc.build_auth_request(
                        0x05, local_sid=self.ctrl.local_sid,
                        remote_sid=self.ctrl.remote_sid,
                        outer_seq=self.ctrl.tx_seq, inner_seq=0x20,
                        auth_id=self.auth_id)
                    self.ctrl.send_data(pkt)
                except OSError:
                    return
        self._reauth_thread = threading.Thread(
            target=_loop, name="control-reauth", daemon=True)
        self._reauth_thread.start()

    def send_civ(self, civ: bytes) -> None:
        """发送一帧 CI-V (Serial 帧封装 + 传输头, tracked)."""
        frame = build_serial_frame(civ, sseq=self._next_sseq(), bulk=True)
        pkt = cc.build_transport_header(
            0x10 + len(frame), 0x00, self.ser.tx_seq,
            self.ser.local_sid, self.ser.remote_sid) + frame
        self.ser.send_data(pkt)

    def read_civ(self, cmd: int, timeout: float = 2.0) -> bytes:
        """等待来自电台 (from=0xA4) 且 cmd 字节匹配的 CI-V 帧.

        自动跳过: 本端请求回环 (to=0xA4) / 其他 cmd 的异步帧 (保留在 stash)。
        """
        def _match(d: bytes) -> bool:
            if len(d) < 22 or d[16] != 0xC1 or d[0] - 0x15 != d[17]:
                return False
            p = d[21:]
            return (len(p) >= 6 and p[0] == 0xFE and p[1] == 0xFE
                    and p[2] == CIV_FROM and p[3] == CIV_IC705 and p[4] == cmd)
        data = self.ser.wait_for(_match, timeout, f"等CI-V cmd=0x{cmd:02X}")
        if data is None:
            raise RadioTimeoutError(f"等 CI-V 应答超时 (cmd=0x{cmd:02X})")
        return data[21:]

    # ---------------- 高层 CI-V 业务 ----------------

    def _civ_query(self, cmd_bytes: bytes, resp_cmd: int, timeout: float) -> bytes:
        """发一帧查询并等应答; 超时重发一次 (电台偶发丢首包, kappanhang 靠重传
        请求兜底, 此处简化为应用层重发一次)."""
        civ = civcmd.build_frame(CIV_IC705, CIV_FROM, cmd_bytes)
        for attempt in range(2):
            self.send_civ(civ)
            try:
                return self.read_civ(resp_cmd, timeout)
            except RadioTimeoutError:
                if attempt == 1:
                    raise
                if self.verbose:
                    _log("serial", f"(cmd=0x{resp_cmd:02X} 超时, 重发一次)")
        raise RadioTimeoutError(f"cmd=0x{resp_cmd:02X} 无应答")

    def read_freq(self, timeout: float = 2.0) -> int:
        """读当前 VFO 频率, 返回 Hz."""
        resp = self._civ_query(civcmd.read_freq_bytes(), 0x03, timeout)
        return civcmd.bytes_to_freq(resp[5:10])

    def read_mode(self, timeout: float = 2.0) -> Tuple[int, int]:
        """读当前模式, 返回 (mode, filter) 原始枚举字节."""
        resp = self._civ_query(bytes([0x04]), 0x04, timeout)
        if len(resp) < 7:
            raise RadioLinkError(f"read_mode 应答过短: {resp.hex()}")
        return resp[5], resp[6]

    def set_freq(self, hz: int) -> None:
        """设置 VFO 频率 (业余段白名单强制; 写命令电台无 ACK, 用 read_freq 复核)."""
        civcmd.assert_allowed_freq(hz)
        self.send_civ(civcmd.build_frame(CIV_IC705, CIV_FROM, civcmd.set_freq_bytes(hz)))

    def read_smeter(self, timeout: float = 2.0) -> int:
        """读取 S-meter 原始数据字节.

        返回: S-meter 原始值 (0-255)。参考 S 表换算为 dB/档位。

        超时: 电台静默时抛出 RadioTimeoutError。
        """
        resp = self._civ_query(bytes([civcmd.CMD_READ_SMETER, 0x03]), 0x1A, timeout)
        # parse_smeter expects the CI-V frame from index 0; our resp starts at frame data
        # parse_civ_response reads from blob[0] expecting FE FE ...
        # Our resp from _civ_query is already stripped to frame body (after strip_civ_frame)
        # But _civ_query returns the raw response blob starting at FE FE,
        # so we need to pass the full frame and let parse_smeter handle it.
        # Actually: read_civ returns data[21:] which is CI-V frame body only (after wire/serial hdr).
        # parse_smeter expects the full CI-V frame. Let's reconstruct.
        from rsba1.mailslot.civ_response import parse_smeter
        # CI-V frame: FE FE <from> <to> <cmd> <sub> <data> FD
        frame = b'\xfe\xfe' + bytes([CIV_FROM, CIV_IC705]) + resp
        return parse_smeter(frame)

    def ptt(self, on: bool) -> None:
        """PTT 控制 (⚠️ on=True 会真正发射, 调用方须确保天线/负载安全)."""
        cmd = civcmd.ptt_on_bytes() if on else civcmd.ptt_off_bytes()
        self.send_civ(civcmd.build_frame(CIV_IC705, CIV_FROM, cmd))

    def __repr__(self) -> str:
        return (f"<RadioLink {self.host} user={self.username!r} "
                f"opened={self._opened} authID="
                f"{self.auth_id.hex() if self.auth_id else '-'}>")
