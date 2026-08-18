"""CI-V 协议帧构造/解析 + 频率 BCD 编解码。

CI-V (Computer Interface V) 是 Icom 电台的串口控制协议。标准帧格式:

    +------+------+--------+----------+-------------+------+
    | 0xFE | 0xFE | toAddr | fromAddr | cmd[+data]  | 0xFD |
    +------+------+--------+----------+-------------+------+
     前导   前导   目标地址  源地址     命令体        结束

参考: phase2-re/notes/pe_analysis/CivCtrl_deep_analysis.md §6 (CI-V 协议实现)。
      CivCtrl.dll 不预存任何帧模板, 帧在 civSendSub @ 0x402360 运行时拼装:
      [0xFE * (preambleCount+2)] + [toAddr, fromAddr, cmd, ...] + [0xFD]。

civSend 契约:
    civSend(handle, data, flag) 的 data 应为 [toAddr, fromAddr, cmd, ...]
    (不含 FE 前导 / FD 尾, DLL 自动包装)。可用 build_frame(...)[2:-1] 取得。
"""

from __future__ import annotations

__all__ = [
    # 地址常量
    "PREAMBLE",
    "POSTAMBLE",
    "PREAMBLE_COUNT",
    "IC705_TO_ADDR",
    "IC7300_TO_ADDR",
    "FROM_ADDR",
    "DEFAULT_FROM_ADDR",
    # 命令常量
    "CMD_READ_FREQ",
    "CMD_READ_MODE",
    "CMD_SET_MODE",
    "CMD_SET_FREQ",
    "CMD_SET_VFO",
    "CMD_READ_PTT",
    "CMD_PTT",
    "CMD_PTT_SUB",
    "CMD_PTT_ON",
    "CMD_PTT_OFF",
    "CMD_READ_SMETER",
    # 函数
    "build_frame",
    "parse_frame",
    "bytes_to_freq",
    "freq_to_bytes",
    "cmd_const_to_bytes",
    "ptt_on_bytes",
    "ptt_off_bytes",
    "read_freq_bytes",
    "set_freq_bytes",
    # 频段白名单
    "AMATEUR_BANDS",
    "is_allowed_freq",
    "assert_allowed_freq",
]


# ============================================================
# CI-V 帧定界符与地址常量
# ============================================================

PREAMBLE = 0xFE          # 前导字节 (重复 N 次, 标准 N=2)
POSTAMBLE = 0xFD         # 帧结束字节
PREAMBLE_COUNT = 2       # 标准前导字节数 (CivCtrl preambleCount=0 时实际写 2)

# 常见电台 CI-V 目标地址
IC705_TO_ADDR = 0xA4     # IC-705
IC7300_TO_ADDR = 0x04    # IC-7300

# 源地址 (控制器)。传统 CI-V 控制器地址 0xE0; RS-BA1 V2 中常用 0x00。
FROM_ADDR = 0x00
DEFAULT_FROM_ADDR = 0x00


# ============================================================
# CI-V 命令常量
# ============================================================

# 单字节命令 (无子命令)
CMD_READ_FREQ = 0x03      # 读频率
CMD_READ_MODE = 0x04      # 读模式
CMD_SET_FREQ = 0x05       # 设频率 (VFO)  ⚠️ 2026-08-18 真机修正: 原与 CMD_SET_MODE 写反,
CMD_SET_MODE = 0x06       # 设模式          ICOM CI-V 标准为 0x05=频率 / 0x06=模式
CMD_SET_VFO = 0x0C        # 设 VFO (A/B/M)

# 带子命令的命令
CMD_READ_PTT = 0x14       # 读 PTT 状态 (子命令 0x0C)
CMD_PTT = 0x1C            # PTT 控制 (子命令 0x00)
CMD_PTT_SUB = 0x00        # PTT 子命令
CMD_READ_SMETER = 0x1A    # 读 S-meter (子命令 0x03)

# PTT 命令 16 位标识 (cmd<<8 | data, 子命令 0x00 隐含)
# 注: 实际 CI-V PTT 帧体为 [0x1C, 0x00, 0x01](TX) / [0x1C, 0x00, 0x00](RX)
CMD_PTT_ON = 0x1C00       # PTT ON 标识 (按 task 规范)
CMD_PTT_OFF = 0x1C01      # PTT OFF 标识 (按 task 规范)


