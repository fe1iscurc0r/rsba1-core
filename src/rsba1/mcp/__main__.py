"""rsba1.mcp.__main__ — MCP server entry point (stdio / SSE transport).

Usage:
    # Recommended: RadioLink backend (cross-platform, no Windows required)
    python -m rsba1.mcp

    # Or explicitly:
    python -m rsba1.mcp --backend radio-link

    # Legacy Windows Mailslot backend (requires RemoteUty.exe on Windows):
    python -m rsba1.mcp --backend mailslot

    # SSE transport for remote/HTTP access:
    python -m rsba1.mcp --transport sse --host 0.0.0.0 --port 8765

    # With credentials via CLI args (or env vars: RADIO_HOST, RADIO_USER, RADIO_PASSWORD)
    python -m rsba1.mcp --host 192.168.0.31 --user radio_user --pwd secret

Environment variables (recommended — avoids putting credentials in CLI history):
    RADIO_HOST=192.168.0.31
    RADIO_USER=radio_user
    RADIO_PASSWORD=your_password

For one-off commands without starting a server:
    python -m rsba1.mcp read-freq
    python -m rsba1.mcp set-freq 7074000
    python -m rsba1.mcp get-status
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m rsba1.mcp",
        description="IC-705 RS-BA1 MCP server (RadioLink backend — cross-platform)",
    )
    p.add_argument(
        "--backend",
        choices=["radio-link", "mailslot"],
        default="radio-link",
        dest="backend",
        help="Backend to use. 'radio-link' (default): pure Python UDP sockets, cross-platform. "
             "'mailslot': Windows-only via RemoteUty.exe.",
    )
    p.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport method (default: stdio for MCP clients)",
    )
    p.add_argument("--host", default=None, help="IC-705 IP address (or set RADIO_HOST env var)")
    p.add_argument("--user", dest="username", default=None,
                   help="RS-BA1 username (or set RADIO_USER env var)")
    p.add_argument("--pwd", dest="password", default=None,
                   help="RS-BA1 password (or set RADIO_PASSWORD env var)")
    p.add_argument("--bind-ip", default=None, help="Local bind IP for multi-NIC machines")
    p.add_argument("--to", dest="to_addr", type=lambda x: int(x, 0), default=0xA4,
                   help="Radio CI-V address in hex (default: 0xA4 = IC-705)")
    p.add_argument("--from", dest="from_addr", type=lambda x: int(x, 0), default=0x00,
                   help="Controller CI-V address in hex (default: 0x00)")
    p.add_argument("--name", default="ic705-rsba1", help="MCP server name (default: ic705-rsba1)")
    p.add_argument("--port", type=int, default=8765, help="SSE port (default: 8765)")
    p.add_argument(
        "command",
        nargs="?",
        choices=["read-freq", "set-freq", "read-mode", "read-smeter", "ptt", "get-status", "help"],
        help="One-shot command (starts server if omitted)",
    )
    p.add_argument("cmd_args", nargs="*", help="Arguments for the one-shot command")
    return p


def _resolve_setting(name: str, cli_val: Optional[str], env_var: str) -> str:
    val = (cli_val or "").strip()
    if val:
        return val
    env_val = os.environ.get(env_var, "").strip()
    if env_val:
        return env_val
    return ""


def main(argv: Optional[list] = None) -> int:
    import os as _os
    args = _build_parser().parse_args(argv)

    # ── One-shot command mode ────────────────────────────────────────────────
    if args.command and args.command != "help":
        from rsba1.mcp.radio_link_server import create_radio_link_server
        from rsba1.radio_link import RadioLink, RadioTimeoutError
        from rsba1.mailslot.civ_response import MODE_NAMES

        host = _resolve_setting("host", args.host, "RADIO_HOST")
        user = _resolve_setting("user", args.username, "RADIO_USER")
        pwd = _resolve_setting("pwd", args.password, "RADIO_PASSWORD")

        if not host or not user or not pwd:
            print("Error: host, user, and password required.", file=sys.stderr)
            print("Pass --host/--user/--pwd or set RADIO_HOST/RADIO_USER/RADIO_PASSWORD", file=sys.stderr)
            return 1

        kwargs = {"host": host, "username": user, "password": pwd}
        if args.bind_ip:
            kwargs["bind_ip"] = args.bind_ip

        link = RadioLink(**kwargs)
        try:
            link.open()
            orig_freq = link.read_freq()

            if args.command == "read-freq":
                freq = link.read_freq()
                print(f"{freq / 1e6:.6f} MHz")
                return 0

            elif args.command == "read-mode":
                code, filt = link.read_mode()
                print(MODE_NAMES.get(code, f"UNKNOWN({code:#x})"))
                return 0

            elif args.command == "read-smeter":
                print(link.read_smeter())
                return 0

            elif args.command == "set-freq":
                if not args.cmd_args:
                    print("Usage: read-freq [hz]", file=sys.stderr)
                    return 1
                hz = int(args.cmd_args[0])
                from rsba1.ctypes_wrappers import civ_commands as civcmd
                try:
                    civcmd.assert_allowed_freq(hz)
                except ValueError as e:
                    print(f"Frequency out of amateur band: {e}", file=sys.stderr)
                    return 1
                link.set_freq(hz)
                print(f"Set to {hz} Hz ({hz / 1e6:.6f} MHz)")
                return 0

            elif args.command == "ptt":
                if not args.cmd_args:
                    print("Usage: ptt tx|rx", file=sys.stderr)
                    return 1
                on = args.cmd_args[0].lower() in ("tx", "on")
                link.ptt(on)
                print(f"PTT {'TX' if on else 'RX'}")
                return 0

            elif args.command == "get-status":
                status = {}
                try:
                    status["freq"] = link.read_freq()
                except Exception:
                    status["freq"] = "error"
                try:
                    code, filt = link.read_mode()
                    status["mode"] = MODE_NAMES.get(code, f"UNKNOWN({code:#x})")
                except Exception:
                    status["mode"] = "error"
                try:
                    status["smeter"] = link.read_smeter()
                except Exception:
                    status["smeter"] = "error"
                print(f"Freq: {status['freq']} Hz | Mode: {status['mode']} | S-meter: {status['smeter']}")
                return 0
        finally:
            try:
                link.close()
            except Exception:
                pass
        return 0

    # ── Server mode ───────────────────────────────────────────────────────────
    if args.command == "help":
        _build_parser().print_help()
        return 0

    if args.backend == "radio-link":
        from rsba1.mcp.radio_link_server import create_radio_link_server
        mcp = create_radio_link_server(
            host=args.host,
            username=args.username,
            password=args.password,
            bind_ip=args.bind_ip,
            name=args.name,
        )
    else:
        print("Mailslot backend is deprecated. Use --backend radio-link (default).", file=sys.stderr)
        return 1

    if args.transport == "sse":
        print(f"[rsba1.mcp] SSE listening on {args.host or '0.0.0.0'}:{args.port} ...", file=sys.stderr)
        mcp.settings.host = args.host or "0.0.0.0"
        mcp.settings.port = args.port
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    import os
    sys.exit(main())
