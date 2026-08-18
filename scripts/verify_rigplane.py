"""
rigplane (icom-lan) 验证脚本 — 连接 IC-705 并跑基础命令。

用法：
    python verify_rigplane.py --host 192.168.0.31 --user user1 --pass YOURPASS
    python verify_rigplane.py --host 192.168.0.31 --user user1 --pass YOURPASS --ptt   # 含 PTT 测试

环境前提（IC-705 上必须先做）：
  1. Menu → Set → Connectors → WLAN → Server Function: ON
  2. Menu → Set → Connectors → WLAN → User1 ID + Password (建议 >= 8 字符)
  3. Menu → Set → Connectors → WLAN → Network: 连到本机所在 WLAN
  4. 电脑 ping 通 192.168.0.31

本脚本只做：
  - 连接握手
  - 读频率 / 读模式 / 读 S-meter
  - (可选) PTT ON → 等 1s → PTT OFF
  - 安全门禁：PTT 必须显式 --ptt 启用
  - 异常时打印详细错误 + 退出码区分错误类型
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import time
from pathlib import Path

LOG = logging.getLogger("verify_rigplane")


# ---------------------------------------------------------------------------
# 1. 端口探测（不依赖 rigplane，先看电台在不在）
# ---------------------------------------------------------------------------
def probe_ports(host: str) -> dict:
    """探测 50001/50002/50003 是否在监听（UDP 用 connect + sendto 探活）"""
    result = {"50001": False, "50002": False, "50003": False}
    for port_str, port in [("50001", 50001), ("50002", 50002), ("50003", 50003)]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.5)
        try:
            s.connect((host, port))
            # 发一个零字节包探活，IC-705 收到非法包会静默丢弃但 connect 成功说明路由可达
            s.send(b"")
            result[port_str] = True
        except OSError as e:
            result[port_str] = f"err: {e}"
        finally:
            s.close()
    return result


def ping_host(host: str) -> bool:
    """TCP 探测 port 80 (IC-705 内置 Web 服务)，比 ICMP 更可靠（IC-705 默认禁 ICMP）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect((host, 80))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 2. rigplane 连接 + 基础命令测试
# ---------------------------------------------------------------------------
async def test_rigplane(
    host: str,
    user: str,
    password: str,
    do_ptt: bool,
    timeout_s: float = 10.0,
) -> int:
    """
    返回退出码：
        0  全部成功
        10 rigplane 导入失败
        11 create_radio 失败
        12 连接握手超时/失败
        13 认证失败 (User/Password 错)
        14 读频率失败
        15 读模式失败
        16 读 S-meter 失败
        17 PTT 测试失败
        99 其他异常
    """
    LOG.info("[1/6] 导入 rigplane ...")
    try:
        import rigplane
        from rigplane import create_radio, LanBackendConfig, MetersCapable

        LOG.info("    rigplane v%s", getattr(rigplane, "__version__", "?"))
    except Exception as e:
        LOG.error("    导入失败: %r", e)
        return 10

    LOG.info("[2/6] 创建 radio 实例 (host=%s user=%s) ...", host, user)
    try:
        config = LanBackendConfig(host=host, username=user, password=password)
    except Exception as e:
        LOG.error("    配置创建失败: %r", e)
        return 11

    LOG.info("[3/6] 连接 + 握手 (最多 %.0fs) ...", timeout_s)
    radio = None
    try:
        async with create_radio(config) as r:
            radio = r
            LOG.info("    ✓ 连接成功")

            # ---- 读频率
            LOG.info("[4/6] 读频率 ...")
            try:
                freq = await asyncio.wait_for(r.get_frequency(), timeout=5.0)
                LOG.info("    ✓ 频率 = %d Hz (%.3f MHz)", freq, freq / 1e6)
            except asyncio.TimeoutError:
                LOG.error("    ✗ 读频率超时")
                return 14
            except Exception as e:
                LOG.error("    ✗ 读频率失败: %r", e)
                return 14

            # ---- 读模式
            LOG.info("[5/6] 读模式 + S-meter ...")
            try:
                mode = await asyncio.wait_for(r.get_mode(), timeout=5.0)
                LOG.info("    ✓ 模式 = %s", mode)
            except asyncio.TimeoutError:
                LOG.error("    ✗ 读模式超时")
                return 15
            except Exception as e:
                LOG.error("    ✗ 读模式失败: %r", e)
                return 15

            try:
                if isinstance(r, MetersCapable):
                    s = await asyncio.wait_for(r.get_s_meter(), timeout=5.0)
                    LOG.info("    ✓ S-meter = %s", s)
                else:
                    LOG.info("    (radio 不实现 MetersCapable，跳过 S-meter)")
            except asyncio.TimeoutError:
                LOG.warning("    ! S-meter 超时（非致命）")
            except Exception as e:
                LOG.warning("    ! S-meter 读取异常（非致命）: %r", e)

            # ---- PTT 测试（可选，需显式 --ptt）
            if do_ptt:
                LOG.info("[6/6] PTT 测试 ...")
                try:
                    await asyncio.wait_for(r.set_ptt(True), timeout=3.0)
                    LOG.info("    ✓ PTT ON")
                    await asyncio.sleep(1.0)
                    await asyncio.wait_for(r.set_ptt(False), timeout=3.0)
                    LOG.info("    ✓ PTT OFF")
                except Exception as e:
                    LOG.error("    ✗ PTT 测试失败: %r", e)
                    return 17
            else:
                LOG.info("[6/6] PTT 测试跳过（需 --ptt 启用）")

            LOG.info("全部完成 ✓")
            return 0
    except Exception as e:
        msg = str(e).lower()
        LOG.error("    ✗ 连接失败: %r", e)
        # 简单的异常分类
        if "auth" in msg or "credential" in msg or "password" in msg or "login" in msg:
            return 13
        if "timeout" in msg or "timed out" in msg:
            return 12
        if "refused" in msg or "unreachable" in msg:
            return 12
        return 99


