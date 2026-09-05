"""e2e_civ_loop.py — End-to-end RS-BA1 CI-V loopback test.

Usage:
    # With credentials as CLI args
    python scripts/e2e_civ_loop.py --host 192.168.0.31 --user radio_user --pwd secret

    # With environment variables (recommended — keeps passwords out of shell history)
    export RADIO_HOST=192.168.0.31
    export RADIO_USER=radio_user
    export RADIO_PASSWORD=secret
    python scripts/e2e_civ_loop.py

    # Dry run (validates params and band whitelist without connecting)
    python scripts/e2e_civ_loop.py --dry-run

    # Set frequency roundtrip test
    python scripts/e2e_civ_loop.py --host 192.168.0.31 --user radio_user --pwd secret --set-freq 145000000

    # PTT test (WARNING: will transmit!)
    python scripts/e2e_civ_loop.py --host 192.168.0.31 --user radio_user --pwd secret --ptt

Prerequisites:
    1. IC-705 powered on, RS-BA1 Server Function enabled (MENU → SET → WLAN)
    2. Valid RS-BA1 username/password configured on the radio
    3. Network connectivity (run: ping <host> first)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rsba1.radio_link import (
    RadioLink,
    RadioAuthError,
    RadioLinkError,
    RadioTimeoutError,
)
from rsba1.ctypes_wrappers import civ_commands as civcmd


def main() -> int:
    p = argparse.ArgumentParser(
        description="E2E: RS-BA1 full链路 CI-V 闭环",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host", default=os.environ.get("RADIO_HOST", "").strip(),
                   help="IC-705 (RS-BA1 Server) IP")
    p.add_argument("--user", default=os.environ.get("RADIO_USER", "").strip(),
                   help="Radio RS-BA1 username (or RADIO_USER env)")
    p.add_argument("--pwd", default=os.environ.get("RADIO_PASSWORD", "").strip(),
                   help="Radio RS-BA1 password (or RADIO_PASSWORD env)")
    p.add_argument("--bind-ip", default=os.environ.get("RADIO_BIND_IP", "").strip(),
                   help="Source IP for multi-NIC machines (or RADIO_BIND_IP env)")
    p.add_argument("--iterations", type=int, default=3,
                   help="read_freq loop count (default: 3)")
    p.add_argument("--read-mode", action="store_true", default=True,
                   help="Also loop read_mode (default: on)")
    p.add_argument("--set-freq", type=int, default=None,
                   help="Roundtrip test: set this frequency (Hz, must be in amateur band), then restore")
    p.add_argument("--ptt", action="store_true",
                   help="PTT ON 1s → OFF (WARNING: actually transmits!)")
    p.add_argument("--timeout", type=float, default=2.0,
                   help="Per-packet timeout in seconds (default: 2.0)")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate params and band whitelist without connecting")
    args = p.parse_args()

    # Validate required args
    missing = []
    if not args.host:
        missing.append("--host / RADIO_HOST")
    if not args.user:
        missing.append("--user / RADIO_USER")
    if not args.pwd:
        missing.append("--pwd / RADIO_PASSWORD")
    if missing:
        print(f"Error: missing required args: {', '.join(missing)}", file=sys.stderr)
        print("Pass --host/--user/--pwd or set RADIO_HOST/RADIO_USER/RADIO_PASSWORD env vars.", file=sys.stderr)
        return 1

    print(f"=== E2E: RS-BA1 CI-V loopback {args.host} (user={args.user}) "
          f"{'(DRY-RUN)' if args.dry_run else ''} ===")

    if args.set_freq is not None:
        try:
            civcmd.assert_allowed_freq(args.set_freq)
        except ValueError as e:
            print(f"✗ Frequency out of amateur band: {e}")
            return 10

    if args.dry_run:
        print("DRY-RUN: params and band whitelist OK, not connecting.")
        return 0

    try:
        kwargs = {"host": args.host, "username": args.user, "password": args.pwd,
                  "verbose": True}
        if args.bind_ip:
            kwargs["bind_ip"] = args.bind_ip
        with RadioLink(**kwargs) as link:
            link.open()
            orig_freq = link.read_freq()
            print(f"\n[0] Original frequency: {orig_freq / 1e6:.6f} MHz")

            # ── Stage 1: read_freq loop ─────────────────────────────────────────
            print(f"\n[1] read_freq loop ({args.iterations}x)")
            freqs = []
            for i in range(args.iterations):
                freq = link.read_freq(timeout=args.timeout)
                freqs.append(freq)
                mode, filt = link.read_mode(timeout=args.timeout) if args.read_mode else (None, None)
                mode_name = _MODE_NAMES.get(mode, f"{mode:#x}") if mode is not None else "N/A"
                print(f"  {i+1}. {freq / 1e6:.6f} MHz  mode={mode_name}")
            if len(set(freqs)) == 1:
                print(f"  ✓ All {args.iterations} reads stable at {freqs[0] / 1e6:.6f} MHz")
            else:
                print(f"  ✗ Inconsistent reads: {freqs}")
                return 2

            # ── Stage 2: set_freq roundtrip ────────────────────────────────────
            if args.set_freq is not None:
                print(f"\n[2] set_freq roundtrip → {args.set_freq} Hz ({args.set_freq / 1e6:.6f} MHz)")
                link.set_freq(args.set_freq)
                time.sleep(0.5)
                read_back = link.read_freq(timeout=args.timeout)
                if read_back == args.set_freq:
                    print(f"  ✓ Wrote {args.set_freq}, read back {read_back} — match!")
                else:
                    print(f"  ✗ Wrote {args.set_freq}, read back {read_back} — MISMATCH")
                    return 3
                print(f"\n[3] Restoring original frequency: {orig_freq / 1e6:.6f} MHz")
                link.set_freq(orig_freq)

            # ── Stage 3: PTT test ──────────────────────────────────────────────
            if args.ptt:
                print("\n[4] PTT TX 1s → RX")
                link.ptt(True)
                time.sleep(1.0)
                link.ptt(False)
                print("  ✓ PTT TX→RX OK")

            print("\n=== PASS: all stages OK ===")
            return 0

    except RadioAuthError as e:
        print(f"\n✗ Auth failed: {e}", file=sys.stderr)
        print("  Check --user / --pwd credentials match the radio's RS-BA1 settings.", file=sys.stderr)
        return 4
    except RadioTimeoutError as e:
        print(f"\n✗ Timeout: {e}", file=sys.stderr)
        print("  Check: (1) radio is reachable at --host, (2) RS-BA1 Server is ON, (3) no firewall blocking.", file=sys.stderr)
        return 5
    except RadioLinkError as e:
        print(f"\n✗ Link error: {e}", file=sys.stderr)
        return 6
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 99


_MODE_NAMES = {
    0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW",
    0x04: "NFM", 0x05: "WFM", 0x06: "CW-R",
}


if __name__ == "__main__":
    sys.exit(main())
