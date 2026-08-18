"""command_client — Command 信道 (UDP 50001) 登录/会话客户端 (IC-705 内嵌 RS-BA1 Server 定案).

2026-08-18 定案: 以 kappanhang (nonoo/kappanhang, 对真机 IC-705 验证的开源实现)
的 controlstream.go / streamcommon.go / pkt0.go / pkt7.go / passcode.go 为权威线序,
与本仓库 RemoteUty.exe 静态反汇编 (re/protocols/command_channel_cmd.md §1-2)
逐字节互证一致:

整体封装 (Q2 定案):
    业务命令**不直接**以业务头起手, 而是嵌套在 16B UDP2 传输头
    (type=0x0000 数据载波) 之后:

        +0x00 dword totalLen (LE)   = 整包长
        +0x04 word  type     (LE)   = 0x0000 数据 / 0x03 / 0x04 / 0x06 / 0x07
        +0x06 word  seq      (LE)   = 传输层序号 (tracked, 数据包从 1 起)
        +0x08 dword localSID (BE)   = (本地IP末2字节 << 16) | 本地端口
        +0x0C dword remoteSID(BE)   = pkt4 应答里服务器下发的 SID (字节级回显)
        +0x10 内层业务头 (0x10B):
            +0x00 word  0x0000      保留
            +0x02 word  version(BE) ConnectServer=0x70 / Auth=0x30 / ConnectTrans=0x80
            +0x04 word  type   (BE) 请求=0x0100~0x0106; 响应按 LE 读 = (idx<<8)|2
            +0x06 word  seq    (BE) 业务序号 (低字节有效, 见 kappanhang authInnerSendSeq)
            +0x08 4B    高字节=seq 高 8 位 + 0x00 + authStartID(2B) / 会话标识
            +0x0C 4B    会话标识 (ConnectServer 响应里为服务器 csid)
        +0x20 起载荷

    ⚠️ SID 线序为 BE (kappanhang 与真机实录一致); 服务器对 SID 只做字节级
    存储/回显, 不解释内容, 故 LE 自洽写法也能握手, 但与权威实现对齐选 BE。
    此前仓库 "LE 定案" 针对的是 Serial 信道 HALF-OPEN 场景的观察, 对本模块
    以 kappanhang 真机实现为准。

ConnectServer 登录包 (0x80B, kappanhang sendPktLogin 逐字节复刻):
    内层 version=0x70 type=0x0100; abs+0x1A 起 2B authStartID (客户端随机);
    abs+0x40..0x4F passcode(username) 16B; abs+0x50..0x5F passcode(password) 16B;
    abs+0x60 起 "icom-pc\\0" (客户端名, 不参与认证 — 对应静态结论 buf+0x50=Memo)。
    (静态反汇编的 buf+0x30/+0x40/+0x50 以**内层业务头**为基址, 绝对偏移即
     0x40/0x50/0x60; 凭证错位 0x10 会被服务器读出空用户名 → result=-2,
     2026-08-18 真机踩坑定案。)

登录应答 (0x60B, kappanhang init 期望 60 00 00 00 00 00 01 00 起手):
    abs[20:22] = 02 00 (LE word 0x0002, 对应静态 resp.buf[4]=2);
    abs[26:32] = 6B authID (前 2B 回显 authStartID, 后 4B 为服务器 csid);
    abs[48:52] == ff ff ff fe → 用户名/密码错误 (对应静态 resp.buf[0x20]=be32(-2))。

认证巩固 (kappanhang sendPktAuth, 0x40B): version=0x30, type=0x0100|magic;
    magic=0x02 (GetInfo) 与 magic=0x05 (KeepAlive) 各发一次; 应答 abs[21]==0x05
    视为 authOk。之后电台会主动推 0xA8(168B) 包, abs[66:82] = a8replyID。

申请 Serial/Audio (kappanhang sendRequestSerialAndAudio, 0x90B):
    version=0x80 type=0x0103 (ConnectTrans), 带 authID + a8replyID +
    "IC-705" + passcode(username) + 端口/采样率/缓冲配置;
    应答 144B 且 abs[96]==1 成功; abs[8:12]=新 remoteSID, abs[12:16]=新 localSID
    (电台在 ConnectTrans 后切换控制会话 SID, 后续控制包必须用新 SID!),
    abs[26:32] 为新 authID。80B 应答 abs[48:51]==ff ff ff → 会话被挤占, 需电台重启。

⚠️ 两层协议的区分 (静态反汇编 + 真机开源实现确证, 见 re/protocols/command_channel_cmd.md +
credential_and_session.md + capture_todo.md):
    - 命令分发器 0x416CD0: 读 buf[4] (word, 大端) 为命令类型, -0x100 后查 7 项跳表。
    - ExecConnectServer 0x416DE0:
        * buf[2] (word, 大端) 须 == 0x70 (版本/标志)
        * 载荷从内层业务头 buf+0x30 起三个字段块 (UserName / Password / Memo, 各 0x10 字节)
        * CheckUser(0x43EC60) 认证; 通过则构造响应 buf[4]=2, buf[6]=seq 回显,
          buf[8]=请求 buf[8] 字节交换回显 (会话标识)
    - 心跳: ExecKeepAlive 0x418910, 请求 type=0x0105, 响应 buf[4]=0x0502;
      心跳超时阈值 0x15F90 ms = 90 秒。
    - 响应类型规则: resp.buf[4] = (command_index << 8) | 0x2
      (ConnectServer=0x2 / GetInfo=0x202 / KeepAlive=0x502 / Disconnect=0x102)
    - RemoteUty.exe (PC 服务器) 的 CCommandCtrl 分发层用 build_command_header(外层
      type 0x0100~0x0106); 而电台内置固件 (IC-705 原生 WiFi 服务器) 的 Command 信道
      与 Serial 共用同一 16 字节 wire 头, 外层 type 恒为 0x00, 命令身份由内层字段
      区分 (登录 0x00 / conninfo 0x03 / token 0x02|0x05|0x01)。
    - 客户端直连电台 (radio 原生服务器) 应使用 build_login_request / build_command_packet
      + encode_icom_credential; build_command_header 仅适配 PC 服务器 (RS-BA1)。

用法 (新链路, 直连电台):
    见 tools/probe_command_connect.py (完整登录 → Serial 透传闭环探测)。

用法 (旧链路, PC 服务器):
    with CommandClient(host="192.168.1.10", username="u", password="p") as c:
        ok = c.connect()
        c.keepalive()

参考:
    - kappanhang controlstream.go / streamcommon.go / pkt0.go / pkt7.go / passcode.go
    - re/protocols/command_channel_cmd.md (静态分发器 0x416CD0)
    - re/protocols/credential_and_session.md (CheckUser 0x43EC60)
    - src/rsba1/serial/serial_client.py (同风格 UDP 客户端)
"""
from __future__ import annotations