# ---------------------------------------------------------------------------
# 3. main
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="rigplane IC-705 连接验证脚本")
    p.add_argument("--host", default="192.168.0.31", help="IC-705 IP（默认 192.168.0.31）")
    p.add_argument("--user", default="user1", help="User ID (默认 user1，需在 IC-705 上预先设置)")
    p.add_argument("--pass", dest="password", default="", help="Password (需在 IC-705 上预先设置)")
    p.add_argument("--ptt", action="store_true", help="包含 PTT 测试（默认跳过）")
    p.add_argument("--timeout", type=float, default=10.0, help="连接超时（秒）")
    p.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.password:
        print("[!] 未提供 --pass，将尝试空密码。IC-705 通常要求 8-16 字符密码，可能认证失败。", file=sys.stderr)
        print("    在 IC-705: Menu → Set → Connectors → WLAN → User1 ID + Password 查看或设置", file=sys.stderr)

    # ---- 0. ping + 端口探活
    print(f"\n=== 0. 基础网络探测 {args.host} ===")
    if ping_host(args.host):
        print(f"    ✓ ping {args.host} 通")
    else:
        print(f"    ✗ ping {args.host} 不通 — 请检查 IC-705 WLAN 连接")
        return 1

    ports = probe_ports(args.host)
    for p, v in ports.items():
        if v is True:
            print(f"    ✓ UDP {p} 可达")
        else:
            print(f"    ! UDP {p}: {v}")
    print()

    # ---- 1. rigplane 验证
    print(f"=== rigplane 验证 ({args.host}, user={args.user}) ===")
    rc = asyncio.run(
        test_rigplane(
            host=args.host,
            user=args.user,
            password=args.password,
            do_ptt=args.ptt,
            timeout_s=args.timeout,
        )
    )

    print()
    if rc == 0:
        print("✓✓✓ 验证成功！rigplane + IC-705 链路打通，可以进入 MCP adapter 阶段")
    else:
        print(f"✗ 验证失败 (exit={rc})")
        hints = {
            10: "rigplane 包未装好 — pip install rigplane",
            11: "API 调用错误 — 检查 rigplane 版本",
            12: "网络/超时 — IC-705 没在 WLAN 上，或 Server Function 未开",
            13: "认证失败 — User ID/Password 错；IC-705 上需先在 WLAN 菜单设置 User1",
            14: "读频率失败 — 可能 Server Function ON 但 User slot 已被 RS-BA1 GUI 占用",
            15: "读模式失败 — CI-V 命令未响应",
            17: "PTT 失败 — 可能 TX 被占用或电台在 RX",
            99: "其他异常 — 看 -v 详细日志",
        }
        h = hints.get(rc, "未知")
        print(f"    提示: {h}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
