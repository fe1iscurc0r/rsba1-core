"""CivCtrl 演示脚本。

典型流程 (默认 --cmd full):
    连接 COM3 -> 设地址 0xA4 (IC-705) -> 读频率 -> 设频率 14.270 MHz
    -> PTT ON -> 等 1s -> PTT OFF -> 关闭

命令行参数:
    --com       COM 端口号 (默认 3 = COM3)
    --baud      波特率 (默认 9600)
    --to-addr   目标电台 CI-V 地址 (默认 0xA4 = IC-705)
    --from-addr 源控制器地址 (默认 0x00)
    --cmd       单步命令: read_freq / set_freq / ptt_on / ptt_off / full
    --freq      set_freq 用的频率 Hz (默认 14270000 = 14.270 MHz)
    --timeout   send_and_wait 超时 ms (默认 1000)
    --dll-path  CivCtrl.dll 路径 (默认内置路径)

注意: 需真实电台 + COM 口才能成功; 无硬件时 civOpen 会失败并打印详细错误。
"""

from __future__ import annotations

import sys
import os
import time
import argparse
import traceback

# 把 src 加入 sys.path (脚本可独立运行)
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")
_SRC = os.path.abspath(_SRC)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from rsba1.ctypes_wrappers.civctrl import (
    CivCtrlDLL,
    CivCtrlError,
    CivCtrlLoadError,
    CivCtrlStateError,
    CivCtrlTimeoutError,
    DEFAULT_CIVCTRL_DLL_PATH,
)
from rsba1.ctypes_wrappers import civ_commands as civcmd


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="CivCtrl.dll CI-V 控制演示",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--com", type=int, default=3, help="COM 端口号 (默认 3 = COM3)")
    p.add_argument("--baud", type=int, default=9600, help="波特率 (默认 9600)")
    p.add_argument("--to-addr", type=lambda x: int(x, 0), default=0xA4,
                   help="目标电台 CI-V 地址 (默认 0xA4 = IC-705)")
    p.add_argument("--from-addr", type=lambda x: int(x, 0), default=civcmd.FROM_ADDR,
                   help="源控制器地址 (默认 0x00)")
    p.add_argument("--cmd", choices=["read_freq", "set_freq", "ptt_on", "ptt_off", "full"],
                   default="full", help="执行的命令 (默认 full 全流程)")
    p.add_argument("--freq", type=int, default=14270000,
                   help="set_freq 频率 Hz (默认 14270000 = 14.270 MHz)")
    p.add_argument("--timeout", type=int, default=1000,
                   help="send_and_wait 超时 ms (默认 1000)")
    p.add_argument("--dll-path", default=DEFAULT_CIVCTRL_DLL_PATH,
                   help="CivCtrl.dll 路径")
    return p.parse_args(argv)


def _send_body(to_addr, from_addr, cmd_bytes):
    """构造 civSend 所需的命令体 [to, from, cmd...] (不含 FE/FD)。"""
    return bytes([to_addr & 0xFF, from_addr & 0xFF]) + bytes(cmd_bytes)


def _hex(b):
    return b.hex(" ")


def do_read_freq(civ, handle, to_addr, from_addr, timeout):
    """读频率: 发 0x03, 解析 BCD 应答。"""
    print(f"[read_freq] 发送读频率命令 (to=0x{to_addr:02X})...")
    body = _send_body(to_addr, from_addr, civcmd.read_freq_bytes())
    payload, flag = civ.send_and_wait(handle, body, timeout_ms=timeout)
    print(f"[read_freq] 应答 payload ({len(payload)} B): {_hex(payload)}  flag={flag}")
    # payload 布局 (不含 FE/FD): [to, from, cmd, bcd_freq(5)]
    if len(payload) < 8:
        print(f"[read_freq] 警告: 应答过短, 无法解析频率 (期望 >=8 字节, 实际 {len(payload)})")
        return
    resp_to, resp_from, resp_cmd = payload[0], payload[1], payload[2]
    bcd = payload[3:8]
    try:
        freq_hz = civcmd.bytes_to_freq(bcd)
    except ValueError as e:
        print(f"[read_freq] BCD 解码失败: {e}")
        return
    print(f"[read_freq] 应答: to=0x{resp_to:02X} from=0x{resp_from:02X} cmd=0x{resp_cmd:02X}")
    print(f"[read_freq] 当前频率: {freq_hz} Hz ({freq_hz / 1e6:.6f} MHz)")


def do_set_freq(civ, handle, to_addr, from_addr, freq_hz, timeout):
    """设频率: 发 0x06 + 5 字节 BCD。"""
    print(f"[set_freq] 设频率 = {freq_hz} Hz ({freq_hz / 1e6:.6f} MHz)...")
    cmd_bytes = civcmd.set_freq_bytes(freq_hz)
    body = _send_body(to_addr, from_addr, cmd_bytes)
    print(f"[set_freq] 命令体: {_hex(body)}")
    civ.send_and_wait(handle, body, timeout_ms=timeout)
    print(f"[set_freq] 完成 (电台应已确认)")


