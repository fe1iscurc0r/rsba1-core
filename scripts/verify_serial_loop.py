"""verify_serial_loop — Serial(50002) 收发 CI-V 闭环验证 (含 Command 探测).

背景 (P4 实测 + 抓包 2026-08-11):
    - CI-V 应答不走 RemoteUtyCtrlRes Mailslot。
    - ⚠️ 归因收敛 (2026-08-11): "Serial 独立建会话、无需 Command 认证"的旧结论**已被抓包推翻**。
      抓包 (parsed_5001_5002.txt) 显示: 服务器只向"已认证源端口"主动发探测包, 源端口信息来自
      Command(50001) ConnectServer 认证。field_8/field_C 是服务器进程生成的会话标识, 客户端只能对调回传。
    - 本脚本 (仅发 Serial 注册包 + CI-V) 因缺 Command 前置而超时, 应结合 command_client.py 先认证再收发
      (详见 serial_channel.md §5.8)。保留了原始探测逻辑以便对照。

本脚本步骤:
    1. SerialClient 发送 read_freq (自动补发注册包), 读取并解析 CI-V 应答;
       尝试"默认 ID"与"默认对调"两种组合 (field_8/field_C 对调语义存疑)。
    2. 可选探测 Command 信道注册 (command_client), 仅用于确认会话建立。

前置条件:
    1. RemoteUtility.exe 在跑 (本机或局域网)
    2. 电台 (IC-705) 已连接并响应

用法:
    # 本机 RemoteUty (默认绑源端口 50002, 匹配真机会话识别)
    python scripts\\verify_serial_loop.py --host 127.0.0.1
    # 局域网 RemoteUty (绑本机 LAN IP 源端口)
    python scripts\\verify_serial_loop.py --host 192.168.0.23 --bind-ip 192.168.0.23

返回:
    打印各会话标识组合下的 read_freq 响应与解析频率。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rsba1.serial.serial_client import (  # noqa: E402
    SerialClient,
    DEFAULT_SERIAL_PORT,
    DEFAULT_SESSION_F8,
    DEFAULT_SESSION_FC,
)
from rsba1.mailslot import civ_response as civresp     # noqa: E402
from rsba1.ctypes_wrappers import civ_commands as civcmd  # noqa: E402


def _run_serial_loop(host, port, f8, fc, timeout, tag, bind_port=None, bind_ip=None,
                     raw=False):
    """用指定会话标识发 read_freq 并解析。返回 (hz, raw_hex) 或 (None, err)。"""
    try:
        with SerialClient(host, port, field_8=f8, field_C=fc, timeout=timeout,
                          bind_port=bind_port, bind_ip=bind_ip or "127.0.0.1") as sc:
            sc.send_read_freq()
            if raw:
                # 原始打印模式: 打印收到的每个 UDP 包 (含 keepalive / 回环)
                _dump_until(sc, timeout)
            resp = sc.read_civ_response(timeout=timeout)
            try:
                hz = civresp.parse_freq(resp)
                return hz, resp.hex()
            except civresp.CivResponseError as e:
                return None, f"解析失败({e}): {resp.hex()}"
    except Exception as e:  # noqa: BLE001 - 探测脚本, 容纳各类底层异常
        return None, f"{type(e).__name__}: {e}"


def _dump_until(sc, timeout):
    """持续接收并打印每个 UDP 包原始 hex, 直到超时."""
    import time
    deadline = time.time() + timeout
    n = 0
    while time.time() < deadline:
        try:
            data = sc.recv_udp(max(0.05, deadline - time.time()))
        except Exception:
            break
        n += 1
        print(f"        [收-{n}] {len(data)}B: {data.hex()}")
    print(f"        [收] 共 {n} 个包 (原始打印超时).")


def main() -> int:
    p = argparse.ArgumentParser(description="Serial 收发 CI-V 闭环验证")
    p.add_argument("--host", default="127.0.0.1", help="RemoteUty 服务器 IP")
    p.add_argument("--port-serial", type=int, default=DEFAULT_SERIAL_PORT)
    p.add_argument("--timeout", type=float, default=3.0, help="收发超时(秒)")
    p.add_argument("--bind-port", type=int, default=DEFAULT_SERIAL_PORT,
                   help="绑定本地源端口 (服务器按源端口识别会话, 默认 50002)")
    p.add_argument("--bind-ip", default="127.0.0.1",
                   help="绑定本地源 IP (默认 127.0.0.1; 局域网访问时填本机 LAN IP)")
    p.add_argument("--raw", action="store_true",
                   help="原始打印模式: 打印收到的每个 UDP 包 (含 keepalive / 回环)")
    args = p.parse_args()

    print(f"=== Serial 收发 CI-V 闭环验证 host={args.host}:{args.port_serial} "
          f"bind={args.bind_ip}:{args.bind_port} ===")

    # 1. Serial 收发 read_freq (自动补发会话注册包)
    print("[1] Serial 收发 read_freq")
    combos = [
        ("稳定默认ID", DEFAULT_SESSION_F8, DEFAULT_SESSION_FC),
        ("默认对调", DEFAULT_SESSION_FC, DEFAULT_SESSION_F8),
    ]
    any_ok = False
    for tag, sf8, sfc in combos:
        hz, info = _run_serial_loop(
            args.host, args.port_serial, sf8, sfc, args.timeout, tag,
            bind_port=args.bind_port, bind_ip=args.bind_ip, raw=args.raw,
        )
        if hz is not None:
            any_ok = True
            print(f"    [{tag}] ✓ 频率: {hz/1e6:.6f} MHz ({hz} Hz)  "
                  f"f8=0x{sf8:08X} fc=0x{sfc:08X}")
        else:
            print(f"    [{tag}] ✗ {info}")

    if not any_ok:
        print("    ✗ 所有会话标识组合均未拿到有效 CI-V 应答。")
        return 3

    print("\n✓ 闭环打通。")
    return 0


if __name__ == "__main__":
    sys.exit(main())