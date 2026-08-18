"""rsba1.mcp — 把 CI-V 控制能力封装成 MCP (Model Context Protocol) 接口。

定位:
    陆墨 (外置 MCP 客户端) 通过 MCP tool 远程控制 IC-705 电台。
    底层复用 CivViaExecCmdSender (Mailslot ExecCmd 桥接), 让 RemoteUty
    把 CI-V 命令转发到电台, 无需 RemoteController.exe / UtyCtrl / CivCtrl。

MCP 接口层结构:
    server.py   : FastMCP 服务定义 + 各 tool 实现 (read_freq/set_freq/
                  read_mode/ptt/smeter 等)
    __main__.py : 服务入口, 支持 stdio / sse 两种传输, 供外置 MCP 客户端发现

依赖:
    fastmcp (pydantic + mcp 底层)。模块采用惰性导入, 未安装 fastmcp 时
    仅导入本模块不报错, 调用 create_server()/入口时才需要。

物理前置条件 (桥接闭环):
    - RemoteUty.exe 运行中 (Mailslot 通信底座)
    - RemoteController.exe 未运行 (否则占用 RemoteUtyCtrlRes 响应 mailslot)
    - IC-705 电台已连接并经 RemoteUty 建立会话
"""
from rsba1.mcp.server import create_server  # noqa: F401

__all__ = ["create_server"]