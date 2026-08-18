"""probe_civ_dump — 决定性验证: 匿名 Serial(50002) 会话能否收到电台真实 CI-V 应答.

连接 -> 发 read_freq -> 静默 N 秒, 通过 SerialClient reader 的 debug 打印
把收到的每一帧(含 empty/keepalive/回显)原样 dump 到 stdout, 不解析不中断。

用法:
    RSBA1_DEBUG_READER=1 python scripts\\probe_civ_dump.py \\
        --host 192.168.0.31 --bind-ip 192.168.0.23 --secs 4
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rsba1.serial.serial_client import (  # noqa: E402
    SerialClient,
    DEFAULT_SERIAL_PORT,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="192.168.0.31")
    p.add_argument("--port-serial", type=int, default=DEFAULT_SERIAL_PORT)
    p.add_argument("--bind-ip", default="192.168.0.23")
    p.add_argument("--bind-port", type=int, default=None)
    p.add_argument("--secs", type=float, default=4.0)
    args = p.parse_args()

    print(f"=== probe_civ_dump: host={args.host}:{args.port_serial} "
          f"src={args.bind_ip}:{args.bind_port or 'random'} ===", flush=True)
    with SerialClient(args.host, args.port_serial, timeout=1.0,
                      bind_port=args.bind_port, bind_ip=args.bind_ip) as sc:
        print(f"会话: f8=0x{sc.field_8:08X} fc=0x{sc.field_C:08X}", flush=True)
        print("-- 发送 read_freq (FE FE A4 00 03 FD) --", flush=True)
        n = sc.send_read_freq()
        print(f"发送 {n} 字节", flush=True)
        time.sleep(args.secs)
    print("=== 结束 ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())