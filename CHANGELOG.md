# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added
- `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, issue and PR templates.
- `docs/re-notes/mailslot-protocol.md` — public summary of the `UtyCtrl.dll`
  mailslot contract (packet layout + nine command codes), replacing code
  comments that pointed at private notes.
- MCP panel-key tools and `get_status` extensions (2026-08-25).
- Python 3.13 to the CI matrix; `windows-latest` job added.
- `ruff` configuration and a lint job.

### Changed
- README rewritten to match the code: 32 MCP tools (was documented as 6),
  three backends, accurate feature status, and an explicit statement of the
  relationship to `rigplane`.
- Example addresses moved to the RFC 5737 range (`192.0.2.0/24`).
- Internal tooling names removed from comments; wording is now project-neutral.
- Packaging metadata modernised (`license = "MIT"`, `license-files`, Changelog
  URL, setuptools ≥ 77).

### Removed
- `scripts/send_command_login.py` moved to `scripts/legacy/` — it targets an API
  that no longer exists and was already marked deprecated in its own header.

## [1.0.0] — 2026-08-18

### Added
- Pure-Python RS-BA1 V2 stack: session handshake, CI-V transport over UDP
  50001/50002, frequency/mode/S-meter read, frequency/mode write, PTT.
- End-to-end verification against a physical IC-705 (read frequency three times
  consistently, set frequency, PTT).
- CLI (`rsba1`) and MCP server over stdio.
- Serial backend (USB CI-V) and Windows `mailslot` backend (`UtyCtrl.dll`).
- Amateur-band whitelist validation for frequency writes.
- Bilingual README and documentation.