import socket
import struct
from typing import Optional, Tuple

__all__ = [
    "DEFAULT_COMMAND_PORT",
    "CMD_HEADER_SIZE",
    "CMD_CONNECT",
    "CMD_DISCONNECT",
    "CMD_GETINFO",
    "CMD_CONNECTTRANS",
    "CMD_DISCONNECTTRANS",
    "CMD_KEEPALIVE",
    "CMD_DEBUG",
    "VERSION_CONNECT",
    "VERSION_AUTH",
    "VERSION_CONNECT_TRANS",
    "LOGIN_PACKET_LEN",
    "LOGIN_RESPONSE_LEN",
    "AUTH_PACKET_LEN",
    "CONNECT_TRANS_PACKET_LEN",
    "CONNECT_TRANS_RESPONSE_LEN",
    "A8_PACKET_LEN",
    "CLIENT_NAME",
    "RADIO_MODEL_NAME",
    "CommandClientError",
    "CommandTimeoutError",
    "AuthFailedError",
    "CommandClient",
    "encode_icom_credential",
    "passcode",
    "build_command_packet",
    "build_command_header",
    "parse_command_header",
    "build_connect_request",
    "build_keepalive_request",
    "make_local_sid",
    "build_transport_header",
    "build_pkt3",
    "build_pkt6",
    "build_disconnect_pkt",
    "build_pkt7",
    "build_idle_pkt0",
    "build_login_request",
    "build_auth_request",
    "build_connect_trans_request",
    "parse_login_response",
    "parse_connect_trans_response",
    "parse_auth_reply_magic",
    "is_pkt7",
    "is_idle_pkt0",
    "is_a8_packet",
    "extract_a8_reply_id",
]

# 默认 Command 信道端口 (注册表 CommandPort, 线上确证)
DEFAULT_COMMAND_PORT = 50001

# Command 信道 wire 头固定 0x10 字节 (与 Serial 信道一致, 字段布局不同)
CMD_HEADER_SIZE = 0x10

# 业务命令类型 (请求内层头 +0x04, BE; 分发器 rol ax,8 还原后 -0x100 查 7 项跳表)
CMD_CONNECT = 0x0100          # ConnectServer (连接 + 凭证)
CMD_DISCONNECT = 0x0101       # DisconnectServer
CMD_GETINFO = 0x0102          # GetInfo (kappanhang 首个 auth 巩固包 magic=0x02)
CMD_CONNECTTRANS = 0x0103     # ConnectTrans (申请 Serial/Audio 信道)
CMD_DISCONNECTTRANS = 0x0104  # DisconnectTrans
CMD_KEEPALIVE = 0x0105        # KeepAlive (kappanhang 周期 auth magic=0x05)
CMD_DEBUG = 0x0106            # (未命名, 0x418D70)

# 内层业务头 version 字段 (BE, 静态确证 + kappanhang 线序一致)
VERSION_CONNECT = 0x0070      # ConnectServer (静态: buf[2] 须 == 0x70)
VERSION_AUTH = 0x0030         # GetInfo/KeepAlive 巩固包 (kappanhang sendPktAuth)
VERSION_CONNECT_TRANS = 0x0080  # ConnectTrans (kappanhang sendRequestSerialAndAudio)

# 各包定长 (kappanhang 逐字节确证)
LOGIN_PACKET_LEN = 0x80             # ConnectServer 请求 128B
LOGIN_RESPONSE_LEN = 0x60           # ConnectServer 应答 96B
AUTH_PACKET_LEN = 0x40              # GetInfo/KeepAlive 巩固包 64B
CONNECT_TRANS_PACKET_LEN = 0x90     # ConnectTrans 请求 144B
CONNECT_TRANS_RESPONSE_LEN = 0x90   # ConnectTrans 应答 144B
A8_PACKET_LEN = 0xA8                # 电台主动下推的 0xA8 包 168B

# 客户端标识 (ConnectServer Memo 字段, 不参与认证) / 电台型号名 (ConnectTrans 用)
CLIENT_NAME = b"icom-pc\x00"
RADIO_MODEL_NAME = b"IC-705\x00\x00"

