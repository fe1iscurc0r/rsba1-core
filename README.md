# rsba1-core

**Pure Python implementation of the Icom RS-BA1 V2 protocol stack — control IC-705, IC-9700 and any CI-V transceiver over the network. No Icom binaries required.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-green.svg)](https://python.org)
[![CI](https://github.com/fe1iscurc0r/rsba1-core/actions/workflows/ci.yml/badge.svg)](https://github.com/fe1iscurc0r/rsba1-core/actions)

## What is this?

Icom's RS-BA1 software lets you remote-control Icom transceivers over LAN/Internet. It's Windows-only and closed-source. This project reverse-engineers the RS-BA1 protocol and provides a **pure Python replacement** that runs anywhere Python runs.

You get:
- A pure Python CI-V protocol stack (no DLLs, no Windows)
- An MCP server for AI agent integration — "tune to 7.074 MHz" in natural language
- A clean CLI for scripting
- Cross-platform: Windows, Linux, macOS, Raspberry Pi

Tested on: **IC-705** (real hardware, 2026-08-18 E2E verified)

## Quick Start

### 1. Prerequisites

- Python 3.11 or later
- IC-705 with RS-BA1 Server enabled (`MENU → SET → WLAN Set → Remote Settings → Remote Server → ON`)
- IC-705 IP address and RS-BA1 username/password (set in the radio's remote settings)

### 2. Install

```bash
pip install rsba1-core
```

Or from source:

```bash
git clone https://github.com/fe1iscurc0r/rsba1-core.git
cd rsba1-core
pip install -e ".[all]"
```

### 3. Run

```bash
# Set credentials as environment variables
export RADIO_HOST=192.168.0.31
export RADIO_USER=linnan
export RADIO_PASSWORD=your_password

# One-shot command (no server needed)
python -m rsba1.mcp read-freq

# Or start the MCP server for AI agent integration
python -m rsba1.mcp
```

### 4. One-command E2E test

```bash
python scripts/e2e_civ_loop.py \
  --host 192.168.0.31 \
  --user linnan \
  --pwd your_password
```

Expected output:
```
=== E2E: RS-BA1 CI-V loopback 192.168.0.31 (user=linnan) ===
[0] Original frequency: 144.920000 MHz
[1] read_freq loop (3x)
  1. 144.920000 MHz  mode=FM
  2. 144.920000 MHz  mode=FM
  3. 144.920000 MHz  mode=FM
  ✓ All 3 reads stable at 144.920000 MHz
=== PASS: all stages OK ===
```

## MCP Server

The MCP server exposes structured tools to any MCP-compatible AI client (Claude Desktop, Cursor, etc.).

### AI Agent Integration

```bash
# Configure your AI client to use rsba1-core as an MCP tool.
# Example ~/.config/claude-code/mcp_settings.json:
{
  "mcpServers": {
    "ic705": {
      "command": "python",
      "args": ["-m", "rsba1.mcp"],
      "env": {
        "RADIO_HOST": "192.168.0.31",
        "RADIO_USER": "linnan",
        "RADIO_PASSWORD": "your_password"
      }
    }
  }
}
```

Then in your AI assistant:
> "Tune the radio to 7.074 MHz and tell me the current mode"
> "What's the S-meter reading right now?"
> "Set the radio to 145 MHz FM and then read back the frequency"

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `read_freq` | Current VFO frequency in Hz |
| `read_mode` | Mode (LSB/USB/AM/CW/FM/WFM) and filter |
| `read_smeter` | S-meter raw value (0-255) |
| `set_freq` | Set frequency in Hz (amateur bands only) |
| `ptt` | Push-to-talk: `{"state":"tx"}` or `{"state":"rx"}` |
| `get_status` | All of the above in one call |
| `restore_freq` | Reset to frequency when MCP server started |
| `shutdown` | Close radio connection cleanly |

## CLI Reference

```bash
# Read frequency
python -m rsba1.mcp read-freq

# Set frequency (amateur band enforcement active)
python -m rsba1.mcp set-freq 7074000

# Read S-meter
python -m rsba1.mcp read-smeter

# PTT (WARNING: transmits!)
python -m rsba1.mcp ptt tx
python -m rsba1.mcp ptt rx

# E2E loop test
python scripts/e2e_civ_loop.py \
  --host 192.168.0.31 --user linnan --pwd your_password

# Dry run (validates setup without connecting)
python scripts/e2e_civ_loop.py --dry-run
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Your App / AI Agent                                    │
│  (MCP client / CLI / Python import)                    │
└───────────────────────┬─────────────────────────────────┘
                        │ stdio / network
┌───────────────────────▼─────────────────────────────────┐
│  rsba1-core (pure Python, no native dependencies)       │
│                                                         │
│  radio_link.py — UDP socket session manager             │
│    ├── Command channel (UDP 50001): login / auth        │
│    └── Serial channel (UDP 50002): CI-V tunnel          │
│                                                         │
│  serial_codec.py — wire format encoding                  │
│  civ_commands.py — CI-V frame construction               │
└───────────────────────┬─────────────────────────────────┘
                        │ UDP (stdlib socket only)
┌───────────────────────▼─────────────────────────────────┐
│  IC-705 built-in RS-BA1 server (or RemoteUty.exe)     │
└─────────────────────────────────────────────────────────┘
```

Key design decisions:
- **No Windows APIs**: uses only Python stdlib `socket` — runs on Linux/macOS/Windows
- **No Icom binaries**: fully open-source protocol implementation
- **RadioLink session reuse**: one UDP session per server instance, not per call
- **Amateur band whitelist**: out-of-band frequencies are rejected before transmission

## Frequency Bands (whitelist)

Setting frequencies outside these bands is rejected by `set_freq`:

| Band | Frequency Range (MHz) |
|------|-----------------------|
| 160m | 1.800 – 2.000 |
| 80m | 3.500 – 4.000 |
| 60m | 5.330 – 5.368 |
| 40m | 7.000 – 7.300 |
| 30m | 10.100 – 10.150 |
| 20m | 14.000 – 14.350 |
| 17m | 18.068 – 18.168 |
| 15m | 21.000 – 21.450 |
| 12m | 24.890 – 24.990 |
| 10m | 28.000 – 29.700 |
| 6m | 50.000 – 54.000 |
| 2m | 144.000 – 148.000 |
| 70cm | 420.000 – 450.000 |

## Hardware & Connection Requirements

### Tested hardware
- **IC-705** firmware — E2E verified 2026-08-18

### Connection modes
| Mode | Prerequisites | Platform |
|------|--------------|----------|
| **Direct network** (recommended) | IC-705 WiFi/Ethernet, RS-BA1 Server enabled, credentials set | Any |
| USB CI-V | CI-V USB cable (op. mode: CI-V) | Any |
| RemoteUty proxy | RemoteUty.exe running on Windows PC | Any |

### Setting up IC-705
1. `MENU → SET → WLAN Set → Remote Settings → Remote Server → ON`
2. `MENU → SET → WLAN Set → Remote Settings → Remote ID → Set username + password`
3. Note the radio's IP address: `MENU → SET → WLAN Set → Information`

## Testing

```bash
# Run full test suite (mock tests, no hardware required)
python -m pytest tests/ -v

# Run with hardware (IC-705 connected)
python scripts/e2e_civ_loop.py \
  --host 192.168.0.31 --user linnan --pwd your_password
```

## Repository Structure

```
src/rsba1/
├── radio_link.py            # High-level session manager (RECOMMENDED)
├── ctypes_wrappers/        # DLL call wrappers (reference only)
├── serial/                  # UDP Serial channel (50002)
│   ├── serial_codec.py     # Wire format encoding
│   └── command_client.py   # UDP Command channel (50001)
├── mailslot/               # Windows Mailslot IPC (deprecated)
└── mcp/
    ├── radio_link_server.py # Cross-platform MCP server (RECOMMENDED)
    └── _server_mailslot_ref.py  # Windows-only reference
```

## Status & Limitations

| Feature | Status |
|---------|--------|
| CI-V read (freq/mode/smeter) | ✅ Verified |
| CI-V write (set_freq) | ✅ Verified |
| PTT control | ✅ Verified |
| Authentication (RS-BA1 credentials) | ✅ Verified |
| Serial channel (UDP 50002) | ✅ Verified |
| Command channel (UDP 50001) | ✅ Verified |
| Audio streaming (UDP 50003) | ❌ Not implemented |
| RemoteUty.exe proxy mode | ⚠️ Not tested |

## Contributing

Issues and pull requests welcome. When reporting bugs:
1. Run `python scripts/e2e_civ_loop.py --dry-run` first to validate credentials
2. Include `--host` and `--user` (redact password) and full error output
3. Describe your hardware (radio model, firmware version)

## Disclaimer

This project is for **educational and research purposes only**.

RS-BA1 V2 and Icom transceivers are proprietary products of **Icom Inc.** (https://www.icomjapan.com). This project is not affiliated with, endorsed by, or connected to Icom Inc. in any way.

You are responsible for complying with applicable laws and Icom's license terms. Do not use this software to operate a transmitter without a valid amateur radio license.

## License

MIT — see [LICENSE](LICENSE) and [DISCLAIMER.md](DISCLAIMER.md).
