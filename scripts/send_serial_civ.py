"""send_serial_civ — CI-V 命令经 Serial 信道 (UDP 50002) 发送的命令行工具.

通过 SerialClient 向运行 RemoteUty.exe 的服务器 UDP 50002 端口发送 CI-V
命令, 并读取应答 (相比 ExecCmd Mailslot 单向 fire-and-forget, 本工具可
真正读到 CI-V 响应帧)。

支持命令:
    read_freq  读频率 (cmd=0x03)
    read_mode  读模式 (cmd=0x04)
    set_freq   设频率 (cmd=0x06 + BCD), 需 Hz 参数
    ptt_on     PTT ON (TX)
    ptt_off    PTT OFF (RX)
    read_smeter 读 S-meter (cmd=0x1A 0x03)
    raw        原始 CI-V 帧 (hex, 含 FE/FD)

前置条件:
    1. 服务器 (远程运行 RemoteUty.exe 的机器) 可达, Serial 信道 UDP 50002 开放
    2. 双方已建立连接 (RemoteController 已连上服务器), 否则服务器可能不回包

用法:
    cd d:\\my git\\rs-ba1-reverse
    python scripts\\send_serial_civ.py --host 192.168.1.10 read_freq
    python scripts\\send_serial_civ.py --host 192.168.1.10 set_freq 14270000
    python scripts\\send_serial_civ.py --host 192.168.1.10 ptt_on
    python scripts\\send_serial_civ.py --host 192.168.1.10 raw fefea4e003fd
    python scripts\\send_serial_civ.py --host 192.168.1.10 read_freq --timeout 3.0 -v

注意:
    - field_8/field_C 默认使用线上确证的会话 id 常值; 若服务器不回包,
      可能需先建立连接或改用实际会话 id (见 serial_channel.md §5.4)。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC_DIR = _SCRIPT_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from rsba1.serial.serial_client import (  # noqa: E402
    SerialClient,
    SerialClientError,
    SerialTimeoutError,
    DEFAULT_SERIAL_PORT,
)

log = logging.getLogger("send_serial_civ")


def _cmd_send(client: SerialClient, args: argparse.Namespace) -> int:
    """按 args.command 发送命令, 返回 CI-V 响应帧 bytes."""
    cmd = args.command
    if cmd == "read_freq":
        client.send_read_freq()
    elif cmd == "read_mode":
        client.send_read_mode()
    elif cmd == "set_freq":
        client.send_set_freq(int(args.value))
    elif cmd == "ptt_on":
        client.send_ptt_on()
    elif cmd == "ptt_off":
        client.send_ptt_off()
    elif cmd == "read_smeter":
        client.send_read_smeter()
    elif cmd == "raw":
        client.send_civ(bytes.fromhex(args.value))
    else:
        raise SerialClientError(f"未知命令: {cmd}")
    return client.read_civ_response(timeout=args.timeout)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="send_serial_civ",
        description="CI-V 命令经 Serial 信道 (UDP 50002) 发送并读取应答",
    )
    parser.add_argument("--host", required=True, help="服务器 IP (运行 RemoteUty.exe)")
    parser.add_argument("--port", type=int, default=DEFAULT_SERIAL_PORT,
                        help=f"Serial 信道 UDP 端口 (默认 {DEFAULT_SERIAL_PORT})")
    parser.add_argument("--timeout", type=float, default=2.0,
                        help="等待 CI-V 响应超时 (秒, 默认 2.0)")
    parser.add_argument("--to-addr", type=lambda s: int(s, 0), default=0xA4,
                        help="目标电台 CI-V 地址 (默认 0xA4 = IC-705)")
    parser.add_argument("--from-addr", type=lambda s: int(s, 0), default=0x00,
                        help="源控制器 CI-V 地址 (默认 0x00)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="打印发送/接收的原始 hex")
    parser.add_argument("command", help="read_freq/read_mode/set_freq/ptt_on/ptt_off/read_smeter/raw")
    parser.add_argument("value", nargs="?",
                        help="set_freq 的频率 Hz, 或 raw 的 CI-V 帧 hex")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    client = SerialClient(
        args.host, args.port,
        to_addr=args.to_addr, from_addr=args.from_addr,
        timeout=args.timeout,
    )
    try:
        with client:
            resp = _cmd_send(client, args)
    except (SerialTimeoutError, SerialClientError, ValueError) as e:
        log.error("失败: %s", e)
        return 1

    if args.verbose:
        log.info("CI-V 响应帧 (%d 字节): %s", len(resp), resp.hex())
    else:
        print(resp.hex())

    # 尝试解析频率 (读频率应答: cmd=0x03 后接 5 字节 BCD)
    if args.command == "read_freq" and len(resp) >= 8:
        from rsba1.ctypes_wrappers.civ_commands import parse_frame, bytes_to_freq
        try:
            _, _, cmd, payload = parse_frame(resp)
            if cmd == 0x03:
                freq = bytes_to_freq(payload[:5])
                print(f"频率: {freq} Hz = {freq/1e6:.6f} MHz")
        except (ValueError, TypeError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())