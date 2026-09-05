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
        export RADIO_USER=radio_user
        export RADIO_PASSWORD=change_me
        python -m rsba1.mcp

    Example (CLI args):
        python -m rsba1.mcp --host 192.168.0.31 --user radio_user --pwd change_me

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
            "  python -m rsba1.mcp --host 192.168.0.31 --user radio_user --pwd secret\n"
            "  export RADIO_USER=radio_user; export RADIO_PASSWORD=secret"
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
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
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
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
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
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
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
    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
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
    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
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
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def get_status() -> Dict[str, Any]:
        """Read all radio status in one call: frequency, mode, S-meter, and panel
        button/key states (PAMP, AGC, NB, NR, VOX, TONE, ATT, NOTCH, MONI, DUP,
        TUNER, XFC, RIT, IF filter, max TX power, etc.).

        Returns:
            dict with keys: freq_hz, mode_name, mode_code, filter, smeter, and
            panel-state keys (pamp/agc/nb/nr/vox/tone_mode/att/notch_auto/
            notch_manual/moni/duplex/tuner/xfc/rit/rit_freq/dtx/if_filter/
            max_tx_power). Any read that fails becomes None.
        """
        _ensure_link()
        link = _link["instance"]

        def _safe(fn, *args):
            try:
                return fn(*args)
            except Exception:
                return None

        from rsba1.mailslot.civ_response import MODE_NAMES

        code_filt = _safe(link.read_mode)
        return {
            "freq_hz": _safe(link.read_freq),
            "mode_code": code_filt[0] if code_filt else None,
            "mode_name": MODE_NAMES.get(code_filt[0], "UNKNOWN") if code_filt else None,
            "filter": code_filt[1] if code_filt else None,
            "smeter": _safe(link.read_smeter),
            "pamp": _safe(link.read_pamp),
            "agc": _safe(link.read_agc),
            "nb": _safe(link.read_nb),
            "nr": _safe(link.read_nr),
            "vox": _safe(link.read_vox),
            "tone_mode": _safe(link.read_tone_mode),
            "att": _safe(link.read_att),
            "notch_auto": _safe(link.read_notch_auto),
            "notch_manual": _safe(link.read_notch_manual),
            "moni": _safe(link.read_moni),
            "duplex": _safe(link.read_duplex),
            "tuner": _safe(link.read_tuner),
            "xfc": _safe(link.read_xfc),
            "tx_status": _safe(link.read_tx_status),
            "rit": _safe(link.read_rit),
            "rit_freq": _safe(link.read_rit_freq),
            "dtx": _safe(link.read_dtx),
            "if_filter": _safe(link.read_if_filter),
            "max_tx_power": _safe(link.read_max_tx_power),
        }    # ── Panel buttons / keys (write side) ────────────────────────────────────────────────

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_mode(mode: str) -> Dict[str, Any]:
        """Set the operating mode.

        Args:
            mode: "LSB" | "USB" | "AM" | "CW" | "RTTY" | "FM" | "WFM" |
                  "CW-R" | "RTTY-R" | "DV".
        """
        _ensure_link()
        codes = {"LSB": 0x00, "USB": 0x01, "AM": 0x02, "CW": 0x03, "RTTY": 0x04,
                 "FM": 0x05, "WFM": 0x06, "CW-R": 0x07, "RTTY-R": 0x08, "DV": 0x17}
        m = codes.get(str(mode).upper())
        if m is None:
            return {"success": False, "error": "unknown mode %r" % mode}
        try:
            _link["instance"].set_mode(m)
            return {"success": True, "mode": str(mode).upper()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_pamp(mode: str) -> Dict[str, Any]:
        """Set preamp (P.AMP). mode: "off" | "pamp1" | "pamp2" ("on" = pamp1)."""
        _ensure_link()
        try:
            _link["instance"].set_pamp(mode)
            return {"success": True, "pamp": mode}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_agc(mode: str) -> Dict[str, Any]:
        """Set AGC time constant. mode: "fast" | "mid" | "slow"."""
        _ensure_link()
        try:
            _link["instance"].set_agc(mode)
            return {"success": True, "agc": mode}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_nb(on: bool) -> Dict[str, Any]:
        """Set noise blanker (NB) on/off."""
        _ensure_link()
        _link["instance"].set_nb(on)
        return {"success": True, "nb": on}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_nr(on: bool) -> Dict[str, Any]:
        """Set noise reduction (NR) on/off."""
        _ensure_link()
        _link["instance"].set_nr(on)
        return {"success": True, "nr": on}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_vox(on: bool) -> Dict[str, Any]:
        """Set VOX on/off."""
        _ensure_link()
        _link["instance"].set_vox(on)
        return {"success": True, "vox": on}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_tone_mode(mode: str) -> Dict[str, Any]:
        """Set tone mode: "off" | "tone" | "tsql" | "dtcs" | "dtcs_t" |
        "tone_t_dtcs_r" | "dtcs_t_tsql_r" | "tone_t_tsql_r"."""
        _ensure_link()
        try:
            _link["instance"].set_tone_mode(mode)
            return {"success": True, "tone_mode": mode}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_tone_freq(tone_hz_x10: int, tsql: bool = False) -> Dict[str, Any]:
        """Set CTCSS tone frequency in 0.1 Hz units (e.g. 885 = 88.5 Hz).

        tsql=False -> repeater tone (0x1B 0x00); tsql=True -> tone squelch (0x1B 0x01).
        """
        _ensure_link()
        try:
            _link["instance"].set_tone_freq(tone_hz_x10, tsql=tsql)
            return {"success": True, "tone_hz_x10": tone_hz_x10, "tsql": tsql}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_att(on: bool) -> Dict[str, Any]:
        """Set 20 dB attenuator (ATT) on/off (HF/50 MHz only)."""
        _ensure_link()
        _link["instance"].set_att(on)
        return {"success": True, "att": on}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_notch(kind: str, on: bool) -> Dict[str, Any]:
        """Set notch filter on/off. kind: "auto" (auto notch, 0x16 0x41) or
        "manual" (manual notch, 0x16 0x48)."""
        _ensure_link()
        k = str(kind).lower()
        try:
            if k == "auto":
                _link["instance"].set_notch_auto(on)
            elif k == "manual":
                _link["instance"].set_notch_manual(on)
            else:
                return {"success": False, "error": "unknown notch kind %r" % kind}
            return {"success": True, "notch": k, "on": on}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_moni(on: bool) -> Dict[str, Any]:
        """Set monitor (MONI) on/off."""
        _ensure_link()
        _link["instance"].set_moni(on)
        return {"success": True, "moni": on}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_duplex(mode: str) -> Dict[str, Any]:
        """Set duplex direction: "simplex" | "dup-" | "dup+"."""
        _ensure_link()
        try:
            _link["instance"].set_duplex(mode)
            return {"success": True, "duplex": mode}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_split(on: bool) -> Dict[str, Any]:
        """Set SPLIT (dual-VFO transmit) on/off. When on, TX uses the other VFO."""
        _ensure_link()
        _link["instance"].set_split(on)
        return {"success": True, "split": on}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_tuner(on: bool) -> Dict[str, Any]:
        """Set antenna tuner on/off (IC-705 needs external AH-705)."""
        _ensure_link()
        _link["instance"].set_tuner(on)
        return {"success": True, "tuner": on}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
    def tune_now() -> Dict[str, Any]:
        """Trigger antenna tuner tuning cycle. WARNING: transmits a carrier for
        a few seconds - ensure an antenna/load is connected."""
        _ensure_link()
        _link["instance"].tune_now()
        return {"success": True, "tune": "started"}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_xfc(on: bool) -> Dict[str, Any]:
        """Set transmit-frequency check (XFC) on/off."""
        _ensure_link()
        _link["instance"].set_xfc(on)
        return {"success": True, "xfc": on}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_rit(on: bool) -> Dict[str, Any]:
        """Set receive incremental tuning (RIT) on/off."""
        _ensure_link()
        _link["instance"].set_rit(on)
        return {"success": True, "rit": on}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_rit_freq(hz: int) -> Dict[str, Any]:
        """Set RIT offset in Hz, signed (range +-9999 Hz)."""
        _ensure_link()
        try:
            _link["instance"].set_rit_freq(hz)
            return {"success": True, "rit_freq_hz": hz}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_dtx(on: bool) -> Dict[str, Any]:
        """Set delta-TX (dTX) on/off."""
        _ensure_link()
        _link["instance"].set_dtx(on)
        return {"success": True, "dtx": on}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
    def scan_start(mode: str = "programmed") -> Dict[str, Any]:
        """Start a scan. mode: "programmed_mem" | "programmed" | "df" |
        "fine_programmed" | "fine_df" | "memory" | "select_memory" | "mode_select"."""
        _ensure_link()
        try:
            _link["instance"].scan_start(mode)
            return {"success": True, "scan_mode": mode}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
    def scan_stop() -> Dict[str, Any]:
        """Cancel/stop scanning."""
        _ensure_link()
        _link["instance"].scan_stop()
        return {"success": True, "scan": "stopped"}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
    def speech(what: str = "all") -> Dict[str, Any]:
        """Voice-announce. what: "all" (everything) | "freq" (frequency+S-meter)
        | "mode". The radio speaks - mind the volume."""
        _ensure_link()
        try:
            _link["instance"].speech(what)
            return {"success": True, "speech": what}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_if_filter(idx: int) -> Dict[str, Any]:
        """Set IF filter bandwidth index (0x1A 0x03). range: SSB/CW 0~40, AM 0~49."""
        _ensure_link()
        try:
            _link["instance"].set_if_filter(idx)
            return {"success": True, "if_filter": idx}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    def set_max_tx_power(val: int) -> Dict[str, Any]:
        """Set max TX power level (battery pack: 0=0.5W 1=1W 2=2.5W 3=5W)."""
        _ensure_link()
        try:
            _link["instance"].set_max_tx_power(val)
            return {"success": True, "max_tx_power": val}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
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
    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
    def shutdown() -> str:
        """Close the radio connection cleanly. Call this before stopping the server."""
        _close_link()
        return "Connection closed."

    return mcp
