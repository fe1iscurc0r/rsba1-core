# rsba1-core

**Pure Python implementation of the Icom RS-BA1 V2 protocol stack** — control IC-705, IC-9700 and other CI-V transceivers over the network. No Icom binaries required.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-green.svg)](https://python.org)
[![CI](https://github.com/fe1iscurc0r/rsba1-core/actions/workflows/ci.yml/badge.svg)](https://github.com/fe1iscurc0r/rsba1-core/actions)

## What is this?

Icom's RS-BA1 software runs on Windows only and is closed-source. This project reverse-engineers the network protocol and provides a **pure Python replacement** that runs anywhere Python runs — plus an MCP server so agents can drive the radio.

**Hardware-verified:** IC-705 (end-to-end, 2026-08-18).

## Quick start

```bash
pip install "rsba1-core[mcp]"     # MCP server needs fastmcp; plain CLI: pip install rsba1-core

export RADIO_HOST=192.0.2.10      # your radio's LAN address (enable Remote Server on the rig)
export RADIO_USER=radio_user
export RADIO_PASSWORD=your_password

python -m rsba1.mcp read-freq
python -m rsba1.mcp set-freq 7074000
```

## Platform support

Three independent backends, chosen with `--backend`:

| Backend | Transport | Linux | macOS | Windows | Notes |
|---|---|---|---|---|---|
| `radio-link` (default) | UDP 50001/50002 | ✅ | ✅ | ✅ | Pure Python, no DLLs, no pywin32 |
| `serial` | USB CI-V (`[serial]`) | ✅ | ✅ | ✅ | Needs `pyserial` |
| `mailslot` | Icom `UtyCtrl.dll` IPC (legacy) | ❌ | ❌ | ✅ | `[windows]` extra; import-guarded so other platforms still install cleanly |

Only the `mailslot` backend touches Icom binaries. It is a legacy compatibility path, kept because the protocol was documented here first — see [docs/re-notes/mailslot-protocol.md](docs/re-notes/mailslot-protocol.md).

## MCP server (for AI agents)

```bash
python -m rsba1.mcp          # stdio transport
```

**32 tools**, grouped:

- **Read** — `read_freq`, `read_mode`, `read_smeter`, `get_status`
- **Tune** — `set_freq`, `set_mode`, `set_if_filter`, `set_att`, `set_pamp`, `set_agc`, `set_nr`, `set_nb`, `set_notch`, `set_mon`, `set_vox`, `set_dtx`, `set_split`, `set_duplex`, `set_xfc`, `set_rit`, `set_rit_freq`
- **Transmit** — `ptt`, `set_max_tx_power`, `set_tuner`
- **Scan** — `scan_start`, `scan_stop`
- **Tone** — `set_tone_mode`, `set_tone_freq`
- **Panel keys** — IC-705 front-panel key emulation
- **Session** — `restore_freq`, `shutdown`

See [docs/MCP集成指南.md](docs/MCP集成指南.md).

## Architecture

```
Your agent / CLI / script
        ↓
rsba1-core  ──┬── radio-link  →  UDP 50001 (control) + 50002 (CI-V)
              ├── serial      →  USB CI-V
              └── mailslot    →  UtyCtrl.dll IPC (Windows legacy)
        ↓
Icom radio (RS-BA1 remote server)
```

## Feature status

| Feature | Status |
|---|---|
| CI-V read (freq / mode / S-meter) | ✅ hardware-verified |
| CI-V write (freq / mode / filters / tone) | ✅ hardware-verified |
| PTT | ✅ hardware-verified |
| Panel-key emulation (IC-705) | ✅ implemented |
| Serial backend | ✅ implemented |
| Mailslot / `UtyCtrl.dll` backend | ✅ implemented (Windows, legacy) |
| MCP server | ✅ 32 tools |
| Audio stream (UDP 50003) | ⚠️ channel handshake only — **no audio data path** |
| Spectrum scope (`0x27 00`) | ❌ not implemented |

The audio row matters: the session negotiates a 50003 channel, which proves the session is alive, but this project does not encode, decode, or play audio. Do not read "audio channel opens" as "audio works".

## Relationship to rigplane

[`rigplane`](https://github.com/rigplane/rigplane-core) (MIT) is a far more complete Icom/Yaesu control stack — audio TX/RX, scope, DSP, web UI, rigctld. It started as `icom-lan` in February 2026; this project started in August 2026 and overlaps with it only on the pure-UDP control path.

This repository is **not** trying to compete with rigplane. Where rigplane already does something better, the intent is to consume it rather than reimplement it. What is kept here is the part that has no equivalent upstream (the MCP surface and the Windows-native backend), as satellites. See [issue #4340](https://github.com/rigplane/rigplane-core/issues/4340) for the proposed boundary.

License-wise the two are compatible in one direction only: MIT here can be absorbed into an AGPL project, never the reverse. Nothing GPL/AGPL is vendored in this repository, and it should stay that way.

## Documentation

- **[README.zh-CN.md](README.zh-CN.md)** — 中文说明
- [docs/README.md](docs/README.md) — bilingual full guide
- [docs/MCP集成指南.md](docs/MCP集成指南.md) — MCP integration guide
- [docs/真机验证报告.md](docs/真机验证报告.md) — hardware verification report
- [docs/re-notes/](docs/re-notes/) — protocol notes from the reverse-engineering work
- [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](CHANGELOG.md)

## License

MIT — see [LICENSE](LICENSE) and [DISCLAIMER.md](DISCLAIMER.md).
