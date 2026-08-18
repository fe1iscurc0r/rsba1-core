"""rsba1.mcp — MCP (Model Context Protocol) server for IC-705 control.

This package exposes rsba1-core's radio control capabilities as MCP tools,
usable by any MCP-compatible AI client.

Recommended backend: radio_link_server.py (pure Python UDP, cross-platform).
Deprecated: _server_mailslot_ref.py (Windows-only, kept for reference).
"""
from rsba1.mcp.radio_link_server import create_radio_link_server as create_server

__all__ = ["create_server"]
