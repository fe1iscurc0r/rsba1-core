"""send_civ_command — CI-V 命令经 ExecCmd Mailslot 发送的命令行工具。

把 CI-V 命令 (read_freq / read_mode / set_freq / ptt_on / ptt_off /
read_smeter / raw) 包成 ExecCmd (cmd_code=2) payload, 通过 MailslotClient
写入 \\.\mailslot\RemoteUtyCtrlCmd, 让本机 RemoteUtility 转发到电台。

核心特性:
    - fire-and-forget (ExecCmd 不直接读响应)
    - 可选 --read-response: RemoteController 未运行时, 陆墨自行创建
      RemoteUtyCtrlRes mailslot 独占接收响应 (方案 A)
    - --dry-run: 仅打印构造的 payload, 不实际写入
    - 完整 hex dump 输出 (CI-V 帧 / ExecCmd payload / 写入字节数)

前置条件:
    1. RS-BA1 GUI 开着 (RemoteUtility.exe 在跑, 监听 RemoteUtyCtrlCmd)
    2. mailslot_probe.py 已确认 RemoteUtyCtrlCmd 存在

用法:
    cd d:\\my git\\rs-ba1-reverse
    d:\\my git\\scratchpad\\.venv\\Scripts\\python.exe scripts\\send_civ_command.py read_freq
    d:\\my git\\scratchpad\\.venv\\Scripts\\python.exe scripts\\send_civ_command.py set_freq 14270000
    d:\\my git\\scratchpad\\.venv\\Scripts\\python.exe scripts\\send_civ_command.py ptt_on
    d:\\my git\\scratchpad\\.venv\\Scripts\\python.exe scripts\\send_civ_command.py raw 03
    d:\\my git\\scratchpad\\.venv\\Scripts\\python.exe scripts\\send_civ_command.py read_freq --read-response
    d:\\my git\\scratchpad\\.venv\\Scripts\\python.exe scripts\\send_civ_command.py read_freq --dry-run -v

注意:
    - ExecCmd 是单向: 写 Mailslot 后无法直接读响应 (响应去 RemoteController mailslot)
    - RemoteController 开着时, --read-response 会失败 (mailslot 已被占用), 只能靠 GUI 看效果
    - RemoteController 没开时, --read-response 才能独占响应
    - arg3 / arg6 / sub_cmd 语义待动态确认, 默认全 0
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 把 src/ 加到 sys.path, 让 rsba1.mailslot 可被 import
_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC_DIR = _SCRIPT_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from rsba1.mailslot.client import (  # noqa: E402
    MailslotError,
    MailslotNotFoundError,
    MailslotWriteError,
    MailslotTimeoutError,
    DEFAULT_MAILSLOT_NAME,
)
from rsba1.mailslot.protocol import CMD_EXEC_CMD  # noqa: E402
from rsba1.mailslot.civ_via_execcmd import (  # noqa: E402
    DEFAULT_TO_ADDR,
    DEFAULT_FROM_ADDR,
    DEFAULT_SUB_CMD,
    DEFAULT_ARG3,
    DEFAULT_ARG6,
    RESPONSE_MAILSLOT_NAME,
    build_exec_cmd_civ,
    build_read_freq_payload,
    build_read_mode_payload,
    build_set_freq_payload,
    build_ptt_on_payload,
    build_ptt_off_payload,
    build_read_smeter_payload,
    CivViaExecCmdSender,
    ResponseReader,
    ResponseReadError,
)

LOG = logging.getLogger("send_civ")

# 子命令 -> (描述, 是否需要位置参数)
SUBCOMMANDS = {
    "read_freq":   ("读频率 (CI-V cmd=0x03)", False),
    "read_mode":   ("读模式 (CI-V cmd=0x04)", False),
    "read_smeter": ("读 S-meter (CI-V cmd=0x1A 0x03)", False),
    "set_freq":    ("设频率 (CI-V cmd=0x06 + BCD), 需 Hz 参数", True),
    "ptt_on":      ("PTT ON / TX (CI-V cmd=0x1C 0x00 0x01)", False),
    "ptt_off":     ("PTT OFF / RX (CI-V cmd=0x1C 0x00 0x00)", False),
    "raw":         ("原始 CI-V 命令体 (hex), 如 '03' 或 '1A03'", True),
}


def hex_dump(data: bytes, prefix: str = "  ") -> str:
    """格式化 bytes 为 hex dump (单行 + 偏移视图)。"""
    if not data:
        return f"{prefix}(空)"
    hex_str = " ".join(f"{b:02X}" for b in data)
    # 单行紧凑 hex
    single = f"{prefix}hex: {hex_str}"
    # 带偏移的多行视图 (每行 16 字节)
    lines = [single]
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{prefix}{i:04X}  {hex_part:<48}  {ascii_part}")
    return "\n".join(lines)


def parse_hex_bytes(s: str) -> bytes:
    """解析 hex 字符串为 bytes (允许空格/冒号分隔, 如 '03' / '1A 03' / '1A:03')。"""
    cleaned = s.replace(" ", "").replace(":", "").replace("-", "")
    if len(cleaned) % 2 != 0:
        raise ValueError(f"hex 字符串长度必须为偶数: {s!r}")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as e:
        raise ValueError(f"无效 hex 字符串 {s!r}: {e}") from e


def build_payload_for_cmd(args: argparse.Namespace) -> tuple[bytes, int, str]:
    """根据子命令构造 ExecCmd payload, 返回 (payload, data_len, 描述)。"""
    cmd = args.command
    common = dict(arg3=args.arg3, arg6=args.arg6, sub_cmd=args.sub_cmd)

    if cmd == "read_freq":
        payload, data_len = build_read_freq_payload(
            args.to_addr, args.from_addr, **common)
        desc = f"read_freq [to=0x{args.to_addr:02X} from=0x{args.from_addr:02X}]"
    elif cmd == "read_mode":
        payload, data_len = build_read_mode_payload(
            args.to_addr, args.from_addr, **common)
        desc = f"read_mode [to=0x{args.to_addr:02X} from=0x{args.from_addr:02X}]"
    elif cmd == "read_smeter":
        payload, data_len = build_read_smeter_payload(
            args.to_addr, args.from_addr, **common)
        desc = f"read_smeter [to=0x{args.to_addr:02X} from=0x{args.from_addr:02X}]"
    elif cmd == "set_freq":
        payload, data_len = build_set_freq_payload(
            args.freq, args.to_addr, args.from_addr, **common)
        desc = f"set_freq {args.freq} Hz ({args.freq / 1e6:.6f} MHz)"
    elif cmd == "ptt_on":
        payload, data_len = build_ptt_on_payload(
            args.to_addr, args.from_addr, **common)
        desc = "ptt_on (TX)"
    elif cmd == "ptt_off":
        payload, data_len = build_ptt_off_payload(
            args.to_addr, args.from_addr, **common)
        desc = "ptt_off (RX)"
    elif cmd == "raw":
        civ_body = parse_hex_bytes(args.hex_bytes)
        # raw: 手动加 to/from 地址 (civSend 约定) 后包成 ExecCmd payload
        body = bytes([args.to_addr & 0xFF, args.from_addr & 0xFF]) + civ_body
        payload, data_len = build_exec_cmd_civ(body, **common)
        desc = f"raw CI-V body: {civ_body.hex().upper()}"
    else:
        raise ValueError(f"未知子命令: {cmd}")

    return payload, data_len, desc


def do_send(args: argparse.Namespace) -> int:
    """构造 + (可选) 发送 ExecCmd, 返回退出码。"""
    try:
        payload, data_len, desc = build_payload_for_cmd(args)
    except (ValueError, TypeError) as e:
        print(f"[错误] payload 构造失败: {e}", file=sys.stderr)
        return 2

    print(f"\n=== CI-V via ExecCmd ===")
    print(f"命令: {desc}")
    print(f"Mailslot: {args.mailslot}")
    print(f"ExecCmd 参数: arg3={args.arg3} arg6={args.arg6} sub_cmd={args.sub_cmd}")
    print(f"data_len: {data_len} (0x{data_len:02X})")
    print(f"\nExecCmd payload ({len(payload)} 字节):")
    print(hex_dump(payload))

    if args.dry_run:
        print(f"\n[dry-run] 未实际写入 Mailslot")
        return 0

    print(f"\n--- 写入 Mailslot ---")
    written = 0
    try:
        with CivViaExecCmdSender(
            mailslot_name=args.mailslot,
            to_addr=args.to_addr,
            from_addr=args.from_addr,
            arg3=args.arg3,
            arg6=args.arg6,
            sub_cmd=args.sub_cmd,
            backend=args.backend,
        ) as sender:
            LOG.debug("sender 已打开: %r", sender)
            written = sender.send_payload(payload)
        print(f"写入成功: {written} 字节 (4 字节头 + {written - 4} 字节 payload)")
        print(f"  cmd_code = 0x{CMD_EXEC_CMD:02X} (ExecCmd)")
        print(f"  data_len = {data_len}")
    except MailslotNotFoundError as e:
        print(f"\n[失败] Mailslot 不存在: {e}", file=sys.stderr)
        print(f"  请确认 RS-BA1 GUI (RemoteUtility.exe) 正在运行", file=sys.stderr)
        return 3
    except MailslotTimeoutError as e:
        print(f"\n[失败] Mailslot 写入超时: {e}", file=sys.stderr)
        return 4
    except MailslotWriteError as e:
        print(f"\n[失败] Mailslot 写入错误: {e}", file=sys.stderr)
        return 5
    except MailslotError as e:
        print(f"\n[失败] Mailslot 错误: {e}", file=sys.stderr)
        return 6

    # 可选: 读响应 (仅 RemoteController 未运行时可用)
    if args.read_response:
        print(f"\n--- 读响应 Mailslot ---")
        print(f"响应 Mailslot: {RESPONSE_MAILSLOT_NAME}")
        try:
            with ResponseReader(read_timeout_ms=args.response_timeout) as reader:
                print(f"已创建响应 Mailslot (独占模式), 等待响应...")
                # 给 RemoteUtility 一点时间处理并回响应
                resp = reader.read(timeout_ms=args.response_timeout)
                if resp is None:
                    print(f"[超时] {args.response_timeout}ms 内无响应")
                    print(f"  可能原因: 电台未连 / RemoteController 抢占了响应 / 命令未触发响应")
                else:
                    print(f"\n收到响应 ({len(resp)} 字节):")
                    print(hex_dump(resp))
                    # 尝试解析响应 cmd_code echo
                    if len(resp) >= 1:
                        print(f"\n  响应 cmd_code echo: 0x{resp[0]:02X}"
                              f" (期望 0x{CMD_EXEC_CMD:02X} = ExecCmd)")
                    # 如果响应里含 CI-V 帧, 尝试解析
                    _try_parse_civ_response(resp)
        except ResponseReadError as e:
            print(f"\n[响应读取失败] {e}", file=sys.stderr)
            print(f"  通常因 RemoteController 正在运行, 已占用 {RESPONSE_MAILSLOT_NAME}",
                  file=sys.stderr)
            print(f"  RemoteController 开着时只能 fire-and-forget, 靠 GUI 看效果",
                  file=sys.stderr)
            return 7

    return 0


def _try_parse_civ_response(resp: bytes) -> None:
    """尝试在响应 bytes 中找 CI-V 帧 (FE FE ... FD) 并解析。"""
    # 找 FE FE 前导
    start = -1
    for i in range(len(resp) - 1):
        if resp[i] == 0xFE and resp[i + 1] == 0xFE:
            start = i
            break
    if start < 0:
        return
    # 找 FD 尾
    end = resp.rfind(0xFD)
    if end <= start:
        return
    frame = resp[start:end + 1]
    print(f"\n  检测到 CI-V 帧 (offset {start}..{end}):")
    print(hex_dump(frame, prefix="    "))
    try:
        from rsba1.ctypes_wrappers import civ_commands as civcmd
        to_addr, from_addr, cmd, payload = civcmd.parse_frame(frame)
        print(f"    to=0x{to_addr:02X} from=0x{from_addr:02X} cmd=0x{cmd:02X} "
              f"payload={payload.hex().upper()}")
        # 已知命令解码
        if cmd == 0x03 and len(payload) == 5:
            freq = civcmd.bytes_to_freq(payload)
            print(f"    -> 读频率响应: {freq} Hz ({freq / 1e6:.6f} MHz)")
        elif cmd == 0x04 and len(payload) >= 2:
            modes = {0: "LSB", 1: "USB", 2: "AM", 3: "CW", 4: "RTTY",
                     5: "FM", 6: "WFM", 7: "CW-R", 8: "RTTY-R", 17: "DV"}
            mode = modes.get(payload[0], f"未知(0x{payload[0]:02X})")
            print(f"    -> 读模式响应: mode={mode} filter={payload[1]}")
        elif cmd == 0x06:
            print(f"    -> 设频率确认 (echo)")
        elif cmd == 0x1C:
            ptt_state = "ON (TX)" if len(payload) >= 2 and payload[1] == 0x01 else "OFF (RX)"
            print(f"    -> PTT 确认: {ptt_state}")
    except Exception as e:
        print(f"    (CI-V 帧解析失败: {e})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="CI-V 命令经 ExecCmd Mailslot 发送 (P3 核心工具)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "子命令示例:",
            "  read_freq            读频率",
            "  read_mode            读模式",
            "  read_smeter          读 S-meter",
            "  set_freq 14270000    设频率 14.270 MHz",
            "  ptt_on               PTT ON (TX)",
            "  ptt_off              PTT OFF (RX)",
            "  raw 03               原始 CI-V 命令体 (hex)",
            "",
            "动态验证步骤:",
            "  1. 开 RS-BA1 GUI 连上电台",
            "  2. python scripts/send_civ_command.py read_freq --dry-run  # 先看 payload",
            "  3. python scripts/send_civ_command.py read_freq             # 实发, 看 GUI",
            "  4. python scripts/send_civ_command.py read_freq --read-response  # 独占读响应",
        ]),
    )
    p.add_argument("command", choices=list(SUBCOMMANDS.keys()),
                   help="CI-V 子命令")
    p.add_argument("value", nargs="?", default=None,
                   help="set_freq 的 Hz / raw 的 hex bytes")

    # 地址 / ExecCmd 参数
    p.add_argument("--to-addr", type=lambda x: int(x, 0), default=DEFAULT_TO_ADDR,
                   help=f"目标电台 CI-V 地址 (默认 0x{DEFAULT_TO_ADDR:02X} = IC-705)")
    p.add_argument("--from-addr", type=lambda x: int(x, 0), default=DEFAULT_FROM_ADDR,
                   help=f"源控制器地址 (默认 0x{DEFAULT_FROM_ADDR:02X})")
    p.add_argument("--arg3", type=lambda x: int(x, 0), default=DEFAULT_ARG3,
                   help=f"ExecCmd arg3 DWORD (默认 {DEFAULT_ARG3}, 语义待确认)")
    p.add_argument("--arg6", type=lambda x: int(x, 0), default=DEFAULT_ARG6,
                   help=f"ExecCmd arg6 BYTE (默认 {DEFAULT_ARG6}, 语义待确认)")
    p.add_argument("--sub-cmd", type=lambda x: int(x, 0), default=DEFAULT_SUB_CMD,
                   help=f"RemoteUty 子命令码 0-5 (默认 {DEFAULT_SUB_CMD}, 语义待确认)")

    # Mailslot / 后端
    p.add_argument("--mailslot", default=DEFAULT_MAILSLOT_NAME,
                   help=f"命令 Mailslot 路径 (默认 {DEFAULT_MAILSLOT_NAME})")
    p.add_argument("--backend", choices=["pywin32", "ctypes"], default=None,
                   help="MailslotClient 后端 (默认自动选择)")

    # 行为开关
    p.add_argument("--dry-run", action="store_true",
                   help="仅打印 payload, 不实际写入 Mailslot")
    p.add_argument("--read-response", action="store_true",
                   help="创建 RemoteUtyCtrlRes 独占读响应 (仅 RemoteController 未运行时可用)")
    p.add_argument("--response-timeout", type=int, default=2000,
                   help="响应读取超时 ms (默认 2000, 仅 --read-response 时生效)")

    p.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # 处理子命令的位置参数
    if args.command == "set_freq":
        if args.value is None:
            print("[错误] set_freq 需要频率 Hz 参数, 如: set_freq 14270000",
                  file=sys.stderr)
            return 2
        try:
            args.freq = int(args.value, 0)
        except ValueError:
            print(f"[错误] 无效频率: {args.value!r} (需整数 Hz)", file=sys.stderr)
            return 2
        if args.freq < 0:
            print(f"[错误] 频率不能为负: {args.freq}", file=sys.stderr)
            return 2
    elif args.command == "raw":
        if args.value is None:
            print("[错误] raw 需要 hex bytes 参数, 如: raw 03 或 raw '1A 03'",
                  file=sys.stderr)
            return 2
        args.hex_bytes = args.value
    else:
        if args.value is not None:
            print(f"[警告] 子命令 {args.command!r} 不接受参数, 忽略 {args.value!r}",
                  file=sys.stderr)

    return do_send(args)


if __name__ == "__main__":
    sys.exit(main())
