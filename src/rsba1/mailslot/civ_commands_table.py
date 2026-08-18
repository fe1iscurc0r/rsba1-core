"""civ_commands_table — IC-705 CI-V 命令字节映射表 + 帧构造器。

集中定义 IC-705 (以及通用 Icom 电台) 的 CI-V 命令字节、模式编码、
业余频段白名单, 并复用 civ_commands.py 的 `build_frame` / `freq_to_bytes`
构造完整 CI-V 帧 (FE FE to from cmd... FD)。

设计:
    - 高效复用 src/rsba1/ctypes_wrappers/civ_commands.py 的帧构造与 BCD 编解码,
      不重复实现底层协议。
    - 频率设置强制白名单校验 (业余频段), 防止误把电台设到非法频率。
    - 模式名 -> 字节的正 / 反向映射 (MODE_CODE / MODE_CODE_NAME)。

命令行工具 (send_civ_command.py) 与高层 API (civ_via_execcmd.py) 发送的
只是"命令体" [to, from, cmd...]; 本模块的 build_*_frame 提供完整帧 (含 FE/FD),
用于展示 / 调试 / 测试。

关键 IC-705 常量:
    to_addr   = 0xA4 (IC-705)
    from_addr = 0x00 (控制器, V2 常用; 传统 0xE0)

参考:
    - src/rsba1/ctypes_wrappers/civ_commands.py (帧 / BCD 实现)
    - d:\\my git\\RS-BA1\\RemoteController\\models\\IC-705.ini (CMD 表)
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from rsba1.ctypes_wrappers import civ_commands as civcmd

__all__ = [
    "IC705_TO_ADDR",
    "DEFAULT_FROM_ADDR",
    "MODE_CODE",
    "MODE_CODE_NAME",
    "COMMAND_TABLE",
    "COMMAND_NAMES",
    "AMATEUR_BANDS",
    "mode_name_to_code",
    "is_allowed_freq",
    "assert_allowed_freq",
    "build_freq_frame",
    "build_mode_frame",
    "build_read_freq_frame",
    "build_read_mode_frame",
    "build_read_smeter_frame",
    "build_ptt_frame",
]


# ============================================================
# 地址常量
# ============================================================

IC705_TO_ADDR = civcmd.IC705_TO_ADDR        # 0xA4
DEFAULT_FROM_ADDR = civcmd.DEFAULT_FROM_ADDR  # 0x00


# ============================================================
# 模式编码 (CI-V 设模式命令 cmd=0x05 的 mode 字节)
# ============================================================

MODE_CODE: Dict[str, int] = {
    "LSB": 0x00,
    "USB": 0x01,
    "AM": 0x02,
    "CW": 0x03,
    "RTTY": 0x04,
    "FM": 0x05,
    "WFM": 0x06,
    "CW-R": 0x07,
    "RTTY-R": 0x08,
    "DV": 0x11,
}

# 反向映射: 字节 -> 模式名
MODE_CODE_NAME: Dict[int, str] = {v: k for k, v in MODE_CODE.items()}


def mode_name_to_code(name: str) -> int:
    """模式字符串 -> CI-V mode 字节 (大小写 / 首尾空格不敏感)。

    参数:
        name: 模式名, 如 "USB"、"usb"、" fm "。

    返回:
        CI-V mode 字节 (int)。

    异常:
        KeyError - 未知模式名。
    """
    key = str(name).strip().upper()
    return MODE_CODE[key]


# ============================================================
# CI-V 命令表
# ============================================================
# 值 = (命令体 bytes, 描述)。命令体不含 to/from 地址与 FE/FD 定界符。
# SET_FREQ / SET_MODE 为动态命令, 表内仅存命令前缀字节, 数据由构造器追加。
# ============================================================

COMMAND_TABLE: Dict[str, Tuple[bytes, str]] = {
    "READ_FREQ": (b"\x03", "读频率"),
    "READ_MODE": (b"\x04", "读模式"),
    "SET_FREQ": (bytes([civcmd.CMD_SET_FREQ]), "设频率 (VFO, 后接 5 字节 BCD)"),
    "SET_MODE": (bytes([civcmd.CMD_SET_MODE]), "设模式 (后接 mode + filter)"),
    "READ_SMETER": (bytes([civcmd.CMD_READ_SMETER, 0x03]), "读 S-meter"),
    "PTT_ON": (bytes([civcmd.CMD_PTT, civcmd.CMD_PTT_SUB, 0x01]), "PTT 开 (TX)"),
    "PTT_OFF": (bytes([civcmd.CMD_PTT, civcmd.CMD_PTT_SUB, 0x00]), "PTT 关 (RX)"),
}

COMMAND_NAMES: List[str] = list(COMMAND_TABLE)


# ============================================================
# 业余频段白名单 (频率设置安全)
# ============================================================
# 防止误把电台设到非法频段。仅放行业余业务可用的连续频段:
#   1.8-30 MHz (HF), 50-54 MHz (6m), 144-148 MHz (2m)。
# ============================================================

AMATEUR_BANDS: List[Tuple[int, int]] = [
    (1_800_000, 30_000_000),    # 1.8-30 MHz HF
    (50_000_000, 54_000_000),   # 50-54 MHz 6m
    (144_000_000, 148_000_000),  # 144-148 MHz 2m
]


def is_allowed_freq(hz: int) -> bool:
    """判断频率 (Hz) 是否在业余频段白名单内。"""
    return any(lo <= hz <= hi for lo, hi in AMATEUR_BANDS)


def assert_allowed_freq(hz: int) -> None:
    """校验频率在业余频段白名单内, 否则抛 ValueError。"""
    if not is_allowed_freq(hz):
        raise ValueError(
            f"频率 {hz} Hz 不在业余频段白名单内: {AMATEUR_BANDS}"
        )


# ============================================================
# 完整 CI-V 帧构造器 (FE FE to from cmd... FD)
# ============================================================

def build_freq_frame(
    freq_hz: int,
    to_addr: int = IC705_TO_ADDR,
    from_addr: int = DEFAULT_FROM_ADDR,
) -> bytes:
    """设频率完整帧: FE FE to from 06 <BCD 5 字节> FD。

    命令字节 0x06 = CMD_SET_FREQ (VFO), 频率用 5 字节 LSB-first BCD 编码。

    参数:
        freq_hz: 频率 (Hz), 如 14270000 = 14.270 MHz。必须位于业余频段白名单。

    返回:
        完整 CI-V 帧 bytes。

    异常:
        ValueError - 频率不在业余频段白名单内。
    """
    assert_allowed_freq(freq_hz)
    cmd_bytes = civcmd.set_freq_bytes(freq_hz)  # [0x06] + 5 字节 BCD
    return civcmd.build_frame(to_addr, from_addr, cmd_bytes)


def build_mode_frame(
    mode: str,
    width: int = 0,
    to_addr: int = IC705_TO_ADDR,
    from_addr: int = DEFAULT_FROM_ADDR,
) -> bytes:
    """设模式完整帧: FE FE to from 05 <mode> <width> FD。

    命令字节 0x05 = CMD_SET_MODE。

    参数:
        mode:  模式名 (LSB/USB/AM/FM/CW...), 见 MODE_CODE。
        width: 滤波器带宽字节 (0 表示默认)。

    返回:
        完整 CI-V 帧 bytes。

    异常:
        KeyError - 未知模式名。
    """
    cmd_bytes = bytes([
        civcmd.CMD_SET_MODE,
        mode_name_to_code(mode),
        width & 0xFF,
    ])
    return civcmd.build_frame(to_addr, from_addr, cmd_bytes)


def build_read_freq_frame(
    to_addr: int = IC705_TO_ADDR,
    from_addr: int = DEFAULT_FROM_ADDR,
) -> bytes:
    """读频率完整帧: FE FE to from 03 FD。"""
    return civcmd.build_frame(to_addr, from_addr, civcmd.read_freq_bytes())


def build_read_mode_frame(
    to_addr: int = IC705_TO_ADDR,
    from_addr: int = DEFAULT_FROM_ADDR,
) -> bytes:
    """读模式完整帧: FE FE to from 04 FD。"""
    return civcmd.build_frame(to_addr, from_addr, bytes([civcmd.CMD_READ_MODE]))


def build_read_smeter_frame(
    to_addr: int = IC705_TO_ADDR,
    from_addr: int = DEFAULT_FROM_ADDR,
) -> bytes:
    """读 S-meter 完整帧: FE FE to from 1A 03 FD。"""
    return civcmd.build_frame(
        to_addr, from_addr, bytes([civcmd.CMD_READ_SMETER, 0x03])
    )


def build_ptt_frame(
    tx: bool = True,
    to_addr: int = IC705_TO_ADDR,
    from_addr: int = DEFAULT_FROM_ADDR,
) -> bytes:
    """PTT 完整帧: on = ...1C 00 01, off = ...1C 00 00。

    参数:
        tx: True 发 PTT ON (TX), False 发 PTT OFF (RX)。
    """
    cmd_bytes = civcmd.ptt_on_bytes() if tx else civcmd.ptt_off_bytes()
    return civcmd.build_frame(to_addr, from_addr, cmd_bytes)