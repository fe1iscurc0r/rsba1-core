# Mailslot / `UtyCtrl.dll` protocol notes (legacy Windows backend)

> Public summary of the reverse-engineering work behind `src/rsba1/mailslot/`.
> The full working notes are kept private; everything below is what the code
> actually implements, verified against the DLL's disassembly.

## Why this backend exists

On Windows, Icom's RS-BA1 stack does not talk to the radio directly from the
application. `RemoteUtility` (= `UtyCtrl.dll`) holds the network session, and
clients talk to it locally through a **mailslot**. That is why this backend is
Windows-only: it is an IPC path to Icom's own process, not a network protocol.

It is kept as a **legacy compatibility path**. The default `radio-link` backend
speaks UDP directly and needs no Icom binaries at all.

## Command packet layout

Fixed 4-byte header + variable payload, little-endian:

| Offset | Length | Field | Notes |
|---|---|---|---|
| 0 | 1 | `cmd_code` | see the command table below |
| 1 | 1 | `data_len` | payload length, **excludes** the 4-byte header |
| 2 | 2 | `reserved` | stack residue in the original, normally 0 |
| 4 | N | `payload` | N = `data_len`, maximum 255 |

Consequences that are easy to get wrong:

- `data_len` is a **single byte**, so the payload ceiling is 255 and the largest
  packet is 259 bytes.
- The value written to the mailslot is `data_len + 4`, not `data_len`. This
  matches `WriteFile`'s third argument in `UtyCtrl.dll` (`add eax, 4` at
  `0x10001113`).

## Command codes

Nine exported functions map one-to-one onto nine command codes. `expected len`
is the reply payload size the code waits for (`0` = immediate acknowledgement,
`None` = variable).

| Code | Name | Expected reply | Note |
|---|---|---|---|
| 0 | `GetCountClientTrans` | 0 | session count |
| 1 | `GetClientTransInfo` | `0x6C` | session descriptor |
| 2 | `ExecCmd` | variable | carries outbound CI-V traffic; `data_len = arg5 + 0x14` |
| 3 | `GetClientTransVol` | `0x24` | audio volume |
| 4 | `GetClientTransInfo2` | `0x78` | extended descriptor |
| 5 | `GetClientTransVol3` | `0x3C` | volume, second form |
| 6 | `GetCommandProcCount` | 0 | command counter |
| 7 | `GetRemoteTransNetworkSet` | `0x40` | network settings |
| 8 | `GetRemoteTransState` | `0x1C` | radio state block |

## What is deliberately not published

The full notes (PE analysis, per-function field breakdowns, mailslot naming
experiments) are not in this repository. They are working documents, contain
machine-specific paths, and are not needed to use or audit the backend.

What **is** published is the contract the code actually relies on — the layout
above and the nine codes — so that anyone can verify the implementation without
trusting a comment.

## Status

Implemented and import-guarded (`[windows]` extra). Not covered by CI on
non-Windows runners; the protocol layer itself is pure `struct` code and is
unit-tested on Linux.
