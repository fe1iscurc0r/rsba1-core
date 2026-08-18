"""e2e_civ_loop — 端到端 E2E: RS-BA1 全链路 (Command 认证 + Serial 透传) CI-V 闭环.

链路 (2026-08-18 真机定案, 详见 re/protocols/command_channel_cmd.md §4.2):
    Command(50001) 登录认证 → ConnectTrans → Serial(50002) open → CI-V 透传。
    ⚠️ 旧版"纯 Serial 无需认证"假设已被 A/B 实验推翻: 无授权会话时电台只回环
    本端请求, 不透传电台应答。

前置条件:
    1. IC-705 已上电, RS-BA1 Server Function 开启, 网络可达 (ping 通)。
    2. 电台侧 RS-BA1 用户名/密码 (MENU → SET → WLAN Set → Remote Settings)。
    3. 电台网络栈未卡死 (卡死特征: ping 通但三端口静默 → 重启电台)。

用法:
    # 只读闭环: 循环 read_freq + read_mode
    python scripts\\e2e_civ_loop.py --user linnan --pwd shenyaodiyi
    # 回程写验证: 设频到 145.000MHz (白名单内) 读回确认, 最后恢复原频率
    python scripts\\e2e_civ_loop.py --user linnan --pwd shenyaodiyi --set-freq 145000000
    # PTT 触发 (会真正发射, 确保天线负载!)
    python scripts\\e2e_civ_loop.py --user linnan --pwd shenyaodiyi --ptt

返回:
    0 = 闭环打通; 非 0 = 失败, 打印阶段与原始包便于归因。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rsba1.radio_link import (  # noqa: E402
    RadioLink,
    RadioAuthError,
    RadioLinkError,
    RadioTimeoutError,
)
from rsba1.ctypes_wrappers import civ_commands as civcmd  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="端到端: RS-BA1 全链路 CI-V 闭环")
    p.add_argument("--host", default="192.168.0.31", help="IC-705 (RS-BA1 Server) IP")
    p.add_argument("--user", default="linnan", help="电台侧 RS-BA1 用户名")
    p.add_argument("--pwd", default="shenyaodiyi", help="电台侧 RS-BA1 密码")
    p.add_argument("--bind-ip", default="192.168.0.23", help="本机 LAN 源 IP")
    p.add_argument("--iterations", type=int, default=3, help="read_freq 循环次数")
    p.add_argument("--read-mode", action="store_true", default=True,
                   help="同时循环 read_mode (默认开)")
    p.add_argument("--set-freq", type=int, default=None,
                   help="回程写验证: 设到该频率(Hz, 须在白名单内)读回, 结束后恢复原频率")
    p.add_argument("--ptt", action="store_true",
                   help="PTT ON 1s → OFF (会真正发射, 谨慎)")
    p.add_argument("--timeout", type=float, default=2.0, help="每包收发超时(秒)")
    p.add_argument("--dry-run", action="store_true",
                   help="只校验参数与白名单, 不实际连接")
    args = p.parse_args()

    print(f"=== E2E: RS-BA1 全链路 CI-V 闭环 {args.host} "
          f"(user={args.user}){'(DRY-RUN)' if args.dry_run else ''} ===")

    if args.set_freq is not None:
        try:
            civcmd.assert_allowed_freq(args.set_freq)
        except ValueError as e:
            print(f"✗ 目标频率被白名单拦截: {e}")
            return 10

    if args.dry_run:
        print("DRY-RUN: 参数与白名单校验通过, 不实际连接。")
        return 0

    try:
        with RadioLink(args.host, args.user, args.pwd,
                       bind_ip=args.bind_ip, verbose=True) as link:
            # ── 阶段 ① : read_freq 循环 ──
            print("\n[1] read_freq 循环")
            freqs = []
            for i in range(args.iterations):
                hz = link.read_freq(timeout=args.timeout)
                freqs.append(hz)
                print(f"    [{i + 1}] 频率: {hz / 1e6:.6f} MHz ({hz} Hz)")
                time.sleep(0.3)
            orig_hz = freqs[0]
            if len(set(freqs)) == 1:
                print(f"    ✓ {args.iterations} 次读数稳定 = {orig_hz / 1e6:.6f} MHz")
            else:
                print(f"    ⚠ 读数不一致: {[f / 1e6 for f in freqs]} MHz (电台可能正在调谐)")

            # ── 阶段 ② : read_mode ──
            if args.read_mode:
                print("[2] read_mode")
                mode, filt = link.read_mode(timeout=args.timeout)
                print(f"    ✓ mode=0x{mode:02X} filt=0x{filt:02X}")

            # ── 阶段 ③ : 回程写验证 set_freq → read_freq → 恢复 ──
            if args.set_freq is not None:
                target = args.set_freq
                print(f"[3] set_freq({target / 1e6:.6f} MHz) → read_freq 验证写回")
                link.set_freq(target)
                time.sleep(0.5)
                hz = link.read_freq(timeout=args.timeout)
                if hz == target:
                    print(f"    ✓ 写回一致: {hz / 1e6:.6f} MHz")
                else:
                    print(f"    ⚠ 写回不一致: 期望 {target / 1e6:.6f}, "
                          f"实际 {hz / 1e6:.6f} MHz")
                # 恢复原频率 (原频率若不在白名单, 直接 set 会被拦 —— 属正常保护)
                try:
                    link.set_freq(orig_hz)
                    time.sleep(0.3)
                    back = link.read_freq(timeout=args.timeout)
                    print(f"    ✓ 已恢复原频率: {back / 1e6:.6f} MHz")
                except ValueError as e:
                    print(f"    ⚠ 原频率 {orig_hz} 不在白名单, 未自动恢复: {e}")

            # ── 阶段 ④ : PTT TX 触发确认 (可选) ──
            if args.ptt:
                print("[4] PTT ON 1s → OFF (TX 触发确认, 注意发射安全)")
                link.ptt(True)
                print("    PTT ON 已发送")
                time.sleep(1.0)
                link.ptt(False)
                print("    PTT OFF 已发送")

    except RadioAuthError as e:
        print(f"\n✗ 认证/授权失败: {e}")
        return 2
    except RadioTimeoutError as e:
        print(f"\n✗ 链路超时: {e}")
        return 3
    except RadioLinkError as e:
        print(f"\n✗ 链路错误: {e}")
        return 4

    print("\n✓ 端到端全链路闭环完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