# 载荷字段偏移 (旧链路, buf+0x30 起三个 0x10 字节块)
CONNECT_FIELD_USER = 0x30
CONNECT_FIELD_PASS = 0x40
CONNECT_FIELD_MEMO = 0x50
CONNECT_FIELD_SIZE = 0x10

# 响应类型规则: resp.buf[4] = (command_index << 8) | 0x2
def _resp_type(req_type: int) -> int:
    """由请求命令类型推导期望的响应类型 (静态确证规则)."""
    return ((req_type & 0x00FF) << 8) | 0x2


# passCode 混淆表 (95 字节, 索引 = p-32, 见 re/protocols/capture_todo.md ③)
# 来源: 真机 IC-705 跑通的开源实现 (j0uni/icom-udp-example 与 OrbitDeck 的
# _encode_icom_credential 逐字节一致) 交叉印证, 2026-08-12 联网确证。
_PASSCODE_TABLE = (
    b"\x47\x5d\x4c\x42\x66\x20\x23\x46\x4e\x57\x45\x3d\x67\x76\x60\x41"
    b"\x62\x39\x59\x2d\x68\x7e\x7c\x65\x7d\x49\x29\x72\x73\x78\x21\x6e"
    b"\x5a\x5e\x4a\x3e\x71\x2c\x2a\x54\x3c\x3a\x63\x4f\x43\x75\x27\x79"
    b"\x5b\x35\x70\x48\x6b\x56\x6f\x34\x32\x6c\x30\x61\x6d\x7b\x2f\x4b"
    b"\x64\x38\x2b\x2e\x50\x40\x3f\x55\x33\x37\x25\x77\x24\x26\x74\x6a"
    b"\x28\x53\x4d\x69\x22\x5c\x44\x31\x36\x58\x3b\x7a\x51\x5f\x52"
)


class CommandClientError(Exception):
    """Command 信道客户端基础异常."""


class CommandTimeoutError(CommandClientError):
    """等待 Command 响应超时."""


class AuthFailedError(CommandClientError):
    """认证失败 (用户名/密码错误, 或会话被挤占)."""


# ============================================================
# ICOM passcode 编码 (kappanhang passcode.go 逐行移植)
# ============================================================

# 可打印字符 (32..126) → 编码字节 置换表
_PASSCODE_SEQUENCE = {
    32: 0x47, 33: 0x5D, 34: 0x4C, 35: 0x42, 36: 0x66, 37: 0x20, 38: 0x23,
    39: 0x46, 40: 0x4E, 41: 0x57, 42: 0x45, 43: 0x3D, 44: 0x67, 45: 0x76,
    46: 0x60, 47: 0x41, 48: 0x62, 49: 0x39, 50: 0x59, 51: 0x2D, 52: 0x68,
    53: 0x7E, 54: 0x7C, 55: 0x65, 56: 0x7D, 57: 0x49, 58: 0x29, 59: 0x72,
    60: 0x73, 61: 0x78, 62: 0x21, 63: 0x6E, 64: 0x5A, 65: 0x5E, 66: 0x4A,
    67: 0x3E, 68: 0x71, 69: 0x2C, 70: 0x2A, 71: 0x54, 72: 0x3C, 73: 0x3A,
    74: 0x63, 75: 0x4F, 76: 0x43, 77: 0x75, 78: 0x27, 79: 0x79, 80: 0x5B,
    81: 0x35, 82: 0x70, 83: 0x48, 84: 0x6B, 85: 0x56, 86: 0x6F, 87: 0x34,
    88: 0x32, 89: 0x6C, 90: 0x30, 91: 0x61, 92: 0x6D, 93: 0x7B, 94: 0x2F,
    95: 0x4B, 96: 0x64, 97: 0x38, 98: 0x2B, 99: 0x2E, 100: 0x50, 101: 0x40,
    102: 0x3F, 103: 0x55, 104: 0x33, 105: 0x37, 106: 0x25, 107: 0x77,
    108: 0x24, 109: 0x26, 110: 0x74, 111: 0x6A, 112: 0x28, 113: 0x53,
    114: 0x4D, 115: 0x69, 116: 0x22, 117: 0x5C, 118: 0x44, 119: 0x31,
    120: 0x36, 121: 0x58, 122: 0x3B, 123: 0x7A, 124: 0x51, 125: 0x5F,
    126: 0x52,
}


def encode_icom_credential(text: str) -> bytes:
    """用 ICOM 固定 95 字节替换表对用户名/密码做位置相关混淆 (非加密).

    对第 i(0 基) 个字符 c: p = c + i; 若 p > 126 则 p = 32 + p % 127;
    取 table[p - 32]。编码后固定 16 字节, 不足补 \\0, 超出截断。

    参数:
        text: 需混淆的明文字符串.

    返回:
        16 字节混淆结果.
    """
    out = bytearray()
    # 与开源实现一致: 按 latin1 取字节, 最多 16 字节
    raw = (text or "").encode("latin1", "ignore")[:16]
    for index, ch in enumerate(raw):
        position = index + ch
        if position > 126:
            position = 32 + position % 127
        out.append(_PASSCODE_TABLE[position - 32])
    out.extend(b"\x00" * (16 - len(out)))
    return bytes(out)


