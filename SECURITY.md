# Security policy

## Reporting

Please **do not open a public issue** for a vulnerability. Use GitHub's
[private vulnerability reporting](https://github.com/fe1iscurc0r/rsba1-core/security/advisories/new)
on this repository. If that is unavailable, open a minimal public issue asking
for a private channel — with no technical details.

Expect an acknowledgement within a few days. This is a hobby project maintained
by one person; there is no SLA.

## Supported versions

Only the latest release on `main` is supported. Fixes are not backported.

## Things this project handles that deserve care

**Radio credentials.** `RADIO_USER` / `RADIO_PASSWORD` are read from the
environment. They are never written to disk, logged, or included in `get_status`
output. If you find any code path that echoes them, that is a bug — report it.

**Transmit (PTT).** `ptt` and `set_max_tx_power` key a real transmitter. Misuse
can transmit out of band or into an antenna mismatch. The MCP server exposes
these tools; if you embed it in an agent loop, gate transmission behind an
explicit human confirmation. Frequency writes are validated against an amateur
band whitelist for this reason.

**Local network exposure.** The radio's RS-BA1 server listens on the LAN with a
single username/password and no transport encryption. Do not expose port
50001/50002 to the internet. If you need remote access, tunnel it.

**The `mailslot` backend** loads Icom's `UtyCtrl.dll` at runtime on Windows.
That DLL is not distributed with this project; you are responsible for having a
licensed copy. Bugs in the DLL are outside this project's control.

## Out of scope

- Attacks that require physical access to the radio or the host.
- Denial of service against the radio's own firmware.
- Anything requiring a modified Icom binary.
