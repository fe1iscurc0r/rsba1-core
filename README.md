# rsba1-core

**Pure Python implementation of the Icom RS-BA1 V2 protocol stack** — control IC-705, IC-9700 and any CI-V transceiver over the network. No Icom binaries required.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-green.svg)](https://python.org)
[![CI](https://github.com/fe1iscurc0r/rsba1-core/actions/workflows/ci.yml/badge.svg)](https://github.com/fe1iscurc0r/rsba1-core/actions)

## What is this?

Icom's RS-BA1 software runs on Windows only and is closed-source. This project reverse-engineers the protocol and provides a **pure Python replacement** that runs anywhere Python runs.

**Tested on: IC-705** (E2E verified 2026-08-18)

## Quick Start

```bash
pip install "rsba1-core[mcp]"   # MCP server needs fastmcp; plain CLI: pip install rsba1-core

export RADIO_HOST=192.168.0.31
export RADIO_USER=radio_user
export RADIO_PASSWORD=your_password

python -m rsba1.mcp read-freq
python -m rsba1.mcp set-freq 7074000
```

## Platform Support

| Platform | Network (radio-link) | Serial | Mailslot / CivCtrl.dll |
|----------|---------------------|--------|------------------------|
| Linux   | ✅ pure Python UDP   | ✅ `[serial]` | ❌ Windows-only |
| macOS   | ✅ pure Python UDP   | ✅ `[serial]` | ❌ Windows-only |
| Windows | ✅ pure Python UDP   | ✅ `[serial]` | ✅ legacy backends |

The default `radio-link` backend is **pure Python UDP** — no Icom binaries, no
DLLs, no pywin32. The Windows-only backends (`--backend mailslot`, `CivCtrl.dll`)
live behind optional `[windows]` extras and are guarded so they never break
imports or test collection on Linux/macOS. Serial support needs `pyserial`
(`pip install "rsba1-core[serial]"`).

## MCP Server (for AI Agents)

```bash
python -m rsba1.mcp   # starts MCP server on stdio
```

Available tools: `read_freq`, `read_mode`, `read_smeter`, `set_freq`, `ptt`, `get_status`

## Architecture

```
Your AI Agent / CLI
        ↓
rsba1-core (pure Python)
        ↓
IC-705 RS-BA1 server (UDP 50001 + 50002)
```

Key: no Windows APIs, no Icom DLLs, Python 3.11+ only.

## Feature Status

| Feature | Status |
|---------|--------|
| CI-V read (freq/mode/smeter) | ✅ E2E verified |
| CI-V write (set_freq) | ✅ E2E verified |
| PTT | ✅ E2E verified |
| Audio streaming (UDP 50003) | ❌ Not implemented |

## Documentation

- [docs/README.md](docs/README.md) — Bilingual (Chinese/English) full guide
- [docs/MCP集成指南.md](docs/MCP集成指南.md) — MCP integration guide
- [docs/真机验证报告.md](docs/真机验证报告.md) — E2E verification report

## License

MIT — see [LICENSE](LICENSE) and [DISCLAIMER.md](DISCLAIMER.md).
