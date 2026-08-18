"""rsba1.mcp.__main__ — MCP 服务入口 (stdio / sse 传输)。

用法:
    # stdio 传输 (默认, 供 MCP 客户端通过子进程发现)
    python -m rsba1.mcp

    # sse 传输 (HTTP, 供远程/可视化客户端发现)
    python -m rsba1.mcp --transport sse --host 127.0.0.1 --port 8765

    # 自定义电台地址 / 查询超时
    python -m rsba1.mcp --to 0xA4 --from 0x00 --query-timeout 2000

物理前置条件 (桥接闭环):
    - RemoteUty.exe 运行中
    - read_* 闭环查询要求 RemoteController.exe 未运行
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m rsba1.mcp",
        description="IC-705 RS-BA1 MCP 服务 (Mailslot ExecCmd 桥接)",
    )
    p.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="传输方式 (默认 stdio)",
    )
    p.add_argument("--host", default="127.0.0.1", help="sse 监听地址 (默认 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="sse 监听端口 (默认 8765)")
    p.add_argument("--to", type=lambda x: int(x, 0), default=0xA4,
                   help="电台 CI-V 地址, 支持十六进制 (默认 0xA4=IC-705)")
    p.add_argument("--from", dest="from_addr",
                   type=lambda x: int(x, 0), default=0x00,
                   help="源控制器 CI-V 地址 (默认 0x00)")
    p.add_argument("--query-timeout", type=int, default=2000,
                   help="闭环查询超时 ms (默认 2000)")
    p.add_argument("--name", default="ic705-rsba1", help="MCP 服务名 (默认 ic705-rsba1)")
    return p


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)

    # 惰性导入: 未装 fastmcp 时在此给出清晰报错。
    from rsba1.mcp.server import create_server

    mcp = create_server(
        name=args.name,
        to_addr=args.to,
        from_addr=args.from_addr,
        query_timeout_ms=args.query_timeout,
    )

    if args.transport == "sse":
        print(f"[rsba1.mcp] SSE 监听 {args.host}:{args.port} ...", file=sys.stderr)
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())