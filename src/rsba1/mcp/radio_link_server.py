"""rsba1.mcp.radio_link_server — Cross-platform MCP server backed by RadioLink.

This is the PRIMARY MCP server implementation. It uses RadioLink (pure Python UDP sockets)
to connect directly to the IC-705's built-in RS-BA1 server — no Windows Mailslot,
no RemoteUty.exe, no DLLs required.

Use this on: Windows, Linux, macOS, anything with Python + network access to the radio.

The older mcp/server.py (Mailslot-based) is Windows-only and kept for reference only.
"""
from __future__ import annotations

REQUIRES_FASTMCP = """\
rsba1 MCP server requires fastmcp. Install it with:
    pip install rsba1-core[mcp]
"""

from typing import Any, Dict, Optional
import os


def _get_fastmcp():
    try:
        from fastmcp import FastMCP
    except ImportError:
        raise ImportError(REQUIRES_FASTMCP)
    return FastMCP


# ---------------------------------------------------------------------------
# RadioLink-backed MCP tools
# ---------------------------------------------------------------------------


def create_radio_link_server(
    host: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    *,
    to_addr: int = 0xA4,
    from_addr: int = 0x00,
    bind_ip: Optional[str] = None,
    name: str = "ic705-rsba1",
    # backward compat
    query_timeout_ms: int = 2000,
) -> Any:
    """Create a FastMCP server backed by RadioLink.

    Args:
        host: IC-705 IP address. Can also be set via RADIO_HOST env var.
        username: RS-BA1 username (set on radio: MENU → WLAN → Remote). Can be
            set via RADIO_USER env var.
        password: RS-BA1 password. Can be set via RADIO_PASSWORD env var.
        to_addr: CI-V address of the radio (default 0xA4 = IC-705).
        from_addr: Controller CI-V address (default 0x00).
        bind_ip: Source IP to bind for multi-NIC machines. Defaults to auto-detect.
        name: MCP server name.
        query_timeout_ms: Query timeout in milliseconds (unused, kept for compat).

    Environment variables:
        RADIO_HOST     — IC-705 IP address
        RADIO_USER     — RS-BA1 username
        RADIO_PASSWORD — RS-BA1 password
        RADIO_BIND_IP  — Local bind IP (optional)

    Example (environment variables):
        export RADIO_HOST=192.168.0.31
        export RADIO_USER=linnan
        export RADIO_PASSWORD=shenyaodiyi
        python -m rsba1.mcp

    Example (CLI args):
        python -m rsba1.mcp --host 192.168.0.31 --user linnan --pwd shenyaodiyi

    Physical prerequisites:
        - IC-705 powered on, RS-BA1 Server Function enabled (MENU → SET → WLAN)
        - Network connectivity to the radio (ping test recommended first)
        - Valid RS-BA1 credentials configured on the radio
    """
    FastMCP = _get_fastmcp()

    # Resolve settings: CLI arg > env var > error
    _host = (host or os.environ.get("RADIO_HOST") or "").strip()
    _user = (username or os.environ.get("RADIO_USER") or "").strip()
    _pwd = (password or os.environ.get("RADIO_PASSWORD") or "").strip()
    _bind = bind_ip or os.environ.get("RADIO_BIND_IP") or ""

    if not _host:
        raise ValueError(
            "Radio host not set. Pass --host or set RADIO_HOST env var.\n"
            "  python -m rsba1.mcp --host 192.168.0.31\n"
            "  export RADIO_HOST=192.168.0.31"
        )
    if not _user or not _pwd:
        raise ValueError(
            "Radio credentials not set. Pass --user/--pwd or set RADIO_USER/RADIO_PASSWORD.\n"
            "  python -m rsba1.mcp --host 192.168.0.31 --user linnan --pwd secret\n"
            "  export RADIO_USER=linnan; export RADIO_PASSWORD=secret"
        )

    # Import lazily to avoid import errors when only installing deps
    from rsba1.radio_link import RadioLink, RadioTimeoutError
    from rsba1.ctypes_wrappers import civ_commands as civcmd

    # Shared RadioLink session (opened once, reused across all tool calls)
    _link: Dict[str, Any] = {"instance": None}

    def _ensure_link():
        if _link["instance"] is None:
            kwargs = {"host": _host, "username": _user, "password": _pwd}
            if _bind:
                kwargs["bind_ip"] = _bind
            _link["instance"] = RadioLink(**kwargs)
            _link["instance"].open()
            # Store original freq so --restore works
            _link["orig_freq"] = _link["instance"].read_freq()

    def _close_link():
        if _link["instance"] is not None:
            try:
                _link["instance"].close()
            except Exception:
                pass
            _link["instance"] = None

    mcp = FastMCP(name)

    # ── read_freq ────────────────────────────────────────────────────────────
    @mcp.tool()
    def read_freq() -> int:
        """Read the radio's current VFO frequency in Hz.

        Returns:
            int: Frequency in Hz (e.g. 14270000 = 14.270 MHz).

        Raises:
            TimeoutError: No response from radio.
            RadioAuthError: Authentication failed (check credentials).
        """
        _ensure_link()
        try:
            return _link["instance"].read_freq()
        except RadioTimeoutError as e:
            raise TimeoutError(f"Radio not responding: {e}") from e

    # ── read_mode ───────────────────────────────────────────────────────────
    @mcp.tool()
    def read_mode() -> Dict[str, Any]:
        """Read the radio's current mode and filter.

        Returns:
            dict with keys: mode_code (int), mode_name (str: LSB/USB/AM/CW/FM/WFM),
            filter (int).
        """
        _ensure_link()
        try:
            code, filt = _link["instance"].read_mode()
        except RadioTimeoutError as e:
            raise TimeoutError(f"Radio not responding: {e}") from e

        from rsba1.mailslot.civ_response import MODE_NAMES
        return {
            "mode_code": code,
            "mode_name": MODE_NAMES.get(code, "UNKNOWN"),
            "filter": filt,
        }

    # ── read_smeter ─────────────────────────────────────────────────────────
    @mcp.tool()
    def read_smeter() -> int:
        """Read the radio's S-meter value.

        Returns:
            int: Raw S-meter byte (0-255). See S-table to convert to dB/S-units.
        """
        _ensure_link()
        try:
            return _link["instance"].read_smeter()
        except RadioTimeoutError as e:
            raise TimeoutError(f"Radio not responding: {e}") from e

    # ── set_freq ─────────────────────────────────────────────────────────────
    @mcp.tool()
    def set_freq(hz: int) -> Dict[str, Any]:
        """Set the radio VFO frequency in Hz.

        Safety: Only amateur bands are allowed. Out-of-band frequencies raise ValueError.

        Args:
            hz: Frequency in Hz, e.g. 7074000 = 7.074 MHz.

        Returns:
            dict with keys: success (bool), freq_hz (int), mode (str).
        """
        _ensure_link()
        try:
            civcmd.assert_allowed_freq(hz)
        except ValueError as e:
            return {"success": False, "error": str(e), "freq_hz": None}
        try:
            _link["instance"].set_freq(hz)
            code, filt = _link["instance"].read_mode()
            from rsba1.mailslot.civ_response import MODE_NAMES
            return {
                "success": True,
                "freq_hz": hz,
                "mode": MODE_NAMES.get(code, "UNKNOWN"),
            }
        except RadioTimeoutError as e:
            return {"success": False, "error": f"Radio timeout: {e}", "freq_hz": None}

    # ── ptt ─────────────────────────────────────────────────────────────────
    @mcp.tool()
    def ptt(state: str) -> Dict[str, str]:
        """Control PTT (push-to-talk / TX/RX).

        Args:
            state: "tx" or "on" to transmit; "rx" or "off" to receive.

        Returns:
            dict with keys: success (bool), state (str).

        WARNING: This will cause the radio to transmit. Ensure antenna is connected!
        """
        _ensure_link()
        on = str(state).lower() in ("tx", "on", "true", "1")
        try:
            _link["instance"].ptt(on)
            return {"success": True, "state": "TX" if on else "RX"}
        except Exception as e:
            return {"success": False, "state": None, "error": str(e)}

    # ── get_status ──────────────────────────────────────────────────────────
    @mcp.tool()
    def get_status() -> Dict[str, Any]:
        """Read all radio status in one call: frequency, mode, S-meter.

        Returns:
            dict with keys: freq_hz (int or None), mode_name (str),
            mode_code (int), filter (int), smeter (int or None).
        """
        _ensure_link()
        status: Dict[str, Any] = {
            "freq_hz": None, "mode_name": None,
            "mode_code": None, "filter": None, "smeter": None,
        }
        try:
            status["freq_hz"] = _link["instance"].read_freq()
        except Exception:
            pass
        try:
            code, filt = _link["instance"].read_mode()
            from rsba1.mailslot.civ_response import MODE_NAMES
            status["mode_code"] = code
            status["mode_name"] = MODE_NAMES.get(code, "UNKNOWN")
            status["filter"] = filt
        except Exception:
            pass
        try:
            status["smeter"] = _link["instance"].read_smeter()
        except Exception:
            pass
        return status

    # ── restore_freq ────────────────────────────────────────────────────────
    @mcp.tool()
    def restore_freq() -> Dict[str, Any]:
        """Restore the original frequency captured when the MCP server started.

        Useful to reset after a test that changed frequency.

        Returns:
            dict with keys: success (bool), freq_hz (int or None).
        """
        _ensure_link()
        orig = _link.get("orig_freq")
        if orig is None:
            return {"success": False, "error": "No original frequency recorded"}
        try:
            _link["instance"].set_freq(orig)
            return {"success": True, "freq_hz": orig}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── shutdown ────────────────────────────────────────────────────────────
    @mcp.tool()
    def shutdown() -> str:
        """Close the radio connection cleanly. Call this before stopping the server."""
        _close_link()
        return "Connection closed."

    return mcp