def passcode(text: str) -> bytes:
    """ICOM 共享密钥编码 (kappanhang passcode() 逐行移植).

    逐字符: 码点 + 下标 → 超过 126 则折回 32 + p % 127 → 查置换表;
    定长 16 字节, 不足补 0。

    ⚠️ 用户名/密码**必须**经此编码后再入包 — 此前 V1~V6 探测用明文
    UTF-8 填充, 是拿不到 0x0002 应答的根本原因之一。
    """
    res = bytearray(16)
    raw = text.encode("ascii", "replace")
    for i, ch in enumerate(raw[:16]):
        p = ch + i
        if p > 126:
            p = 32 + p % 127
        res[i] = _PASSCODE_SEQUENCE.get(p, 0)
    return bytes(res)


# ============================================================
# 会话标识 (SID) 与传输层头
# ============================================================

def make_local_sid(ip_str: str, port: int) -> int:
    """localSID = (本地IP末2字节 << 16) | 本地端口 (kappanhang streamcommon.init).

    与 2026-08-12 真机定案的语义一致; 上线时按 **BE** 序列化 (见
    build_transport_header)。
    """
    ip_val = struct.unpack(">I", socket.inet_aton(ip_str))[0]
    return ((ip_val & 0xFFFF) << 16) | (port & 0xFFFF)


def build_transport_header(
    total_len: int,
    type_: int,
    seq: int,
    local_sid: int,
    remote_sid: int,
) -> bytes:
    """16B UDP2 传输头 (kappanhang 线序): totalLen/type/seq LE, SID 对 BE."""
    return (
        struct.pack("<IHH", total_len & 0xFFFFFFFF, type_ & 0xFFFF, seq & 0xFFFF)
        + struct.pack(">II", local_sid & 0xFFFFFFFF, remote_sid & 0xFFFFFFFF)
    )


def build_pkt3(local_sid: int) -> bytes:
    """pkt3 会话握手 (type=0x03, seq=0, remoteSID=0)."""
    return build_transport_header(0x10, 0x03, 0, local_sid, 0)


def build_pkt6(local_sid: int, remote_sid: int) -> bytes:
    """pkt6 会话握手确认 (type=0x06, seq=1)."""
    return build_transport_header(0x10, 0x06, 1, local_sid, remote_sid)


def build_disconnect_pkt(local_sid: int, remote_sid: int) -> bytes:
    """传输层断连 (type=0x05, kappanhang sendDisconnect)."""
    return build_transport_header(0x10, 0x05, 0, local_sid, remote_sid)


def build_idle_pkt0(local_sid: int, remote_sid: int, seq: int = 0) -> bytes:
    """16B 空载数据包 (type=0x0000, 链路保活; kappanhang sendIdle)."""
    return build_transport_header(0x10, 0x00, seq, local_sid, remote_sid)


def build_pkt7(
    local_sid: int,
    remote_sid: int,
    seq: int,
    reply_id: Optional[bytes] = None,
    inner_seq: int = 0x8304,
) -> bytes:
    """21B keepalive (type=0x0007; kappanhang pkt7.sendDo).

    reply_id=None → 主动请求: flag=0x00, 载荷 = [随机/0, innerSeq LE, 0x06];
    reply_id=电台请求包 [17:21] 4B → 应答: flag=0x01, 原样回显。
    """
    if reply_id is None:
        rid = bytes([0, inner_seq & 0xFF, (inner_seq >> 8) & 0xFF, 0x06])
        flag = 0x00
    else:
        if len(reply_id) != 4:
            raise ValueError(f"reply_id 须 4 字节, 实际 {len(reply_id)}")
        rid = bytes(reply_id)
        flag = 0x01
    return build_transport_header(0x15, 0x07, seq, local_sid, remote_sid) + bytes([flag]) + rid


def is_pkt7(data: bytes) -> bool:
    """是否 keepalive 包 (21B, type=0x0007; 首字节 0x15/0x00 皆有, 故只看 [1:6])."""
    return len(data) == 21 and data[1:6] == b"\x00\x00\x00\x07\x00"


def is_idle_pkt0(data: bytes) -> bool:
    """是否 16B 空载数据包 (totalLen=0x10, type=0x0000)."""
    return len(data) == 16 and data[:6] == b"\x10\x00\x00\x00\x00\x00"


def is_a8_packet(data: bytes) -> bool:
    """是否电台主动下推的 0xA8 包 (168B; 含 ConnectTrans 所需的 a8replyID)."""
    return len(data) == A8_PACKET_LEN and data[:6] == b"\xa8\x00\x00\x00\x00\x00"


def extract_a8_reply_id(data: bytes) -> bytes:
    """从 0xA8 包提取 16B a8replyID (abs[66:82], kappanhang gotA8ReplyID)."""
    if not is_a8_packet(data):
        raise ValueError("非 0xA8 包")
    return data[66:82]


# ============================================================
# 业务请求构造 (内层头 + 载荷, 嵌套于传输头之后)
# ============================================================

def _build_biz_header(
    version: int,
    type_cmd: int,
    inner_seq: int,
    id4: bytes = b"\x00\x00\x00\x00",
) -> bytes:
    """内层业务头 (**严格 0x10B**).

    布局 (kappanhang sendPktLogin/sendPktAuth 逐字节对应, 绝对偏移 = 下值 + 0x10):
        +0x00 word 0x0000
        +0x02 word version (BE)
        +0x04 word type    (BE, 0x0100|idx)
        +0x06 byte 0x00 + byte innerSeq 低 8 位
        +0x08 byte innerSeq 高 8 位 + byte 0x00
        +0x0A 4B   id4 (登录包 = authStartID(2B)+00 00; 巩固包 = authID 前 4B)
        +0x0E 2B   0x0000 (巩固包的 authID 后 2B 由调用方另行覆写)
    """
    return (
        b"\x00\x00"
        + struct.pack(">H", version & 0xFFFF)
        + struct.pack(">H", type_cmd & 0xFFFF)
        + bytes([0x00, inner_seq & 0xFF, (inner_seq >> 8) & 0xFF, 0x00])
        + bytes(id4[:4]).ljust(4, b"\x00")
        + b"\x00\x00"
    )


