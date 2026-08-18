"""civ_response — 把 RemoteUtyCtrlRes Mailslot 原始字节解析成 CI-V 响应。

用途 (P4 闭环):
    陆墨通过 RemoteUtyCtrlCmd 发送 CI-V 命令后, RemoteUtility 会把电台应答
    写入 RemoteUtyCtrlRes Mailslot (RemoteController 未运行时由本包独占创建)。
    本模块负责把从 RemoteUtyCtrlRes 读到的"原始字节"里, 弹性地提取出
    CI-V 应答帧, 再解析成 频率 / 模式 / S-meter / PTT 等结构化值。

响应格式的弹性处理 (★★ 存疑项, 需 p4-1 真机实测确认):
    RemoteUtyCtrlRes 里一条消息的真实封装格式尚未定死, 存在几种可能:
        A. 直接是 CI-V 帧:            FE FE <from> <to> <cmd> <data> FD
        B. Mailslot 命令包内嵌帧:      <4 字节头> + <CI-V 帧>
        C. ExecCmd 风格包内嵌帧:       <20 字节头> + <CI-V 帧>
    find_civ_frame() 对所有可能统一做帧定界(找 FE..FD 帧), 不依赖外层封装,
    因此无论远端实际用哪种封装, 都能提取出 CI-V 应答帧 (前提: 帧定界符
    原样保留在 payload 里)。若真机实测发现帧定界符被剥离(只留命令体),
    需在 parse_civ_response 增加"命令体模式"分支。

参考:
    - src/rsba1/ctypes_wrappers/civ_commands.py (CI-V 帧构造/解析)
    - docs/REVERSE_PLAN.md P4; re/remoteuty/mailslot_server.md §5.4
    - 待 p4-1 probe_res_response.py 真机输出修正封装假设
"""
from __future__ import annotations

from typing import Optional, Tuple

from rsba1.ctypes_wrappers import civ_commands as civcmd

__all__ = [
    "PREAMBLE",
    "POSTAMBLE",
    "RESP_FREQ",
    "RESP_MODE",
    "RESP_SMETER",
    "RESP_PTT",
    "MODE_NAMES",
    "CivResponseError",
    "CivFrameNotFoundError",
    "find_civ_frame",
    "parse_civ_response",
    "parse_freq",
    "parse_mode",
    "parse_smeter",
    "parse_ptt",
    "parse_any",
]

PREAMBLE = civcmd.PREAMBLE    # 0xFE
POSTAMBLE = civcmd.POSTAMBLE  # 0xFD

# CI-V 应答命令码 (电台 -> 控制器)
RESP_FREQ = 0x03      # 频率应答
RESP_MODE = 0x04      # 模式应答
RESP_PTT = 0x14       # PTT 状态应答 (子命令 0x0C)
RESP_SMETER = 0x1A    # S-meter 应答 (子命令 0x03)

# IC-705 模式码 -> 名称 (常用子集, 真机可扩展)
MODE_NAMES = {
    0x01: "LSB",
    0x02: "USB",
    0x03: "AM",
    0x04: "CW",
    0x05: "RTTY",
    0x06: "FM",
    0x07: "WFM",
    0x08: "CW-R",
    0x09: "RTTY-R",
}


# ============================================================
# 异常
# ============================================================

class CivResponseError(Exception):
    """CI-V 响应解析错误 (基类)。"""


class CivFrameNotFoundError(CivResponseError):
    """在原始字节里找不到合法的 CI-V 应答帧 (FE..FD)。"""


# ============================================================
# 帧定界提取
# ============================================================

def find_civ_frame(blob: bytes) -> bytes:
    """从任意原始字节中提取第一条合法的 CI-V 帧 (FE..FD)。

    策略:
        依次扫描每个可作为帧头的位置 (byte == PREAMBLE), 从该位置起查找
        以 POSTAMBLE 结尾且长度 >= 6 (FE FE to from cmd FD) 的最短帧。
        返回第一个合法帧。未找到抛 CivFrameNotFoundError。

    参数:
        blob: RemoteUtyCtrlRes 读到的原始字节 (任意封装)。

    返回:
        完整 CI-V 帧 bytes (含 FE 前导 / FD 尾)。

    异常:
        CivFrameNotFoundError - 未找到合法帧。
    """
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError(f"blob 必须是 bytes/bytearray, 实际 {type(blob).__name__}")
    blob = bytes(blob)
    n = len(blob)
    i = 0
    while i < n:
        if blob[i] == PREAMBLE:
            # 从 i 起找 FD 结尾
            j = blob.find(POSTAMBLE, i + 1)
            if j != -1 and (j - i + 1) >= 6:
                candidate = blob[i:j + 1]
                # 校验帧结构 (至少含 to/from/cmd, 且去掉前导后仍有 3 字节地址/命令)
                try:
                    civcmd.parse_frame(candidate)
                    return candidate
                except ValueError:
                    pass  # 不是合法帧, 继续找下一个 FE
            # 若找不到 FD 或帧不合法, 继续向后找下一个 FE
            i += 1
        else:
            i += 1
    raise CivFrameNotFoundError(
        f"在 {n} 字节原始数据中未找到合法 CI-V 帧: {blob.hex()}"
    )


# ============================================================
# 通用应答解析
# ============================================================

def parse_civ_response(blob: bytes) -> Tuple[int, int, int, bytes]:
    """通用解析: 提取 CI-V 应答帧并返回 (to, from, cmd, payload)。

    返回元组与 civ_commands.parse_frame 一致:
        to     : 目标地址 (本案例为控制器地址)
        from   : 源地址 (电台地址)
        cmd    : 应答命令码
        payload: 命令后的数据 (子命令 + 数据)

    异常:
        CivFrameNotFoundError - 未找到 CI-V 帧。
    """
    frame = find_civ_frame(blob)
    return civcmd.parse_frame(frame)


