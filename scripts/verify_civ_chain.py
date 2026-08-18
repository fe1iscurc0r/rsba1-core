"""
动态验证 CI-V 经 ExecCmd 链路 — 发送只读命令到真实 RS-BA1 RemoteUtility。

前置条件:
    1. RS-BA1 GUI (RemoteUtility.exe) 在跑, 已连上 IC-705 电台
    2. mailslot \\\\.\\mailslot\\RemoteUtyCtrlCmd 存在 (GUI 开着即存在)

本脚本 (P3 动态验证):
    - 顺序发送 8 个只读 CI-V 命令: 读频率 / 读模式 / 读 S-meter (各多次)
    - 每个命令 fire-and-forget (ExecCmd 单向, 不读响应)
    - 打印每个命令的帧字节数 + 写入结果 (成功/失败)
    - 提示用户观察 RS-BA1 GUI 是否响应 (频率/模式/S 表变化)

只读、安全, 不会改变电台状态 (不设频率 / 不 PTT)。

用法:
    python scripts\\verify_civ_chain.py
    python scripts\\verify_civ_chain.py --mailslot "\\\\\\\\.\\\\mailslot\\\\RemoteUtyCtrlCmd"
    python scripts\\verify_civ_chain.py --dry-run   # 仅打印字节序列, 不发送
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# 加入 src 到 path
_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC_DIR = _SCRIPT_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from rsba1.mailslot.client import (  # noqa: E402
    MailslotClient,
    DEFAULT_MAILSLOT_NAME,
    MailslotError,
)
from rsba1.mailslot.civ_via_execcmd import (  # noqa: E402
    CivViaExecCmdSender,
    build_read_freq_payload,
    build_read_mode_payload,
    build_read_smeter_payload,
    DEFAULT_TO_ADDR,
    DEFAULT_FROM_ADDR,
)

LOG = logging.getLogger("verify_civ_chain")

# 8 个只读命令的 payload 构造器 (各命令多次以增加命中窗口)
PAYLOAD_BUILDERS = [
    ("读频率 (read_freq)", build_read_freq_payload),
    ("读频率 (read_freq)", build_read_freq_payload),
    ("读模式 (read_mode)", build_read_mode_payload),
    ("读模式 (read_mode)", build_read_mode_payload),
    ("读 S-meter (read_smeter)", build_read_smeter_payload),
    ("读 S-meter (read_smeter)", build_read_smeter_payload),
    ("读 S-meter (read_smeter)", build_read_smeter_payload),
    ("读 S-meter (read_smeter)", build_read_smeter_payload),
]


def main() -> int:
    p = argparse.ArgumentParser(description="CI-V 经 ExecCmd 链路动态验证 (只读命令)")
    p.add_argument("--mailslot", default=DEFAULT_MAILSLOT_NAME,
                   help=f"Mailslot 路径 (默认 {DEFAULT_MAILSLOT_NAME})")
    p.add_argument("--to-addr", type=lambda x: int(x, 0), default=DEFAULT_TO_ADDR,
                   help=f"目标电台 CI-V 地址 (默认 0x{DEFAULT_TO_ADDR:02X} = IC-705)")
    p.add_argument("--from-addr", type=lambda x: int(x, 0), default=DEFAULT_FROM_ADDR,
                   help=f"控制器地址 (默认 0x{DEFAULT_FROM_ADDR:02X})")
    p.add_argument("--dry-run", action="store_true",
                   help="仅构造并打印 payload, 不实际写入 Mailslot")
    p.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"\n=== CI-V 经 ExecCmd 链路验证 (只读) ===")
    print(f"Mailslot: {args.mailslot}")
    print(f"电台地址 to=0x{args.to_addr:02X} from=0x{args.from_addr:02X}")
    print(f"模式: {'dry-run (不写入)' if args.dry_run else '实发'}")
    print()

    # 统一构造参数: 各 build_*_payload 的公共关键字
    common = dict(to_addr=args.to_addr, from_addr=args.from_addr)

    if args.dry_run:
        success = 0
        for name, builder in PAYLOAD_BUILDERS:
            try:
                payload, data_len = builder(**common)
                print(f"  {name:28s}  payload={len(payload):3d}B  data_len={data_len:3d}  ✓ 构造")
                success += 1
            except Exception as e:
                print(f"  {name:28s}  ✗ {e}")
        print(f"\n=== 结果 (dry-run) ===")
        print(f"构造成功: {success}/{len(PAYLOAD_BUILDERS)}")
        print("  实发请去掉 --dry-run")
        return 0

    # 实发: 先打开 mailslot 确认存在
    client = None
    try:
        client = MailslotClient(mailslot_name=args.mailslot)
        client.open()
        print(f"✓ Mailslot 已打开: {args.mailslot}")
    except MailslotError as e:
        print(f"✗ 打开 Mailslot 失败: {e}")
        print(f"  提示: 请确认 RS-BA1 GUI 已启动 (RemoteUtility.exe 在跑) 且已连上 IC-705")
        return 1

    success = 0
    failed = 0
    try:
        with CivViaExecCmdSender(
            mailslot_name=args.mailslot,
            to_addr=args.to_addr,
            from_addr=args.from_addr,
            backend="ctypes",
        ) as sender:
            for name, builder in PAYLOAD_BUILDERS:
                try:
                    payload, _ = builder(**common)
                    n = sender.send_payload(payload)
                    print(f"  {name:28s}  written={n:3d}B  ✓")
                    success += 1
                    time.sleep(1.5)  # 给 RemoteUtility/GUI 处理时间 (慢速便于观察)
                except Exception as e:
                    print(f"  {name:28s}  ✗ {e}")
                    failed += 1
    finally:
        client.close()
        print(f"\n✓ Mailslot 已关闭")

    print(f"\n=== 结果 ===")
    print(f"成功: {success}/{len(PAYLOAD_BUILDERS)}")
    print(f"失败: {failed}/{len(PAYLOAD_BUILDERS)}")

    if failed == 0 and success == len(PAYLOAD_BUILDERS):
        print("\n✓✓✓ 全部只读 CI-V 命令写入成功！")
        print("    请观察 RS-BA1 GUI: 频率读数 / 模式指示 / S 表是否在变动。")
        print("    若 GUI 有响应, 说明 ExecCmd + CI-V 链路打通, P3 完成。")
        print("    若 GUI 无响应, 可能是 arg3/arg6/sub_cmd 语义需调整 (见文档)。")
        return 0
    else:
        print(f"\n✗ 有 {failed} 个命令失败")
        return 2


if __name__ == "__main__":
    sys.exit(main())