def build_login_request(
    username: str,
    password: str,
    *,
    local_sid: int,
    remote_sid: int,
    outer_seq: int = 1,
    inner_seq: int = 0,
    auth_start_id: Optional[bytes] = None,
) -> bytes:
    """ConnectServer 登录请求 (整包 0x80B; kappanhang sendPktLogin 逐字节复刻).

    参数:
        username/password: 明文 (内部做 passcode 编码, 各 16B)
        local_sid/remote_sid: 传输会话标识 (remote_sid 来自 pkt4)
        outer_seq:  传输层 tracked seq (首包 = 1)
        inner_seq:  业务序号 (authInnerSendSeq, 首包 = 0)
        auth_start_id: 2B 客户端随机数 (响应里回显为 authID 前 2B); None 用 0
    """
    aid = bytes(auth_start_id[:2]).ljust(2, b"\x00") if auth_start_id else b"\x00\x00"
    pkt = bytearray(LOGIN_PACKET_LEN)
    pkt[0x00:0x10] = build_transport_header(
        LOGIN_PACKET_LEN, 0x00, outer_seq, local_sid, remote_sid
    )
    # 内层业务头: version=0x70, type=0x0100, seq + authStartID
    pkt[0x10:0x20] = _build_biz_header(VERSION_CONNECT, CMD_CONNECT, inner_seq, aid + b"\x00\x00")
    # 静态偏移 buf+0x30/+0x40/+0x50 相对内层头 (buf=abs 0x10) → 绝对偏移 0x40/0x50/0x60
    pkt[0x40:0x50] = passcode(username)   # buf+0x30 UserName (参与 CheckUser)
    pkt[0x50:0x60] = passcode(password)   # buf+0x40 Password (参与 CheckUser)
    pkt[0x60:0x60 + len(CLIENT_NAME)] = CLIENT_NAME  # buf+0x50 Memo (不参与认证)
    return bytes(pkt)


def build_auth_request(
    magic: int,
    *,
    local_sid: int,
    remote_sid: int,
    outer_seq: int,
    inner_seq: int,
    auth_id: bytes,
) -> bytes:
    """认证巩固包 (整包 0x40B; kappanhang sendPktAuth).

    magic=0x02 → type 0x0102 (GetInfo); magic=0x05 → type 0x0105 (KeepAlive)。
    auth_id 为登录应答 [26:32] 的 6B 会话令牌, 置于 abs[26:32]。
    """
    if len(auth_id) != 6:
        raise ValueError(f"auth_id 须 6 字节, 实际 {len(auth_id)}")
    pkt = bytearray(AUTH_PACKET_LEN)
    pkt[0x00:0x10] = build_transport_header(
        AUTH_PACKET_LEN, 0x00, outer_seq, local_sid, remote_sid
    )
    pkt[0x10:0x20] = _build_biz_header(
        VERSION_AUTH, CMD_CONNECT | (magic & 0xFF), inner_seq, auth_id[:4]
    )
    pkt[0x1E:0x20] = auth_id[4:6]
    return bytes(pkt)


def build_connect_trans_request(
    username: str,
    *,
    local_sid: int,
    remote_sid: int,
    outer_seq: int,
    inner_seq: int,
    auth_id: bytes,
    a8_reply_id: bytes,
    serial_port: int = 50002,
    audio_port: int = 50003,
    sample_rate: int = 48000,
    tx_seq_buf_ms: int = 300,
) -> bytes:
    """ConnectTrans 申请 Serial/Audio 信道 (整包 0x90B; kappanhang sendRequestSerialAndAudio).

    参数:
        auth_id:     当前 6B 会话令牌
        a8_reply_id: 电台 0xA8 包 [66:82] 的 16B 标识
        serial_port/audio_port: 申请开启的 UDP 端口 (默认 50002/50003)
        sample_rate: 音频采样率 (kappanhang 48000)
        tx_seq_buf_ms: 重传缓冲毫秒数 (kappanhang 300; >500~600 会导致音频 TX 失败)
    """
    if len(auth_id) != 6:
        raise ValueError(f"auth_id 须 6 字节, 实际 {len(auth_id)}")
    if len(a8_reply_id) != 16:
        raise ValueError(f"a8_reply_id 须 16 字节, 实际 {len(a8_reply_id)}")
    pkt = bytearray(CONNECT_TRANS_PACKET_LEN)
    pkt[0x00:0x10] = build_transport_header(
        CONNECT_TRANS_PACKET_LEN, 0x00, outer_seq, local_sid, remote_sid
    )
    pkt[0x10:0x20] = _build_biz_header(
        VERSION_CONNECT_TRANS, CMD_CONNECTTRANS, inner_seq, auth_id[:4]
    )
    pkt[0x1E:0x20] = auth_id[4:6]
    pkt[0x20:0x30] = a8_reply_id
    pkt[0x40:0x48] = RADIO_MODEL_NAME                      # "IC-705\0\0"
    pkt[0x60:0x70] = passcode(username)                    # 用户名 (passcode 编码)
    pkt[0x70:0x74] = b"\x01\x01\x04\x04"
    pkt[0x76:0x78] = struct.pack(">H", sample_rate)
    pkt[0x7A:0x7C] = struct.pack(">H", sample_rate)
    pkt[0x7E:0x80] = struct.pack(">H", serial_port)
    pkt[0x82:0x84] = struct.pack(">H", audio_port)
    pkt[0x86:0x88] = struct.pack(">H", tx_seq_buf_ms)
    pkt[0x88] = 0x01
    return bytes(pkt)


