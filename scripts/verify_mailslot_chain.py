"""
动态验证 Mailslot 链路 — 发送 8 个 Get* 命令到真实 RemoteUtility。

前置条件:
    1. RS-BA1 RemoteUtility.exe 在跑 (用户开了 RS-BA1 GUI)
    2. mailslot_probe.py 已确认 \\.\mailslot\\RemoteUtyCtrlCmd 存在

本脚本:
    - 顺序发送 cmd 0, 1, 3, 4, 5, 6, 7, 8 (跳过 ExecCmd=2, 避免干扰电台)
    - 每个命令 fire-and-forget (不读响应 mailslot, 因 RemoteUtyCtrlRes 由 RemoteController 创建/读)
    - 记录每个命令的字节数 + 写入结果 (成功/失败)
    - 不读取响应 (避免与 RemoteController 抢响应 mailslot)

用法:
    python verify_mailslot_chain.py
    python verify_mailslot_chain.py --mailslot "\\\\.\\\mailslot\\RemoteUtyCtrlCmd"
    python verify_mailslot_chain.py --dry-run   # 仅打印不发送
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

from rsba1.mailslot.client import MailslotClient, DEFAULT_MAILSLOT_NAME, MailslotError
from rsba1.mailslot.protocol import CMD_CODES, CMD_NAME
from rsba1.mailslot.commands import (
    build_get_count_client_trans,
    build_get_client_trans_info,
    build_get_client_trans_vol,
    build_get_client_trans_info_2,
    build_get_client_trans_vol_3,
    build_get_command_proc_count,
    build_get_remote_trans_network_set,
    build_get_remote_trans_state,
)

LOG = logging.getLogger("verify_chain")

# 8 个 Get* 命令的测试序列 (跳过 ExecCmd=2)
TEST_SEQUENCE = [
    (0x00, "GetCountClientTrans", build_get_count_client_trans),
    (0x06, "GetCommandProcCount", build_get_command_proc_count),
    (0x01, "GetClientTransInfo(0)", lambda: build_get_client_trans_info(0)),
    (0x04, "GetClientTransInfo2(0)", lambda: build_get_client_trans_info_2(0)),
    (0x03, "GetClientTransVol(0,0,0,0)", lambda: build_get_client_trans_vol(0, 0, 0, 0)),
    (0x05, "GetClientTransVol3(0,0,0,0)", lambda: build_get_client_trans_vol_3(0, 0, 0, 0)),
    (0x07, "GetRemoteTransNetworkSet(0)", lambda: build_get_remote_trans_network_set(0)),
    (0x08, "GetRemoteTransState(0)", lambda: build_get_remote_trans_state(0)),
]


def main() -> int:
    p = argparse.ArgumentParser(description="Mailslot 链路动态验证")
    p.add_argument("--mailslot", default=DEFAULT_MAILSLOT_NAME,
                   help=f"Mailslot 路径 (默认 {DEFAULT_MAILSLOT_NAME})")
    p.add_argument("--dry-run", action="store_true",
                   help="仅构造 payload 打印, 不实际写入")
    p.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"\n=== Mailslot 链路验证 ===")
    print(f"Mailslot: {args.mailslot}")
    print(f"模式: {'dry-run (不写入)' if args.dry_run else '实发'}")
    print()

    # 先打开 mailslot (除非 dry-run)
    client = None
    if not args.dry_run:
        try:
            client = MailslotClient(mailslot_name=args.mailslot)
            client.open()
            print(f"✓ Mailslot 已打开: {args.mailslot}")
        except MailslotError as e:
            print(f"✗ 打开 Mailslot 失败: {e}")
            print(f"  提示: 请确认 RS-BA1 GUI 已启动 (RemoteUtility 在跑)")
            return 1

    success = 0
    failed = 0
    for cmd_code, name, builder in TEST_SEQUENCE:
        try:
            payload = builder()
            data_len = len(payload)
            total = data_len + 4  # 加 4 字节头

            if args.dry_run:
                print(f"  [{cmd_code:#04x}] {name:32s}  payload={data_len:3d}B  total={total:3d}B  ✓ 构造")
                success += 1
                continue

            # 实发
            n_written = client.write_command(cmd_code, payload)
            print(f"  [{cmd_code:#04x}] {name:32s}  payload={data_len:3d}B  written={n_written:3d}B  ✓")
            success += 1
            time.sleep(0.1)  # 给 RemoteUtility 处理时间, 避免淹没
        except Exception as e:
            print(f"  [{cmd_code:#04x}] {name:32s}  ✗ {e}")
            failed += 1

    if client:
        client.close()
        print(f"\n✓ Mailslot 已关闭")

    print(f"\n=== 结果 ===")
    print(f"成功: {success}/{len(TEST_SEQUENCE)}")
    print(f"失败: {failed}/{len(TEST_SEQUENCE)}")

    if failed == 0 and success == len(TEST_SEQUENCE):
        print("\n✓✓✓ 全部命令写入成功！Mailslot 链路打通")
        print("    下一步: 实现 ExecCmd (cmd_code=2) 发送真实 CI-V 命令到电台")
        return 0
    else:
        print(f"\n✗ 有 {failed} 个命令失败")
        return 2


if __name__ == "__main__":
    sys.exit(main())