# ============================================================
# 结构化解析 (频率 / 模式 / S-meter / PTT)
# ============================================================

def parse_freq(blob: bytes, to_addr: Optional[int] = None) -> int:
    """解析频率应答 -> Hz。

    期望帧: FE FE <from> <to> 0x03 <BCD 5字节> FD
    payload = [0x03 的后续数据] = BCD 频率 (5 字节)。

    参数:
        blob:     原始响应字节。
        to_addr:  可选, 期望的控制器地址 (用于校验, 不匹配不报错, 仅忽略)。

    返回:
        频率 Hz (int)。

    异常:
        CivFrameNotFoundError / ValueError - 未找到帧或数据不足。
    """
    to, frm, cmd, payload = parse_civ_response(blob)
    if cmd != RESP_FREQ:
        raise CivResponseError(
            f"期望频率应答 cmd=0x{RESP_FREQ:02X}, 实际 cmd=0x{cmd:02X}"
        )
    if len(payload) < 5:
        raise CivResponseError(
            f"频率应答数据不足: 期望 5 字节 BCD, 实际 {len(payload)} 字节"
        )
    return civcmd.bytes_to_freq(payload[:5])


def parse_mode(blob: bytes, to_addr: Optional[int] = None) -> Tuple[int, int]:
    """解析模式应答 -> (mode_code, filter)。

    期望帧: FE FE <from> <to> 0x04 <mode> <filter> FD
    payload = [mode, filter]。

    返回:
        (mode_code, filter): mode_code 见 MODE_NAMES, 未知码保留原值。

    异常:
        CivFrameNotFoundError / ValueError - 未找到帧或数据不足。
    """
    to, frm, cmd, payload = parse_civ_response(blob)
    if cmd != RESP_MODE:
        raise CivResponseError(
            f"期望模式应答 cmd=0x{RESP_MODE:02X}, 实际 cmd=0x{cmd:02X}"
        )
    if len(payload) < 2:
        raise CivResponseError(
            f"模式应答数据不足: 期望 2 字节, 实际 {len(payload)} 字节"
        )
    return payload[0], payload[1]


def parse_smeter(blob: bytes, to_addr: Optional[int] = None) -> int:
    """解析 S-meter 应答 (原始数据字节)。

    期望帧: FE FE <from> <to> 0x1A 0x03 <data> FD
    payload = [0x03, data...] (子命令 0x03 + 数据)。

    返回:
        S-meter 原始数据字节 (int)。真机若用 S 表 (S表: 信号强度->dB 显示),
        需额外查表换算, 见 re/remoteuty/exec_cmd_subcmd.md 的 S 表章节。

    异常:
        CivFrameNotFoundError / ValueError - 未找到帧或数据不足。
    """
    to, frm, cmd, payload = parse_civ_response(blob)
    if cmd != RESP_SMETER:
        raise CivResponseError(
            f"期望 S-meter 应答 cmd=0x{RESP_SMETER:02X}, 实际 cmd=0x{cmd:02X}"
        )
    if len(payload) < 2 or payload[0] != 0x03:
        raise CivResponseError(f"S-meter 应答格式异常: payload={payload.hex()}")
    return payload[1]


def parse_ptt(blob: bytes, to_addr: Optional[int] = None) -> bool:
    """解析 PTT 状态应答 -> bool (True=TX, False=RX)。

    期望帧: FE FE <from> <to> 0x14 0x0C <data> FD
    payload = [0x0C, data] (子命令 0x0C + 状态)。

    返回:
        True 表示 PTT 按下 (TX), False 表示 RX。

    异常:
        CivFrameNotFoundError / ValueError - 未找到帧或数据不足。
    """
    to, frm, cmd, payload = parse_civ_response(blob)
    if cmd != RESP_PTT:
        raise CivResponseError(
            f"期望 PTT 应答 cmd=0x{RESP_PTT:02X}, 实际 cmd=0x{cmd:02X}"
        )
    if len(payload) < 2:
        raise CivResponseError(
            f"PTT 应答数据不足: 期望 2 字节, 实际 {len(payload)} 字节"
        )
    return payload[1] != 0


# ============================================================
# 通用分派 (按应答 cmd 自动路由)
# ============================================================

def parse_any(blob: bytes) -> dict:
    """按应答命令码自动分派到具体解析器, 返回 dict。

    返回:
        {
            "cmd":  int 应答命令码,
            "to":   int 目标地址,
            "from": int 源地址,
            "kind": str 类型 ("freq"/"mode"/"smeter"/"ptt"/"unknown"),
            "value": 解析结果 (类型见对应解析器; unknown 时为原始 payload),
        }

    异常:
        CivFrameNotFoundError - 未找到 CI-V 帧。
    """
    to, frm, cmd, payload = parse_civ_response(blob)
    if cmd == RESP_FREQ:
        return {"cmd": cmd, "to": to, "from": frm, "kind": "freq",
                "value": civcmd.bytes_to_freq(payload[:5])}
    if cmd == RESP_MODE:
        return {"cmd": cmd, "to": to, "from": frm, "kind": "mode",
                "value": (payload[0], payload[1])}
    if cmd == RESP_SMETER:
        return {"cmd": cmd, "to": to, "from": frm, "kind": "smeter",
                "value": payload[1] if len(payload) >= 2 and payload[0] == 0x03 else payload}
    if cmd == RESP_PTT:
        return {"cmd": cmd, "to": to, "from": frm, "kind": "ptt",
                "value": payload[1] != 0 if len(payload) >= 2 else None}
    return {"cmd": cmd, "to": to, "from": frm, "kind": "unknown", "value": payload}