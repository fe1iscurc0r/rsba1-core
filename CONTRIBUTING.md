# Contributing

Thanks for looking. This is a small, single-maintainer project, so the most useful contributions are focused bug reports and small, well-tested patches.

## Development setup

```bash
git clone https://github.com/fe1iscurc0r/rsba1-core.git
cd rsba1-core
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[all]"
```

Optional extras:

- `[mcp]` — MCP server (`fastmcp`)
- `[serial]` — USB CI-V backend (`pyserial`)
- `[windows]` — mailslot backend (`pywin32`, Windows only)

## Tests and lint

```bash
ruff check .                    # style / imports / pyupgrade
python -m pytest tests/ -q      # full suite
```

Hardware tests are **skipped automatically** unless the radio is reachable:

```bash
export RSBA1_E2E_HOST=192.0.2.10
export RADIO_USER=radio_user
export RADIO_PASSWORD=...
pytest tests/ -q -k e2e
```

Never commit a real radio password or a real LAN address. Documentation uses the
RFC 5737 range (`192.0.2.0/24`); please do the same.

## Pull requests

- One concern per PR. A typo fix bundled with a refactor will be asked to split.
- Commit messages: `type(scope): summary` — `fix(serial)`, `feat(mcp)`, `docs(readme)`, `chore(ci)`.
- New public API needs tests; bug fixes need a test that fails before the fix.
- Keep comments where they carry knowledge (protocol findings, hardware quirks). Comments may be Chinese or English — do not "clean up" one into the other.
- Do not reformat untouched files; it makes review impossible.

## Licensing boundary (important)

This project is **MIT**. Do not paste code from GPL-3.0 or AGPL-3.0 projects
(including `wfview`, `wfweb`, `sdroxide`, `Nexus`) into it. MIT code may flow
into an AGPL project, never the other way round. If a patch needs GPL code,
open an issue first so the license question is settled before the code arrives.

The Windows `mailslot` backend interoperates with Icom's `UtyCtrl.dll` at
runtime; no Icom binaries, headers, or decompiled output are redistributed here.

## Scope

In scope: protocol correctness, backends, CLI, MCP tools, docs, tests.

Out of scope for now: audio streaming (UDP 50003) and spectrum scope (`0x27 00`).
Those are better served by [rigplane](https://github.com/rigplane/rigplane-core);
see the note in the README before starting on them.

## Reporting bugs

Use the issue template. Include: platform, Python version, backend
(`radio-link` / `serial` / `mailslot`), radio model and firmware, and the exact
command. Redact credentials and addresses.

Security issues: see [SECURITY.md](SECURITY.md) — do not open a public issue.