# ============================================================
# 帧构造 / 解析
# ============================================================

def build_frame(to_addr, from_addr, cmd_bytes, preamble_count=PREAMBLE_COUNT):
    """构造完整 CI-V 帧: [FE * preamble_count][to][from][cmd_bytes...][FD]。

    参数:
        to_addr:        目标电台地址 (如 IC705_TO_ADDR=0xA4)。
        from_addr:      源控制器地址 (通常 0x00 或 0xE0)。
        cmd_bytes:      命令体 bytes (cmd [+ subcmd] [+ data]), 不含地址/FE/FD。
        preamble_count: 前导 0xFE 字节数 (标准 2)。

    返回:
        完整 CI-V 帧 bytes。

    注:
        civSend 需要 [to, from, cmd_bytes] (不含 FE/FD), 可用 build_frame(...)[2:-1]。
    """
    if not isinstance(cmd_bytes, (bytes, bytearray)):
        raise TypeError(f"cmd_bytes 必须是 bytes, 实际 {type(cmd_bytes).__name__}")
    if not (0 <= to_addr <= 0xFF) or not (0 <= from_addr <= 0xFF):
        raise ValueError("地址必须在 0x00-0xFF 范围内")
    pre = bytes([PREAMBLE]) * int(preamble_count)
    return pre + bytes([to_addr & 0xFF, from_addr & 0xFF]) + bytes(cmd_bytes) + bytes([POSTAMBLE])