# ============================================================
# 响应解析
# ============================================================

def parse_login_response(data: bytes) -> Tuple[bool, bytes, int]:
    """解析 ConnectServer 应答 (0x60B).

    返回 (ok, auth_id, result_code):
        ok:          True = 认证通过
        auth_id:     6B 会话令牌 (abs[26:32]; 失败时全 0 无意义)
        result_code: abs[48:52] BE int32 (0=通过, -2=用户名/密码错误)
    异常:
        ValueError - 包长不符 / 起手特征不符。
    """
    if len(data) != LOGIN_RESPONSE_LEN:
        raise ValueError(f"登录应答须 0x60B, 实际 {len(data)}B: {data.hex()}")
    if data[:5] != b"\x60\x00\x00\x00\x00":
        raise ValueError(f"非登录应答特征: {data[:8].hex()}")
    auth_id = data[26:32]
    result = struct.unpack(">i", data[48:52])[0]
    return result == 0, auth_id, result


def parse_connect_trans_response(data: bytes) -> Tuple[bool, int, int, bytes, str]:
    """解析 ConnectTrans 应答 (144B).

    返回 (ok, new_remote_sid, new_local_sid, new_auth_id, dev_name):
        ok:             abs[96] == 1
        new_remote_sid: abs[8:12] BE (服务器新 localSID)
        new_local_sid:  abs[12:16] BE (服务器为本端新分配的 SID)
        new_auth_id:    abs[26:32]
        dev_name:       abs[64:] 的 \0 结尾设备名 ("IC-705")
    """
    if len(data) != CONNECT_TRANS_RESPONSE_LEN:
        raise ValueError(f"ConnectTrans 应答须 0x90B, 实际 {len(data)}B: {data.hex()}")
    if data[:5] != b"\x90\x00\x00\x00\x00":
        raise ValueError(f"非 ConnectTrans 应答特征: {data[:8].hex()}")
    ok = data[96] == 1
    new_remote = struct.unpack(">I", data[8:12])[0]
    new_local = struct.unpack(">I", data[12:16])[0]
    new_auth_id = data[26:32]
    dev_raw = data[64:].split(b"\x00", 1)[0]
    dev_name = dev_raw.decode("ascii", "replace")
    return ok, new_remote, new_local, new_auth_id, dev_name


def parse_auth_reply_magic(data: bytes) -> Optional[int]:
    """从 0x40B 巩固包应答取 magic (abs[21]; 0x02=GetInfo, 0x05=KeepAlive).

    非巩固包应答返回 None。
    """
    if len(data) == AUTH_PACKET_LEN and data[:6] == b"\x40\x00\x00\x00\x00\x00":
        return data[21]
    return None


# ============================================================
# 旧链路 (PC 服务器 RemoteUtity.exe 分发层) wire 头编解码
# ============================================================

def build_command_packet(
    inner_len: int,
    req_code: int,
    sender_id: int,
    receiver_id: int,
    inner_seq: int,
    local_token: int,
    rig_token: int,
    *,
    seq: int = 0,
    body: bytes = b"",
) -> bytes:
    """构造 Command 信道业务包 (16 字节 wire 头 + 内层字段 + 载荷).

    布局 (与 Serial 共用同一 wire 头, 外层 type 恒为 0x00; 命令身份由
    内层字段区分, 见 re/protocols/capture_todo.md ④投但确证):
        +0x00 dword totalLen (LE)   = 0x10 + 内层字段 + len(body)
        +0x04 word  type    (LE)    = 0x00 (数据)
        +0x06 word  seq     (LE)    = 传输层序号
        +0x08 dword sender  (LE)    = 本端会话 id (my_id)
        +0x0C dword receiver(LE)    = 对端会话 id (remote_id)
        +0x10 word  innerLen (BE)   = 内层字段长 (不含 body)
        +0x12 byte  0x01            = 请求标志
        +0x13 byte  req_code        = 请求码 (0x00 登录 / 0x03 连接信息 / token 见下)
        +0x14 word  innerSeq (BE)   = 内层序号
        +0x1A word  localToken(BE)  = token 请求 id
        +0x1C dword rigToken (BE)   = 会话 token
        +0x20 ...                   = body (各业务字段块)

    注: 此处 inner_len 为 [0x10, 0x20) 固定 0x10 字节内层字段区长度 (不含 body),
        与 j0uni 的 LOGIN_SIZE-0x10 等定义对应 (body 已并入 totalLen)。

    参数:
        inner_len: 内层固定字段区长度 (BE16, 如 0x70/0x30/0x80)
        req_code:  请求码 (登录 0x00 / conninfo 0x03 / token 见调用方)
        sender_id: 本端会话 id
        receiver_id: 对端会话 id
        inner_seq: 内层序号 (BE16)
        local_token: token 请求 id (BE16)
        rig_token:  会话 token (BE32)
        seq:        传输层序号 (LE16)
        body:       业务字段块 (用户/密码/名称等)

    返回:
        完整 Command 业务包 bytes.
    """
    head = bytearray(0x20)
    total_len = 0x20 + len(body)
    struct.pack_into("<I", head, 0, total_len)
    struct.pack_into("<H", head, 0x06, seq & 0xFFFF)
    struct.pack_into("<I", head, 0x08, sender_id & 0xFFFFFFFF)
    struct.pack_into("<I", head, 0x0C, receiver_id & 0xFFFFFFFF)
    struct.pack_into(">H", head, 0x10, inner_len & 0xFFFF)
    head[0x12] = 0x01
    head[0x13] = req_code & 0xFF
    struct.pack_into(">H", head, 0x14, inner_seq & 0xFFFF)
    struct.pack_into(">H", head, 0x1A, local_token & 0xFFFF)
    struct.pack_into(">I", head, 0x1C, rig_token & 0xFFFFFFFF)
    return bytes(head) + body


