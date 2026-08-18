"""send_command_login — Command 信道 (UDP 50001) 登录/心跳验证命令行工具.

通过 CommandClient 向运行 RemoteUty.exe 的服务器 UDP 50001 端口发送
ConnectServer 请求完成认证, 并可发送 KeepAlive 心跳。

子命令:
    connect   发送 ConnectServer, 打印认证结果与会话标识
    keepalive 发送 KeepAlive 心跳
    loop      循环 connect + keepalive (验证会话保持)

前置条件:
    服务器 (运行 RemoteUty.exe) 可达, Command 信道 UDP 50001 开放。

用法:
    cd d:\\my git\\rs-ba1-reverse
    python scripts\\send_command_login.py --host 192.168.1.10 --user alice --pass secret connect
    python scripts\\send_command_login.py --host 192.168.1.10 --user alice --pass secret keepalive
    python scripts\\send_command_login.py -v --host 192.168.1.10 --user alice --pass secret connect

注意:
    - Command 信道的 header 布局为静态推断 (见 command_client.py 存疑项),
      若服务器无响应, 需抓包复核 totalLen/version 偏移与 field_8/field_C 字节序。
"""

# ---------------------------------------------------------------------------
# ⚠️ 已废弃 (2026-08-18): 本脚本基于旧 command_client API (CommandClient /
# build_connect_request), 该 API 已被真机验证的新链路取代并删除。
# 替代入口:
#   - 全流程认证探测:  python tools\probe_command_connect.py
#   - 端到端 CI-V 闭环: python scripts\e2e_civ_loop.py
#   - 可复用库:        src/rsba1/radio_link.py (RadioLink)
# 协议定案见 re/protocols/command_channel_cmd.md §4.2。
# ---------------------------------------------------------------------------
import sys as _sys
print(__doc__ or "", flush=True)
print("⚠️ 本脚本已废弃 (2026-08-18), 请改用 tools/probe_command_connect.py 或 "
      "scripts/e2e_civ_loop.py (协议定案见 command_channel_cmd.md §4.2)。", flush=True)
_sys.exit(2)