def do_ptt(civ, handle, to_addr, from_addr, on, timeout):
    """PTT ON/OFF: 发 0x1C 0x00 0x01/0x00。"""
    label = "ON (TX)" if on else "OFF (RX)"
    cmd_bytes = civcmd.ptt_on_bytes() if on else civcmd.ptt_off_bytes()
    print(f"[ptt] PTT {label}...")
    body = _send_body(to_addr, from_addr, cmd_bytes)
    civ.send_and_wait(handle, body, timeout_ms=timeout)
    print(f"[ptt] PTT {label} 完成")


def run_full(civ, handle, to_addr, from_addr, freq_hz, timeout):
    """完整演示流程: 读频率 -> 设频率 -> PTT ON -> 等 1s -> PTT OFF。"""
    print("=" * 60)
    print(f"完整流程: COM 已打开, to=0x{to_addr:02X}, from=0x{from_addr:02X}")
    print("=" * 60)

    # 1. 读当前频率
    try:
        do_read_freq(civ, handle, to_addr, from_addr, timeout)
    except CivCtrlTimeoutError:
        print("[read_freq] 超时 (电台可能未连接或地址不对), 跳过")

    # 2. 设频率 14.270 MHz
    do_set_freq(civ, handle, to_addr, from_addr, freq_hz, timeout)

    # 3. PTT ON
    do_ptt(civ, handle, to_addr, from_addr, on=True, timeout=timeout)

    # 4. 等 1s
    print("[full] 等待 1s (模拟发射)...")
    time.sleep(1.0)

    # 5. PTT OFF
    do_ptt(civ, handle, to_addr, from_addr, on=False, timeout=timeout)

    print("=" * 60)
    print("完整流程结束")
    print("=" * 60)


def main(argv=None):
    args = parse_args(argv)
    print(f"CivCtrl 演示 | COM{args.com} @ {args.baud} baud | "
          f"to=0x{args.to_addr:02X} from=0x{args.from_addr:02X} | cmd={args.cmd}")
    print(f"DLL: {args.dll_path}")

    # 1. 加载 DLL
    try:
        civ = CivCtrlDLL(args.dll_path)
    except CivCtrlLoadError as e:
        print(f"[错误] DLL 加载失败: {e}", file=sys.stderr)
        print("提示: 检查 --dll-path 是否指向有效的 CivCtrl.dll", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[错误] 加载 DLL 时发生未预期异常:", file=sys.stderr)
        traceback.print_exc()
        return 2

    # 2. 打开 COM + 执行命令 (确保 civClose)
    handle = None
    try:
        with civ:
            print(f"[open] civOpen(COM{args.com}, {args.baud}, dtr=0, rts=0)...")
            handle = civ.civOpen(args.com, args.baud, 0, 0)
            print(f"[open] 成功, handle={handle}")

            civ.civSetAddress(handle, args.to_addr, args.from_addr)
            print(f"[addr] 已设 to=0x{args.to_addr:02X} from=0x{args.from_addr:02X}")

            if args.cmd == "read_freq":
                do_read_freq(civ, handle, args.to_addr, args.from_addr, args.timeout)
            elif args.cmd == "set_freq":
                do_set_freq(civ, handle, args.to_addr, args.from_addr, args.freq, args.timeout)
            elif args.cmd == "ptt_on":
                do_ptt(civ, handle, args.to_addr, args.from_addr, on=True, timeout=args.timeout)
            elif args.cmd == "ptt_off":
                do_ptt(civ, handle, args.to_addr, args.from_addr, on=False, timeout=args.timeout)
            elif args.cmd == "full":
                run_full(civ, handle, args.to_addr, args.from_addr, args.freq, args.timeout)
            else:
                print(f"[错误] 未知命令: {args.cmd}", file=sys.stderr)
                return 2
    except CivCtrlError as e:
        print(f"[错误] CivCtrl 操作失败: {e}", file=sys.stderr)
        print("常见原因: COM 端口被占用 / 电台未连接 / CI-V 地址错误 / 状态机忙",
              file=sys.stderr)
        return 1
    except CivCtrlStateError as e:
        print(f"[错误] 状态机非 IDLE, 无法发送: {e}", file=sys.stderr)
        return 1
    except CivCtrlTimeoutError as e:
        print(f"[错误] 接收超时: {e}", file=sys.stderr)
        print("提示: 增大 --timeout 或检查电台连接/CI-V 波特率", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[错误] 未预期异常:", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        # 上下文管理器已自动 civClose(self._handle); 此处仅提示
        if handle is not None:
            print(f"[close] 已关闭 handle={handle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())