def build_command_header(
    type_cmd: int = CMD_CONNECT,
    seq: int = 0,
    field_8: int = 0,
    field_C: int = 0,
    total_len: int = 0,
    version: int = 0,
) -> bytes:
    """构造 Command 信道 wire 头 bytes (0x10 字节).

    ⚠️ totalLen 与 version 的偏移/字节序为静态推断, 待线上抓包复核 (R-U4)。

    布局 (与 Serial 信道不同, Command 含 version 字段):
        +0x00 word  totalLen (LE)   报文总长度
        +0x02 word  version (BE)    版本/标志 (ConnectServer=0x0070)
        +0x04 word  type    (BE)    命令类型 (0x0100~0x0106)
        +0x06 word  seq     (BE)    序号
        +0x08 dword field_8 (LE)    会话/对端标识
        +0x0C dword field_C (LE)    会话/对端标识

    参数:
        type_cmd:  命令类型 (CMD_CONNECT 等)
        seq:       序号 (BE uint16)
        field_8:   会话/对端标识高半
        field_C:   会话/对端标识低半
        total_len: 报文总长度 (word, LE); 0 时按 CMD_HEADER_SIZE 计算
        version:   版本/标志 (BE), ConnectServer 应传 VERSION_CONNECT

    返回:
        wire 头 bytes (16 字节)。
    """
    if version == 0 and type_cmd == CMD_CONNECT:
        version = VERSION_CONNECT
    if total_len == 0:
        total_len = CMD_HEADER_SIZE
    return (
        struct.pack("<H", total_len & 0xFFFF)
        + struct.pack(">H", version & 0xFFFF)
        + struct.pack(">H", type_cmd & 0xFFFF)
        + struct.pack(">H", seq & 0xFFFF)
        + struct.pack("<I", field_8 & 0xFFFFFFFF)
        + struct.pack("<I", field_C & 0xFFFFFFFF)
    )


def parse_command_header(data: bytes) -> Tuple[int, int, int, int, int]:
    """解析 Command wire 头.

    参数:
        data: 至少 0x10 字节的 UDP 包前部.

    返回:
        (total_len, version, type_cmd, seq, field_8, field_C) 的 6 元组:
            total_len: 报文总长度 (word)
            version:   版本/标志
            type_cmd:  命令/响应类型
            seq:       序号
            field_8:   会话标识高半
            field_C:   会话标识低半

    异常:
        ValueError - data 长度不足 0x10。
    """
    if len(data) < CMD_HEADER_SIZE:
        raise ValueError(
            f"Command wire 头长度不足: 需要 {CMD_HEADER_SIZE}, 实际 {len(data)}"
        )
    total_len = struct.unpack("<H", data[0:2])[0]
    version, type_cmd, seq = struct.unpack(">HHH", data[2:8])
    f8, fc = struct.unpack("<II", data[8:16])
    return total_len, version, type_cmd, seq, f8, fc


def build_connect_request(
    username: str,
    password: str,
    memo: str = "",
    *,
    seq: int = 1,
    field_8: int = 0,
    field_C: int = 0,
    inner_seq: int = 0,
    local_token: int = 0,
    rig_token: int = 0,
) -> bytes:
    """构造 ConnectServer 登录请求包 (0x80 字节, 权威 wire 布局).

    载荷布局 (整包偏移, 见 j0uni/OrbitDeck 真机确证):
        [0x40] 16 用户名 (passCode 混淆)
        [0x50] 16 密码   (passCode 混淆)
        [0x60] 16 客户端名 (明文, 不足 \\0 填充)

    参数:
        username: 用户名
        password: 密码
        memo:     客户端名 (原 Memo 字段, 写入 [0x60])
        seq:      传输层序号 (LE uint16)
        field_8:  本端会话 id (sender)
        field_C:  对端会话 id (receiver)
        inner_seq: 内层序号 (BE uint16)
        local_token: token 请求 id (BE uint16)
        rig_token:  会话 token (BE32)

    返回:
        完整 ConnectServer 请求包 bytes.
    """
    body = bytearray(0x60)  # [0x20, 0x80) 三字段块
    body[0x40 - 0x20:0x40 - 0x20 + 16] = encode_icom_credential(username)
    body[0x50 - 0x20:0x50 - 0x20 + 16] = encode_icom_credential(password)
    raw_memo = (memo or "").encode("latin1", "ignore")[:16]
    body[0x60 - 0x20:0x60 - 0x20 + len(raw_memo)] = raw_memo
    return build_command_packet(
        inner_len=0x70, req_code=0x00,
        sender_id=field_8, receiver_id=field_C,
        inner_seq=inner_seq, local_token=local_token, rig_token=rig_token,
        seq=seq, body=bytes(body),
    )


