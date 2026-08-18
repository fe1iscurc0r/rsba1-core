"""rsba1 — Icom RS-BA1 V2 pure-Python protocol stack.

This package provides:
- RadioLink: cross-platform UDP session manager (pure Python, no native deps)
- CI-V protocol: frame encoding/decoding, frequency/mode/PTT control
- MCP server: AI-agent integration via Model Context Protocol

Quick start:
    import os
    os.environ["RADIO_HOST"] = "192.168.0.31"
    os.environ["RADIO_USER"] = "linnan"
    os.environ["RADIO_PASSWORD"] = "secret"

    from rsba1.radio_link import RadioLink
    with RadioLink(os.environ["RADIO_HOST"],
                   os.environ["RADIO_USER"],
                   os.environ["RADIO_PASSWORD"]) as link:
        print(link.read_freq() / 1e6, "MHz")
"""
__version__ = "1.0.0"
