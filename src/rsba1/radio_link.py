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
        audio:    True=会话期间同时开启并维活 Audio(50003) 信道, 退出时正确拆除
                  (kappanhang 完整模式 —— ConnectTrans 一次性分配 serial+audio,
                  音频流被分配却从不建立/拆除会把电台 MOD 输入挂死在 WLAN 网络流上,
                  造成"能 PTT 但声音进不去, 初始化电台才恢复");
                  False=ConnectTrans 音频端口填 0, 不申请音频流 (实验性,
                  电台固件可能拒绝, 视真机表现选用)。
        verbose:  打印各阶段日志
    """

    def __init__(self, host: str, username: str, password: str, *,
                 bind_ip: str = "", audio: bool = True, verbose: bool = True):
        self.host = host
        self.username = username
        self.password = password
        self.bind_ip = bind_ip
        self.audio = audio
        self.verbose = verbose
        self._reset_chans()
        self._opened = False

    def _reset_chans(self) -> None:
        """(重)建信道与全部会话状态 (open 重试时复用)."""
        self.ctrl = _Chan(self.host, 50001, self.bind_ip, "control", self.verbose)
        self.ser = _Chan(self.host, 50002, self.bind_ip, "serial", self.verbose)
        self.aud: Optional[_Chan] = (
            _Chan(self.host, 50003, self.bind_ip, "audio", self.verbose)
            if self.audio else None)
        self.auth_id: Optional[bytes] = None
        self._sseq = 0                       # Serial 层 BE 序号
        self._serial_opened = False          # serial open(magic=0x05) 是否已发
        self._audio_opened = False           # audio 握手是否已完成
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
                if self.aud is not None:      # 音频信道: 握手 + pkt7 维活
                    if not self.aud.handshake():
                        raise RadioTimeoutError("audio 信道握手失败 (无 pkt4)")
                    self._audio_opened = True
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
        """优雅退出: serial close → audio disconnect → deauth → disconnect → 关 socket.

        五件套缺一不可 (kappanhang deinit 复刻):
          ① serial close 帧 (magic=0x00) — 漏发会让电台认为 serial/audio 流仍被
             占用, 下一次 ConnectTrans 直接被拒 (80B ff ff ff, 需重启电台);
          ② audio 信道 disconnect (开启过才发) — 漏拆会把电台 MOD 输入挂死在
             WLAN 网络流上 (能 PTT 但声音进不去);
          ③ control deauth (auth magic=0x01);
          ④ 各信道传输层 disconnect (type=0x05);
          ⑤ 关 socket。
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
        chans = [self.ser, self.ctrl]
        if self.aud is not None:
            chans.insert(0, self.aud)   # ② audio 先拆 (⑤ 一并关 socket)
        if self.auth_id is not None:  # ③ control deauth
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
        for chan in chans:  # ④⑤ disconnect + close
            try:
                if chan.remote_sid:
                    chan.send(cc.build_disconnect_pkt(
                        chan.local_sid, chan.remote_sid), twice=True)
            except OSError:
                pass
            chan.close()
        self._audio_opened = False
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
            auth_id=auth_id, a8_reply_id=a8_id,
            audio_port=50003 if self.audio else 0)
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

    def read_civ(self, cmd: int, timeout: float = 2.0, sub: bytes = b"") -> bytes:
        """等待来自电台 (from=0xA4) 且 cmd (+可选 sub 前缀) 匹配的 CI-V 帧.

        自动跳过: 本端请求回环 (to=0xA4) / 其他 cmd 的异步帧 (保留在 stash)。
        """
        def _match(d: bytes) -> bool:
            if len(d) < 22 or d[16] != 0xC1 or d[0] - 0x15 != d[17]:
                return False
            p = d[21:]
            if not (len(p) >= 6 and p[0] == 0xFE and p[1] == 0xFE
                    and p[2] == CIV_FROM and p[3] == CIV_IC705 and p[4] == cmd):
                return False
            return p[5:5 + len(sub)] == sub
        data = self.ser.wait_for(_match, timeout, f"等CI-V cmd=0x{cmd:02X}")
        if data is None:
            raise RadioTimeoutError(f"等 CI-V 应答超时 (cmd=0x{cmd:02X})")
        return data[21:]

    # ---------------- 高层 CI-V 业务 ----------------

    def _civ_query(self, cmd_bytes: bytes, resp_cmd: int, timeout: float,
                   resp_sub: bytes = b"") -> bytes:
        """发一帧查询并等应答; 超时重发一次 (电台偶发丢首包, kappanhang 靠重传
        请求兜底, 此处简化为应用层重发一次)."""
        civ = civcmd.build_frame(CIV_IC705, CIV_FROM, cmd_bytes)
        for attempt in range(2):
            self.send_civ(civ)
            try:
                return self.read_civ(resp_cmd, timeout, sub=resp_sub)
            except RadioTimeoutError:
                if attempt == 1:
                    raise
                if self.verbose:
                    _log("serial", f"(cmd=0x{resp_cmd:02X} 超时, 重发一次)")
        raise RadioTimeoutError(f"cmd=0x{resp_cmd:02X} 无应答")

    def _civ_set(self, cmd_bytes: bytes) -> None:
        """发一帧设置命令 (CI-V 写命令电台无 ACK, 需要确认请读回复核)."""
        self.send_civ(civcmd.build_frame(CIV_IC705, CIV_FROM, cmd_bytes))

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

    def set_mode(self, mode: int, filt: int = 0x01) -> None:
        """设工作模式 (0x06) + 滤波器 (可选, 默认 FIL1).
        mode: 0x00=LSB 0x01=USB 0x02=AM 0x03=CW 0x04=RTTY 0x05=FM 0x06=WFM
              0x07=CW-R 0x08=RTTY-R 0x17=DV.
        注意: 切换模式后部分功能(ATT/PAMP/AGC/IF滤波)的可用性会变化."""
        self._civ_set(bytes([0x06, mode, filt]))

    def set_freq(self, hz: int) -> None:
        """设置 VFO 频率 (业余段白名单强制; 写命令电台无 ACK, 用 read_freq 复核)."""
        civcmd.assert_allowed_freq(hz)
        self.send_civ(civcmd.build_frame(CIV_IC705, CIV_FROM, civcmd.set_freq_bytes(hz)))

    def ptt(self, on: bool) -> None:
        """PTT 控制 (⚠️ on=True 会真正发射, 调用方须确保天线/负载安全)."""
        cmd = civcmd.ptt_on_bytes() if on else civcmd.ptt_off_bytes()
        self.send_civ(civcmd.build_frame(CIV_IC705, CIV_FROM, cmd))

    # ---------------- 亚音 (CTCSS) ----------------
    # 官方 IC-705 CI-V 参考 (p.21 格式图, 真机 2026-08-22 验证):
    #   0x16 0x5D 亚音功能模式 / 0x1B 0x00 中继亚音频率 / 0x1B 0x01 TSQL 频率
    #   频率数据 = 3 字节: [0x00 固定][100Hz|10Hz][1Hz|0.1Hz]
    #   88.5Hz → 00 08 85 (真机应答实测)

    #: 亚音模式枚举 (0x16 0x5D)
    TONE_MODES = {
        "off": 0x00, "tone": 0x01, "tsql": 0x02, "dtcs": 0x03,
        "dtcs_t": 0x06, "tone_t_dtcs_r": 0x07,
        "dtcs_t_tsql_r": 0x08, "tone_t_tsql_r": 0x09,
    }

    @staticmethod
    def _tone_freq_to_bcd(hz_x10: int) -> bytes:
        """亚音频率 (0.1Hz 单位, 如 885=88.5Hz) → 2B BCD."""
        if not (0 <= hz_x10 <= 9999):
            raise ValueError(f"亚音频率超范围 (0~999.9Hz): {hz_x10}")
        return bytes([((hz_x10 // 1000) << 4) | ((hz_x10 // 100) % 10),
                      (((hz_x10 // 10) % 10) << 4) | (hz_x10 % 10)])

    @staticmethod
    def _tone_bcd_to_hz_x10(b: bytes) -> int:
        return ((b[0] >> 4) * 1000 + (b[0] & 0x0F) * 100
                + (b[1] >> 4) * 10 + (b[1] & 0x0F))

    def set_tone_mode(self, mode, *, _retry: bool = True) -> None:
        """设亚音功能模式: "off"/"tone"/"tsql"/"dtcs" 等 (0x16 0x5D)."""
        val = self.TONE_MODES[mode] if isinstance(mode, str) else int(mode)
        self._civ_set(bytes([0x16, 0x5D, val]))

    def read_tone_mode(self, timeout: float = 2.0) -> int:
        """读亚音功能模式 (0x16 0x5D) → 原始枚举值."""
        resp = self._civ_query(bytes([0x16, 0x5D]), 0x16, timeout, resp_sub=b"\x5d")
        return resp[6]

    def set_tone_freq(self, hz_x10: int, *, tsql: bool = False) -> None:
        """设亚音频率 (0.1Hz 单位; tsql=False 中继亚音 0x1B 0x00, True TSQL 0x1B 0x01).
        数据 3 字节: 0x00 固定头 + 2B BCD."""
        self._civ_set(bytes([0x1B, 0x01 if tsql else 0x00])
                      + b"\x00" + self._tone_freq_to_bcd(hz_x10))

    def read_tone_freq(self, timeout: float = 2.0, *, tsql: bool = False) -> int:
        """读亚音频率 → 0.1Hz 单位 (如 885 = 88.5Hz)."""
        sub = bytes([0x01 if tsql else 0x00])
        resp = self._civ_query(bytes([0x1B]) + sub, 0x1B, timeout, resp_sub=sub)
        # 3 字节数据: [0x00][100Hz|10Hz][1Hz|0.1Hz]
        return self._tone_bcd_to_hz_x10(resp[7:9])

    # ---------------- ATT / NB ----------------

    def set_att(self, on: bool) -> None:
        """ATT 衰减开关 (0x11: 0x00=OFF, 0x20=20dB; ⚠️ 仅 HF/50MHz 段可设,
        VHF/UHF 段电台可能拒收)."""
        self._civ_set(bytes([0x11, 0x20 if on else 0x00]))

    def read_att(self, timeout: float = 2.0) -> int:
        """读 ATT 状态 → 原始值 (0x00=OFF / 0x20=20dB)."""
        resp = self._civ_query(bytes([0x11]), 0x11, timeout)
        return resp[5]

    def set_nb(self, on: bool) -> None:
        """NB 噪声抑制开关 (0x16 0x22: 0x00=OFF, 0x01=ON)."""
        self._civ_set(bytes([0x16, 0x22, 0x01 if on else 0x00]))

    def read_nb(self, timeout: float = 2.0) -> bool:
        """读 NB 状态 → True=ON."""
        resp = self._civ_query(bytes([0x16, 0x22]), 0x16, timeout, resp_sub=b"\x22")
        return resp[6] == 0x01

    # ---------------- 频差 (DUP/offset) ----------------

    #: 频差方向枚举 (0x0F)
    DUPLEX_MODES = {"simplex": 0x10, "dup-": 0x11, "dup+": 0x12}

    def set_duplex(self, mode) -> None:
        """设频差方向: "simplex"/"dup-"/"dup+" (0x0F).

        ⚠️ 影响发射频率 (=VFO±offset), 请确认目标中继/频段合规后再设。
        """
        val = self.DUPLEX_MODES[mode] if isinstance(mode, str) else int(mode)
        self._civ_set(bytes([0x0F, val]))

    def read_duplex(self, timeout: float = 2.0) -> int:
        """读频差/split 状态 → 原始值.
        ⚠️ 读写不归一 (官方命令表 + 真机 2026-08-24 实测):
        写入枚举 0x10=simplex / 0x11=DUP- / 0x12=DUP+;
        裸查询应答电台归一化后的当前状态: simplex 读回 0x00,
        DUP- 读回 0x11, DUP+ 读回 0x12."""
        resp = self._civ_query(bytes([0x0F]), 0x0F, timeout)
        return resp[5]

    @staticmethod
    def _offset_to_bcd3(hz: int) -> bytes:
        """频差偏移 Hz → 3B BCD (0x0D 格式, p.16 格式图, 真机 2026-08-22 验证):
        b0=[1kHz|100Hz]  b1=[100kHz|10kHz]  b2=[0 固定|1MHz]
        分辨率 100Hz; 600kHz → 00 60 00 (真机实测)."""
        if hz % 100 != 0 or not (0 <= hz <= 9_999_900):
            raise ValueError(f"偏移须为 100Hz 整数倍且 ≤ 9.9999MHz: {hz}")
        return bytes([
            (((hz // 1000) % 10) << 4) | ((hz // 100) % 10),
            (((hz // 100000) % 10) << 4) | ((hz // 10000) % 10),
            (hz // 1000000) % 10,
        ])

    @staticmethod
    def _bcd3_to_offset(b: bytes) -> int:
        return ((b[0] >> 4) * 1000 + (b[0] & 0x0F) * 100
                + (b[1] >> 4) * 100000 + (b[1] & 0x0F) * 10000
                + (b[2] & 0x0F) * 1000000)

    def set_offset(self, hz: int) -> None:
        """设频差偏移 (0x0D, 100Hz 整数倍; ⚠️ 影响发射频率)."""
        self._civ_set(bytes([0x0D]) + self._offset_to_bcd3(hz))

    def read_offset(self, timeout: float = 2.0) -> int:
        """读频差偏移 (0x0C) → Hz."""
        resp = self._civ_query(bytes([0x0C]), 0x0C, timeout)
        return self._bcd3_to_offset(resp[5:8])

    # ---------------- S 表 ----------------

    def read_smeter(self, timeout: float = 2.0) -> int:
        """读 S 表电平 (0x15 0x02) → 原始值 (0000=S0, 0120=S9, 0241=S9+60dB).
        数据 2B BCD MSB-first (与 0x14 电平族同风格, 范围 0000~0255);
        ⚠️ IC-705 在 S0 时只回 1 字节 0x00 (真机 2026-08-22 实测)."""
        resp = self._civ_query(bytes([0x15, 0x02]), 0x15, timeout, resp_sub=b"\x02")
        return self._bcd2_msb_to_int(resp[6:-1])

    # ---------------- 0x14 电平族 (0000~0255, 2B BCD MSB-first) ----------------
    # 真机 2026-08-24 实测: b0=[千位|百位] b1=[十位|个位] (MSB-first),
    # 128 → 01 28 / 255 → 02 55 / 72 → 00 72。

    @staticmethod
    def _int_to_bcd2_msb(val: int) -> bytes:
        if not (0 <= val <= 255):
            raise ValueError(f"电平值超范围 (0~255): {val}")
        return bytes([((val // 1000) % 10) << 4 | ((val // 100) % 10),
                      ((val // 10) % 10) << 4 | (val % 10)])

    @classmethod
    def _bcd2_msb_to_int(cls, data: bytes) -> int:
        val = (data[0] >> 4) * 1000 + (data[0] & 0x0F) * 100
        if len(data) >= 2:
            val += (data[1] >> 4) * 10 + (data[1] & 0x0F)
        return val

    def _read_level14(self, sub: int, timeout: float = 2.0) -> int:
        resp = self._civ_query(bytes([0x14, sub]), 0x14, timeout,
                               resp_sub=bytes([sub]))
        return self._bcd2_msb_to_int(resp[6:-1])

    def _set_level14(self, sub: int, val: int) -> None:
        self._civ_set(bytes([0x14, sub]) + self._int_to_bcd2_msb(val))

    def read_rf_power(self, timeout: float = 2.0) -> int:
        """读发射功率电平 (0x14 0x0A) → 0~255."""
        return self._read_level14(0x0A, timeout)

    def set_rf_power(self, val: int) -> None:
        """设发射功率电平 (0x14 0x0A, 0~255; ⚠️ 影响发射功率)."""
        self._set_level14(0x0A, val)

    def read_mic_gain(self, timeout: float = 2.0) -> int:
        """读 MIC 增益 (0x14 0x0B) → 0~255."""
        return self._read_level14(0x0B, timeout)

    def set_mic_gain(self, val: int) -> None:
        """设 MIC 增益 (0x14 0x0B, 0~255; ⚠️ 影响发射音频)."""
        self._set_level14(0x0B, val)

    def read_nr_level(self, timeout: float = 2.0) -> int:
        """读 NR 降噪深度 (0x14 0x06) → 0~255."""
        return self._read_level14(0x06, timeout)

    def set_nr_level(self, val: int) -> None:
        """设 NR 降噪深度 (0x14 0x06, 0~255)."""
        self._set_level14(0x06, val)

    def read_notch_pos(self, timeout: float = 2.0) -> int:
        """读 Manual Notch 位置 (0x14 0x0D) → 0~255."""
        return self._read_level14(0x0D, timeout)

    def set_notch_pos(self, val: int) -> None:
        """设 Manual Notch 位置 (0x14 0x0D, 0~255)."""
        self._set_level14(0x0D, val)

    def read_moni_gain(self, timeout: float = 2.0) -> int:
        """读 MONI 监听音量 (0x14 0x15) → 0~255."""
        return self._read_level14(0x15, timeout)

    def set_moni_gain(self, val: int) -> None:
        """设 MONI 监听音量 (0x14 0x15, 0~255)."""
        self._set_level14(0x15, val)

    def read_vox_gain(self, timeout: float = 2.0) -> int:
        """读 VOX 增益 (0x14 0x16) → 0~255."""
        return self._read_level14(0x16, timeout)

    def set_vox_gain(self, val: int) -> None:
        """设 VOX 增益 (0x14 0x16, 0~255)."""
        self._set_level14(0x16, val)

    # ---------------- 0x16 开关族扩展 ----------------

    def _read_sw16(self, sub: int, timeout: float = 2.0) -> int:
        resp = self._civ_query(bytes([0x16, sub]), 0x16, timeout,
                               resp_sub=bytes([sub]))
        return resp[6]

    def _set_sw16(self, sub: int, val: int) -> None:
        self._civ_set(bytes([0x16, sub, val]))

    #: PAMP 前置放大枚举 (0x16 0x02)
    PAMP_MODES = {"off": 0x00, "pamp1": 0x01, "pamp2": 0x02}
    #: AGC 档位枚举 (0x16 0x12)
    AGC_MODES = {"fast": 0x01, "mid": 0x02, "slow": 0x03}

    def set_pamp(self, mode) -> None:
        """设前置放大 (0x16 0x02): "off"/"pamp1"/"pamp2"."""
        val = self.PAMP_MODES[mode] if isinstance(mode, str) else int(mode)
        self._set_sw16(0x02, val)

    def read_pamp(self, timeout: float = 2.0) -> int:
        """读前置放大 (0x16 0x02) → 0x00=OFF/0x01=PAMP1/0x02=PAMP2."""
        return self._read_sw16(0x02, timeout)

    def set_agc(self, mode) -> None:
        """设 AGC 档位 (0x16 0x12): "fast"/"mid"/"slow"."""
        val = self.AGC_MODES[mode] if isinstance(mode, str) else int(mode)
        self._set_sw16(0x12, val)

    def read_agc(self, timeout: float = 2.0) -> int:
        """读 AGC 档位 (0x16 0x12) → 0x01/0x02/0x03."""
        return self._read_sw16(0x12, timeout)

    def set_nr(self, on: bool) -> None:
        """NR 降噪开关 (0x16 0x40)."""
        self._set_sw16(0x40, 0x01 if on else 0x00)

    def read_nr(self, timeout: float = 2.0) -> bool:
        """读 NR 降噪开关 (0x16 0x40)."""
        return self._read_sw16(0x40, timeout) == 0x01

    def set_notch_auto(self, on: bool) -> None:
        """Auto Notch 开关 (0x16 0x41)."""
        self._set_sw16(0x41, 0x01 if on else 0x00)

    def read_notch_auto(self, timeout: float = 2.0) -> bool:
        """读 Auto Notch 开关 (0x16 0x41)."""
        return self._read_sw16(0x41, timeout) == 0x01

    def set_notch_manual(self, on: bool) -> None:
        """Manual Notch 开关 (0x16 0x48)."""
        self._set_sw16(0x48, 0x01 if on else 0x00)

    def read_notch_manual(self, timeout: float = 2.0) -> bool:
        """读 Manual Notch 开关 (0x16 0x48)."""
        return self._read_sw16(0x48, timeout) == 0x01

    def set_moni(self, on: bool) -> None:
        """MONI 监听开关 (0x16 0x45)."""
        self._set_sw16(0x45, 0x01 if on else 0x00)

    def read_moni(self, timeout: float = 2.0) -> bool:
        """读 MONI 监听开关 (0x16 0x45)."""
        return self._read_sw16(0x45, timeout) == 0x01

    def set_vox(self, on: bool) -> None:
        """VOX 声控开关 (0x16 0x46)."""
        self._set_sw16(0x46, 0x01 if on else 0x00)

    def read_vox(self, timeout: float = 2.0) -> bool:
        """读 VOX 声控开关 (0x16 0x46)."""
        return self._read_sw16(0x46, timeout) == 0x01

    # ---------------- 0x1C 族: TX 状态 / TUNER / XFC ----------------

    def read_tx_status(self, timeout: float = 2.0) -> bool:
        """读收发状态 (0x1C 0x00) → True=TX 发射中."""
        resp = self._civ_query(bytes([0x1C, 0x00]), 0x1C, timeout,
                               resp_sub=b"\x00")
        return resp[6] == 0x01

    def set_tuner(self, on: bool) -> None:
        """天调开关 (0x1C 0x01: 0x00=OFF, 0x01=ON).
        ⚠️ IC-705 无内置天调 (AH-705 为外置), 未接天调时电台不应答 (2026-08-24 实测)."""
        self._civ_set(bytes([0x1C, 0x01, 0x01 if on else 0x00]))

    def read_tuner(self, timeout: float = 2.0) -> int:
        """读天调状态 (0x1C 0x01) → 0x00=OFF/0x01=ON/0x02=调谐中.
        ⚠️ IC-705 无内置天调, 未接 AH-705 时查询超时 (2026-08-24 实测)."""
        resp = self._civ_query(bytes([0x1C, 0x01]), 0x1C, timeout,
                               resp_sub=b"\x01")
        return resp[6]

    def tune_now(self) -> None:
        """触发天调调谐 (0x1C 0x01 data=0x02).
        ⚠️ 会载波发射数秒! 调用方须确保天线/负载安全."""
        self._civ_set(bytes([0x1C, 0x01, 0x02]))

    def set_xfc(self, on: bool) -> None:
        """XFC 发射频率监视开关 (0x1C 0x02)."""
        self._civ_set(bytes([0x1C, 0x02, 0x01 if on else 0x00]))

    def read_xfc(self, timeout: float = 2.0) -> bool:
        """读 XFC 发射频率监视开关 (0x1C 0x02)."""
        resp = self._civ_query(bytes([0x1C, 0x02]), 0x1C, timeout,
                               resp_sub=b"\x02")
        return resp[6] == 0x01

    # ---------------- SPLIT (0x0F 00/01) ----------------

    def set_split(self, on: bool) -> None:
        """SPLIT 异频开关 (0x0F: 0x00=OFF, 0x01=ON).
        ⚠️ 开启后发射频率=另一 VFO, 请确认合规."""
        self._civ_set(bytes([0x0F, 0x01 if on else 0x00]))

    # ---------------- RIT (0x21) ----------------
    # 频率格式 (p.25 图): 3B = [10Hz|1Hz][1kHz|100Hz][符号 00=+/01=-], ≤±9.999kHz

    def set_rit(self, on: bool) -> None:
        """RIT 开关 (0x21 0x01)."""
        self._civ_set(bytes([0x21, 0x01, 0x01 if on else 0x00]))

    def read_rit(self, timeout: float = 2.0) -> bool:
        """读 RIT 开关 (0x21 0x01)."""
        resp = self._civ_query(bytes([0x21, 0x01]), 0x21, timeout,
                               resp_sub=b"\x01")
        return resp[6] == 0x01

    def set_dtx(self, on: bool) -> None:
        """∂TX 开关 (0x21 0x02)."""
        self._civ_set(bytes([0x21, 0x02, 0x01 if on else 0x00]))

    def read_dtx(self, timeout: float = 2.0) -> bool:
        """读 ∂TX 开关 (0x21 0x02)."""
        resp = self._civ_query(bytes([0x21, 0x02]), 0x21, timeout,
                               resp_sub=b"\x02")
        return resp[6] == 0x01

    @staticmethod
    def _rit_freq_to_bcd(hz: int) -> bytes:
        """带符号 RIT 频率 Hz → 3B (p.25 格式)."""
        if not (-9999 <= hz <= 9999):
            raise ValueError(f"RIT 频率超范围 (±9999Hz): {hz}")
        v = abs(hz)
        return bytes([((v // 10) % 10) << 4 | (v % 10),
                      ((v // 1000) % 10) << 4 | ((v // 100) % 10),
                      0x01 if hz < 0 else 0x00])

    @staticmethod
    def _bcd_to_rit_freq(b: bytes) -> int:
        v = ((b[0] >> 4) * 10 + (b[0] & 0x0F)
             + (b[1] >> 4) * 1000 + (b[1] & 0x0F) * 100)
        return -v if b[2] == 0x01 else v

    def set_rit_freq(self, hz: int) -> None:
        """设 RIT 频率 (0x21 0x00, ±9999Hz, 带符号)."""
        self._civ_set(bytes([0x21, 0x00]) + self._rit_freq_to_bcd(hz))

    def read_rit_freq(self, timeout: float = 2.0) -> int:
        """读 RIT 频率 (0x21 0x00) → 带符号 Hz."""
        resp = self._civ_query(bytes([0x21, 0x00]), 0x21, timeout,
                               resp_sub=b"\x00")
        return self._bcd_to_rit_freq(resp[6:9])

    # ---------------- SCAN (0x0E) / SPEECH (0x13) ----------------

    #: 扫描模式枚举 (0x0E)
    SCAN_MODES = {
        "programmed_mem": 0x01, "programmed": 0x02, "df": 0x03,
        "fine_programmed": 0x12, "fine_df": 0x13,
        "memory": 0x22, "select_memory": 0x23, "mode_select": 0x24,
    }

    def scan_start(self, mode="programmed") -> None:
        """启动扫描 (0x0E; 模式见 SCAN_MODES). ⚠️ 电台会持续步进."""
        val = self.SCAN_MODES[mode] if isinstance(mode, str) else int(mode)
        self._civ_set(bytes([0x0E, val]))

    def scan_stop(self) -> None:
        """停止扫描 (0x0E 0x00)."""
        self._civ_set(bytes([0x0E, 0x00]))

    def speech(self, what="all") -> None:
        """语音播报 (0x13): "all"=0x00 全播报 / "freq"=0x01 频率 / "mode"=0x02 模式.
        电台会出声, 注意音量."""
        code = {"all": 0x00, "freq": 0x01, "mode": 0x02}[what]
        self._civ_set(bytes([0x13, code]))

    # ---------------- TBW 滤波带宽 (0x1A 0x03) ----------------

    def read_if_filter(self, timeout: float = 2.0) -> int:
        """读 IF 滤波带宽索引 (0x1A 0x03) → 原始索引 (含义随模式而异, p.19 表).
        ⚠️ FM 模式下电台不应答 (2026-08-24 实测); SSB/CW/RTTY/AM 模式可用."""
        resp = self._civ_query(bytes([0x1A, 0x03]), 0x1A, timeout,
                               resp_sub=b"\x03")
        return resp[6]

    def set_if_filter(self, idx: int) -> None:
        """设 IF 滤波带宽索引 (0x1A 0x03; 范围随模式: SSB/CW 0~40, AM 0~49)."""
        if not (0 <= idx <= 49):
            raise ValueError(f"滤波带宽索引超范围 (0~49): {idx}")
        self._civ_set(bytes([0x1A, 0x03, idx]))

    # ---------------- MAX TX POWER (0x1A 05 0036) ----------------

    def read_max_tx_power(self, timeout: float = 2.0) -> int:
        """读最大发射功率档位 (0x1A 05 0036) → 0~3."""
        resp = self._civ_query(bytes([0x1A, 0x05, 0x00, 0x36]), 0x1A, timeout,
                               resp_sub=b"\x05\x00\x36")
        return resp[8]

    def set_max_tx_power(self, val: int) -> None:
        """设最大发射功率档位 (0x1A 05 0036, 0~3; ⚠️ 影响发射上限)."""
        if not (0 <= val <= 3):
            raise ValueError(f"MAX TX POWER 档位超范围 (0~3): {val}")
        self._civ_set(bytes([0x1A, 0x05, 0x00, 0x36, val]))

    def __repr__(self) -> str:
        return (f"<RadioLink {self.host} user={self.username!r} "
                f"opened={self._opened} authID="
                f"{self.auth_id.hex() if self.auth_id else '-'}>")