def build_keepalive_request(
    seq: int = 1,
    field_8: int = 0,
    field_C: int = 0,
) -> bytes:
    """构造 KeepAlive 请求包 (wire 头, 无载荷)."""
    return build_command_header(
        type_cmd=CMD_KEEPALIVE, seq=seq,
        field_8=field_8, field_C=field_C,
        total_len=CMD_HEADER_SIZE, version=0,
    )


# ============================================================
# CommandClient (旧链路, PC 服务器)
# ============================================================

class CommandClient:
    """Command 信道 (UDP 50001) 登录/会话客户端.

    封装 socket, 提供 ConnectServer 认证与 KeepAlive 心跳。

    参数:
        host:     服务器 IP (运行 RemoteUty.exe 的机器)
        port:     UDP 端口 (默认 50001)
        username: 用户名 (ConnectServer 用)
        password: 密码 (ConnectServer 用; 服务器端空密码可直通)
        memo:     Memo 字段 (可选)
        timeout:  socket 默认超时 (秒), 用于读响应
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_COMMAND_PORT,
        *,
        username: str = "",
        password: str = "",
        memo: str = "",
        timeout: float = 2.0,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.memo = memo
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._seq = 1          # Command 信道 seq (BE uint16, 发送侧递增)
        self.field_8 = 0       # 登录后由响应确立的会话标识
        self.field_C = 0
        self.connected = False

    # ============================================================
    # 连接管理
    # ============================================================

    def open(self) -> None:
        """创建并绑定 UDP socket. 幂等."""
        if self._sock is not None:
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(self.timeout)

    def close(self) -> None:
        """关闭 socket. 幂等."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            finally:
                self._sock = None

    def __enter__(self) -> "CommandClient":
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    # ============================================================
    # 发送 / 接收
    # ============================================================

    def _next_seq(self) -> int:
        """Command 信道 seq (BE uint16 递增)."""
        s = self._seq & 0xFFFF
        self._seq = (self._seq + 1) & 0xFFFF
        return s

    def send(self, pkt: bytes) -> int:
        """发送原始 Command 包, 返回发送字节数."""
        if self._sock is None:
            raise CommandClientError("CommandClient 未 open")
        return self._sock.sendto(pkt, (self.host, self.port))

    def recv(self, timeout: Optional[float] = None) -> bytes:
        """接收一个 UDP 数据报 (原始 bytes).

        参数:
            timeout: 覆盖 socket 默认超时 (秒); None 用 self.timeout.

        异常:
            CommandTimeoutError - 超时无数据。
        """
        if self._sock is None:
            raise CommandClientError("CommandClient 未 open")
        old = None
        if timeout is not None:
            old = self._sock.gettimeout()
            self._sock.settimeout(timeout)
        try:
            try:
                data, _ = self._sock.recvfrom(0x1000)
                return data
            except socket.timeout:
                raise CommandTimeoutError(
                    f"接收 Command 数据超时 ({timeout if timeout is not None else self.timeout} s)"
                )
        finally:
            if old is not None:
                self._sock.settimeout(old)

    # ============================================================
    # 业务命令
    # ============================================================

    def connect(self, timeout: Optional[float] = None) -> bool:
        """发送 ConnectServer 请求并等待响应.

        认证成功 (响应 type == 0x0002) 返回 True, 并据响应建立会话标识。
        认证失败返回 False (不抛异常)。

        参数:
            timeout: 覆盖默认超时 (秒).

        异常:
            CommandTimeoutError - 超时无响应。
        """
        pkt = build_connect_request(
            self.username, self.password, self.memo,
            seq=self._next_seq(),
            field_8=self.field_8, field_C=self.field_C,
        )
        self.send(pkt)
        data = self.recv(timeout)
        resp_type, seq, f8, fc = self._parse_response(data)
        if resp_type != _resp_type(CMD_CONNECT):
            self.connected = False
            return False
        # 响应 buf[8] 为服务器回显的字节交换会话标识 (静态确证 0x41706c)
        self.field_8 = f8
        self.field_C = fc
        self.connected = True
        return True

    def keepalive(self, timeout: Optional[float] = None) -> bool:
        """发送 KeepAlive 心跳并等待响应.

        响应 type == 0x0502 视为成功。

        异常:
            CommandTimeoutError - 超时无响应。
        """
        pkt = build_keepalive_request(
            seq=self._next_seq(),
            field_8=self.field_8, field_C=self.field_C,
        )
        self.send(pkt)
        data = self.recv(timeout)
        resp_type, seq, f8, fc = self._parse_response(data)
        return resp_type == _resp_type(CMD_KEEPALIVE)

    # ============================================================
    # 响应解析
    # ============================================================

    @staticmethod
    def _parse_response(data: bytes) -> Tuple[int, int, int, int]:
        """从响应包解析 (type, seq, field_8, field_C).

        异常:
            CommandClientError - 数据不足 / 解析失败。
        """
        try:
            total_len, version, type_, seq, f8, fc = parse_command_header(data)
        except struct.error as e:
            raise CommandClientError(f"Command 响应过短: {e}") from e
        return type_, seq, f8, fc

    def __repr__(self) -> str:
        return (
            f"<CommandClient {self.host}:{self.port} "
            f"user={self.username!r} connected={self.connected} "
            f"f8=0x{self.field_8:08X} fc=0x{self.field_C:08X} "
            f"open={self._sock is not None}>"
        )