def parse_frame(data):
    """解析 CI-V 帧 -> (to_addr, from_addr, cmd, payload)。

    参数:
        data: 完整 CI-V 帧 bytes (含 FE 前导 / FD 尾)。

    返回:
        (to_addr, from_addr, cmd, payload):
            to_addr  : 目标地址 (int)
            from_addr: 源地址 (int)
            cmd      : 命令字节 (int, 地址后第一个字节)
            payload  : 命令体剩余 bytes (子命令 + 数据, 不含 cmd / FE / FD)

    异常:
        ValueError - 帧格式错误 (长度不足/无前导/无尾/地址段缺失)。
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"data 必须是 bytes, 实际 {type(data).__name__}")
    data = bytes(data)
    if len(data) < 6:
        raise ValueError(f"帧过短 ({len(data)} 字节), 至少需要 6 (FE FE to from cmd FD)")
    if data[0] != PREAMBLE:
        raise ValueError(f"帧头非 0xFE: 0x{data[0]:02X}")
    if data[-1] != POSTAMBLE:
        raise ValueError(f"帧尾非 0xFD: 0x{data[-1]:02X}")
    # 去掉尾 FD, 再跳过所有前导 FE
    body = data[:-1]
    i = 0
    while i < len(body) and body[i] == PREAMBLE:
        i += 1
    if len(body) - i < 3:
        raise ValueError(f"地址/命令段缺失: 前导后仅 {len(body) - i} 字节")
    to_addr = body[i]
    from_addr = body[i + 1]
    cmd = body[i + 2]
    payload = body[i + 3:]
    return to_addr, from_addr, cmd, payload


# ============================================================
# 频率 BCD 编解码
# ============================================================

def bytes_to_freq(bcd_bytes):
    """CI-V BCD 频率字节 -> Hz (int)。

    CI-V 频率为 5 字节 (10 BCD 位), 字节序 LSB-first (低位字节在前),
    每字节高低半字节各 1 个 BCD 位。

    例: b"\\x00\\x00\\x70\\x42\\x01" -> 14270000 (14.270 MHz)。

    参数:
        bcd_bytes: BCD 频率字节 (标准 5 字节, 也支持其他长度)。

    返回:
        频率 Hz (int)。

    异常:
        ValueError - 字节为空或含非 BCD 半字节 (>9)。
    """
    if not isinstance(bcd_bytes, (bytes, bytearray)):
        raise TypeError(f"bcd_bytes 必须是 bytes, 实际 {type(bcd_bytes).__name__}")
    if len(bcd_bytes) == 0:
        raise ValueError("bcd_bytes 为空")
    # CI-V LSB-first -> 反转为 MSB-first 再解码
    msb_first = bytes(reversed(bcd_bytes))
    for b in msb_first:
        hi, lo = (b >> 4) & 0xF, b & 0xF
        if hi > 9 or lo > 9:
            raise ValueError(f"无效 BCD 字节 0x{b:02X} (半字节 > 9)")
    digits = "".join(f"{b:02x}" for b in msb_first)
    return int(digits)


def freq_to_bytes(hz, num_bytes=5):
    """Hz (int) -> CI-V BCD 频率字节 (LSB-first)。

    参数:
        hz:        频率 (Hz), 非负整数。
        num_bytes: 输出字节数 (标准 5 = 10 BCD 位)。

    返回:
        BCD 频率字节 (LSB-first)。

    例: freq_to_bytes(14270000) -> b"\\x00\\x00\\x70\\x42\\x01"。

    异常:
        ValueError - hz 为负或超出 num_bytes 能表示的范围。
    """
    hz = int(hz)
    if hz < 0:
        raise ValueError(f"频率不能为负: {hz}")
    max_freq = 10 ** (num_bytes * 2) - 1
    if hz > max_freq:
        raise ValueError(f"频率 {hz} 超出 {num_bytes} 字节 BCD 范围 (max {max_freq})")
    s = str(hz).zfill(num_bytes * 2)
    # 每 2 位 BCD 组成 1 字节, MSB-first
    msb_first = bytes(int(s[i:i + 2], 16) for i in range(0, len(s), 2))
    # CI-V 发送 LSB-first
    return bytes(reversed(msb_first))


# ============================================================
# 命令体构造辅助
# ============================================================

def cmd_const_to_bytes(cmd_const):
    """16 位命令常量 -> bytes (大端, 高字节在前)。

    用于 CMD_PTT_ON=0x1C00 -> b"\\x1C\\x00" 等。
    """
    if not (0 <= cmd_const <= 0xFFFF):
        raise ValueError(f"cmd_const 超出 16 位范围: {cmd_const}")
    return bytes([(cmd_const >> 8) & 0xFF, cmd_const & 0xFF])


def ptt_on_bytes():
    """PTT ON (TX) 命令体: 0x1C 0x00 0x01。"""
    return bytes([CMD_PTT, CMD_PTT_SUB, 0x01])


def ptt_off_bytes():
    """PTT OFF (RX) 命令体: 0x1C 0x00 0x00。"""
    return bytes([CMD_PTT, CMD_PTT_SUB, 0x00])


def read_freq_bytes():
    """读频率命令体: 0x03。"""
    return bytes([CMD_READ_FREQ])


def set_freq_bytes(hz):
    """设频率命令体: 0x06 + 5 字节 BCD 频率。"""
    return bytes([CMD_SET_FREQ]) + freq_to_bytes(hz)


# ============================================================
# 频段白名单 (安全约束)
# ============================================================

# 业余频段范围 (Hz), 闭区间 [lo, hi]。见 user_profile 安全约束:
#     HF 1.8-30 MHz, 50 MHz 波段 50-54 MHz, 2m 波段 144-148 MHz。
AMATEUR_BANDS = (
    (1_800_000, 30_000_000),    # 160m - 10m
    (50_000_000, 54_000_000),   # 6m
    (144_000_000, 148_000_000), # 2m
)


def is_allowed_freq(hz):
    """判断频率是否落在业余频段白名单内。

    参数:
        hz: 频率 (Hz, int)。

    返回:
        bool - True 表示可安全设置, False 表示超出业余频段。
    """
    hz = int(hz)
    return any(lo <= hz <= hi for lo, hi in AMATEUR_BANDS)


def assert_allowed_freq(hz):
    """断言频率在业余频段白名单内, 否则抛 ValueError。

    用于设频率命令派发前拦截越界频率, 防止误设到业余频段之外。

    参数:
        hz: 频率 (Hz, int)。

    异常:
        ValueError - 频率不在白名单内 (负值 / 越界)。
    """
    hz = int(hz)
    if not is_allowed_freq(hz):
        raise ValueError(f"频率 {hz} Hz 不在业余频段白名单内: {AMATEUR_BANDS}")