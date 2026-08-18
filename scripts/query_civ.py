"""query_civ — 真实环境 CI-V 闭环查询 CLI (P4).

通过 RemoteUtyCtrlCmd 发送 CI-V 只读命令, 从 RemoteUtyCtrlRes Mailslot
读取电台应答并解析成结构化结果 (频率 / 模式 / S-meter)。

前置条件:
    1. RemoteUtility.exe 在跑 (监听 RemoteUtyCtrlCmd)
    2. RemoteController 未运行 (否则 RemoteUtyCtrlRes 被占用, 无法独占创建)
    3. 电台 (IC-705) 已连接并响应

用法:
    python scripts\\query_civ.py --cmd freq
    python scripts\\query_civ.py --cmd mode
    python scripts\\query_civ.py --cmd smeter
    python scripts\\query_civ.py --cmd freq --timeout 5 --sub-cmd 1

返回:
    --cmd freq   打印 "频率: 14.270000 MHz"
    --cmd mode   打印 "模式: USB (filter=0x01)"
    --cmd smeter 打印 "S-meter: 0x42"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rsba1.mailslot.civ_via_execcmd import (  # noqa: E402
    CivViaExecCmdSender,
    ResponseTimeoutError,
    ResponseReadError,
)
from rsba1.mailslot import civ_response as civresp  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="P4 CI-V 闭环查询")
    p.add_argument("--cmd", choices=["freq", "mode", "smeter"], default="freq",
                   help="要查询的只读命令")
    p.add_argument("--timeout", type=float, default=3.0,
                   help="读响应超时(秒)")
    p.add_argument("--sub-cmd", type=int, default=0,
                   help="ExecCmd sub_cmd (0-5)")
    args = p.parse_args()

    timeout_ms = int(args.timeout * 1000)
    print(f"=== P4 闭环查询: {args.cmd}  sub_cmd={args.sub_cmd}  "
          f"timeout={args.timeout}s ===")

    with CivViaExecCmdSender(sub_cmd=args.sub_cmd) as sender:
        try:
            if args.cmd == "freq":
                hz = sender.query_freq(timeout_ms=timeout_ms)
                print(f"✓ 频率: {hz/1e6:.6f} MHz ({hz} Hz)")
            elif args.cmd == "mode":
                mode, filt = sender.query_mode(timeout_ms=timeout_ms)
                name = civresp.MODE_NAMES.get(mode, f"0x{mode:02X}")
                print(f"✓ 模式: {name} (code=0x{mode:02X}, filter=0x{filt:02X})")
            else:  # smeter
                val = sender.query_smeter(timeout_ms=timeout_ms)
                print(f"✓ S-meter: 0x{val:02X} ({val})")
        except ResponseTimeoutError as e:
            print(f"✗ 超时: {e}")
            return 2
        except ResponseReadError as e:
            print(f"✗ 读取失败: {e}")
            return 3
        except civresp.CivResponseError as e:
            print(f"✗ 解析失败: {e}")
            return 4

    return 0


if __name__ == "__main__":
    sys.exit(main())