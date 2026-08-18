# rsba1-core

**Pure Python implementation of the Icom RS-BA1 V2 protocol stack — cross-platform radio control for IC-705, IC-9700 and other CI-V compatible transceivers.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-green.svg)](https://python.org)

## What is this?

RS-BA1 is Icom's official Windows software for remote control of their transceivers over LAN/Internet. This project reverse-engineers the RS-BA1 protocol to build a **platform-independent Python library** that can control IC-705 (and other CI-V radios) without Icom's binary components.

You get:
- A pure Python CI-V protocol stack (no DLL dependencies)
- An MCP server for AI agent integration (Lumo/LMStudio/etc.)
- CLI tools for scripting and automation
- Cross-platform support (Windows/Linux/macOS)

## Status

| Component | Status |
|-----------|--------|
| CI-V frame encoding/decoding | ✅ Stable |
| Mailslot IPC (CivCtrl path) | ✅ Stable |
| Serial channel (UDP 50002) | ✅ Stable |
| Command channel (UDP 50001) | ✅ Stable |
| FastMCP server (6 tools) | ✅ Stable |
| **Real hardware E2E test** | ✅ Verified on IC-705 |

See [docs/REVERSE_PLAN.md](docs/REVERSE_PLAN.md) for full reverse engineering documentation.

## Quick Start

### Prerequisites

- Python 3.11+
- IC-705 connected via USB (CI-V mode) or network (RS-BA1 remote mode)
- RadioLink bridge running on Windows (for remote mode) — or use direct USB

### Installation

```bash
pip install .
```

For MCP server only:
```bash
pip install ".[mcp]"
```

### CLI Usage

```bash
# Read frequency
python -m rsba1.cli ic705 read-freq

# Read S-meter
python -m rsba1.cli ic705 read-smeter

# Set frequency
python -m rsba1.cli ic705 set-freq 7.074

# Set mode
python -m rsba1.cli ic705 set-mode USB

# PTT on/off
python -m rsba1.cli ic705 ptt tx
python -m rsba1.cli ic705 ptt rx
```

### MCP Server

Start the MCP server for AI agent integration:

```bash
# Stdio mode (default, for MCP clients)
python -m rsba1.mcp

# HTTP SSE mode
python -m rsba1.mcp --transport sse --port 8765
```

Available tools: `ic705_read_freq`, `ic705_read_mode`, `ic705_read_smeter`, `ic705_set_freq`, `ic705_ptt`, `ic705_get_status`

## Architecture

```
Your App / AI Agent
        ↓
FastMCP Server (rsba1.mcp)
        ↓
Python Protocol Stack (rsba1.*)
        ↓
RadioLink (Windows) ← USB → IC-705
   or
Direct Serial / Network
```

See [docs/MCP_CLIENT.md](docs/MCP_CLIENT.md) for MCP integration guide.

## Hardware Requirements

- **IC-705** (primary target, tested)
- **IC-9700** (compatible, same CI-V protocol)
- Other Icom CI-V radios (IC-7300, IC-7610, etc.) — may work with minor adjustments

Connection modes:
1. **USB direct** (recommended for local control): RadioLink CI-V USB cable
2. **Network remote**: RS-BA1 server running on Windows PC connected to radio

## Documentation

- [Reverse Engineering Plan](docs/REVERSE_PLAN.md) — full protocol analysis
- [Progress Tracker](docs/进度.md) — detailed implementation status (Chinese)
- [MCP Client Guide](docs/MCP_CLIENT.md) — AI agent integration
- [Live E2E Verification](docs/live_e2e_verification.md) — hardware testing guide

## Testing

```bash
# Run all tests (193 cases)
python -m pytest tests/ -v

# Mock tests only (no hardware)
python -m pytest tests/ -v -k "mock"

# E2E test (requires IC-705 connected)
python scripts/e2e_civ_loop.py
```

## Limitations

- Audio streaming (UDP 50003) not yet implemented
- Remote mode requires RadioLink on Windows (pure Python remote implementation is future work)
- CI-V over Bluetooth not tested

## Contributing

Issues and PRs welcome. Please see [docs/REVERSE_PLAN.md](docs/REVERSE_PLAN.md) for the reverse engineering methodology if you want to extend support to additional radios or features.

## Disclaimer

This project is for **educational and research purposes only**. RS-BA1 and related software are proprietary products of Icom Inc. This project is not affiliated with, endorsed by, or connected to Icom Inc. in any way.

The reverse engineering was performed on locally-installed software for the purpose of understanding interoperability and creating open-source tooling for amateur radio operators. Users are responsible for complying with applicable laws and Icom's license terms.

## License

MIT License — see [LICENSE](LICENSE).
