# VARA API: observed protocol notes

This document records what a real VARA modem was observed to do over its two
TCP ports (command and data), captured with `scripts/vara_api_sniffer.py`
proxying between a VARA terminal client and a real VARA instance. It is not a
transcription of an official specification — EA5HVK has not published one —
it is inferred from one full connect/data/disconnect session between two
VARA-HF stations (`F4JAW-2` connecting to `F4JAW-1`, bandwidth 2300 Hz).

Treat every entry below as **observed, not guaranteed**. Anything not
exercised in the capture (incoming LISTEN-side connect, CONNECT FAILED,
compression, WINLINK extensions, chat-mode data framing) is marked as such
and needs a follow-up capture before being treated as settled. This document
exists to plan `whale/vara_server.py` work against; see that file and
[LINK.md](LINK.md) for the current whale-side implementation and its
documented deviations.

## Ports

Two TCP ports per station, both observed on loopback:

- **Command port**: line-oriented ASCII, `\r`-terminated. Carries setup
  commands, connection control, and asynchronous status/telemetry lines.
- **Data port**: raw byte stream. Bytes written are transmitted; bytes
  received over the air are written back out. No framing, headers, or length
  prefixes were visible in the payloads — see "Data port" below.

## Command port

### Line format

- Commands and status lines are terminated by `\r` (not `\r\n`, not bare
  `\n`, in this capture).
- Multiple commands or multiple status lines are routinely batched into a
  single TCP write, separated by `\r`, e.g. one packet contained
  `MYCALL F4JAW-2\rCHAT ON\rBW2300\r` and one reply packet contained
  `OK\rPTT ON\rBUSY ON\r`. A client-side or server-side parser must not
  assume one command/status line per `recv()`.

### Commands observed (client -> VARA)

| Command | Example | Notes |
| --- | --- | --- |
| `MYCALL <call>` | `MYCALL F4JAW-2` | Sets the local callsign. |
| `CHAT ON` | `CHAT ON` | Enables chat/keyboard mode. `CHAT OFF` not observed but presumed symmetric. |
| `BW<n>` | `BW2300` | Selects bandwidth in Hz, no space between `BW` and the number. |
| `CONNECT <mycall> <dstcall>` | `CONNECT F4JAW-2 F4JAW-1` | Initiates an outbound connection. |
| `ABORT` | `ABORT` | Tears down the current connection. See "ABORT / disconnect timing" below; `DISCONNECT` itself was not exercised in this capture. |

Not exercised in this capture (present in whale today, or known from VARA's
general reputation, but unconfirmed against real traffic here): `LISTEN
ON`/`LISTEN OFF`, `DISCONNECT` (as distinct from `ABORT`), compression
commands, WINLINK-session commands.

### Status/telemetry lines observed (VARA -> client)

| Line | Example | Meaning / timing |
| --- | --- | --- |
| `OK` | `OK` | Acknowledges an accepted command. Observed after `MYCALL`, `CHAT ON`, `BW2300`, `CONNECT`, and `ABORT`. One `OK` per accepted command in sequence. |
| `BUSY ON` / `BUSY OFF` | `BUSY OFF` | Busy-channel detector state. Toggles around setup and around connect; not a simple one-shot — seen multiple times per session. |
| `IAMALIVE` | `IAMALIVE` | Unsolicited keepalive, observed roughly every 60 seconds for the life of the session, unconditionally (connected or not). |
| `REGISTERED` | `REGISTERED` | Sent once, shortly (~5s) after the first `IAMALIVE`, paired with `ENCRYPTION DISABLED` in the same packet. Likely a licensing/registration status push, not a reply to any command. |
| `ENCRYPTION DISABLED` | `ENCRYPTION DISABLED` | See above; sent alongside `REGISTERED`. |
| `PTT ON` / `PTT OFF` | `PTT ON` | Brackets every transmit burst, including the modem's own control/ACK frames during nominal receive. Matches whale's current format exactly. |
| `UNENCRYPTED LINK` | `UNENCRYPTED LINK` | Sent once, right after `CONNECT` succeeds, before the `CONNECTED` line. |
| `BITRATE (n)  <val> bps <TX|RX>` | `BITRATE (3)  82 bps TX` | Reports the modem's current bitrate index/value/direction. **Note the double space** between `(n)` and the value — verbatim in the capture. Appears before nearly every PTT ON/OFF pair once connected, both TX (client's own outbound burst) and RX (received burst). |
| `CONNECTED <mycall> <dstcall> <bandwidth>` | `CONNECTED F4JAW-2 F4JAW-1 2300` | **Three arguments, not two**: local call, peer call, and the bandwidth in Hz. This differs from whale's current `CONNECTED <peer> <mycall>` (two arguments, and peer/mycall order also differs — see whale/vara_server.py `_on_modem_event`). |
| `SN <x.y>` | `SN 11.4` | Signal-to-noise readout, one decimal place, varies per burst. Usually appears immediately before a `BITRATE (...) RX` line, sometimes in the same packet. |
| `BUFFER <n>` | `BUFFER 0` | Observed once, after an outbound data burst. Confirms this line is real (whale's docstring mentions it but the implementation never sends it). Only value `0` was observed; semantics (bytes queued? frames queued?) are not confirmed. |
| `DISCONNECTED` | `DISCONNECTED` | Sent once teardown completes — see ABORT timing below. Matches whale's current format. |

Not exercised in this capture: `CONNECT FAILED` (the connect in this capture
succeeded), any status specific to an incoming/LISTEN-side connect, WINLINK
or compression status lines.

### Session sequence (as observed)

1. **Setup**: client sends `MYCALL <call>`, `CHAT ON`, `BW<n>` (observed
   batched in one packet). VARA acks each with `OK`, interleaved with
   `BUSY OFF`/`BUSY ON` toggles.
2. **Idle**: `IAMALIVE` every ~60s; `REGISTERED`/`ENCRYPTION DISABLED` once,
   early.
3. **Connect**: client sends `CONNECT <mycall> <dstcall>`. VARA replies `OK`,
   then `PTT ON`/`BUSY ON`/`PTT OFF` as it attempts the link, then
   `UNENCRYPTED LINK`, a `BITRATE (...) TX` line, another `PTT ON`/`PTT OFF`
   pair, then `CONNECTED <mycall> <dstcall> <bandwidth>`, then one more
   `PTT ON`/`PTT OFF` pair.
4. **Data phase**: for each burst (either direction), a
   `SN <x.y>`/`BITRATE (...) <TX|RX>` pair precedes a `PTT ON`/`PTT OFF`
   bracket; the corresponding payload appears on the data port within
   ~0.1-0.2s of the command-port status. A `BUFFER <n>` line appeared once,
   right after the client's own outbound burst.
5. **Idle-but-connected**: once no application data is flowing, `BITRATE`/
   `SN` reporting stops but bare `PTT ON`/`PTT OFF` pairs continue every
   ~12s (link-idle polling), interleaved with the ongoing `IAMALIVE` every
   60s.
6. **Teardown (`ABORT`)**: client sends `ABORT`. VARA immediately acks with
   `OK`. One more `PTT ON`/`PTT OFF` cycle follows (~3.6s later, presumably
   the actual disconnect handshake with the peer), then VARA sends
   `PTT OFF`, `DISCONNECTED`, `BUSY OFF`, `BUSY ON` together, and finally a
   trailing `BUSY OFF`.

Important: `ABORT`'s `OK` is an immediate command acknowledgment, not a
completion signal — `DISCONNECTED` only arrives after the real teardown
finishes several seconds later. A client (or a whale reimplementation) must
not treat `OK` after `ABORT` as "already disconnected."

## Data port

- Pure byte stream, both directions; no visible framing, headers, or length
  prefixes distinguishable from payload content in this capture.
- Payload sizes cluster at fixed values per burst (38 bytes outbound; 89,
  171, or 178 bytes inbound in this capture), consistent with fixed-size
  FEC/ARQ blocks at whatever bitrate was in effect, not arbitrary
  application-message lengths.
- Timing: an outbound data-port write happens *before* the corresponding
  `PTT ON`/`BITRATE ... TX` status appears on the command port (the client
  hands off bytes, then the modem reports it is transmitting them).
  Inbound, it's the reverse: the `SN`/`BITRATE ... RX`/`PTT ON` status
  appears first, and the decoded payload lands on the data port ~0.1-0.2s
  later.

## Known gaps in this document

This spec is built from a single successful outbound-connect session on one
bandwidth (2300 Hz). It does not yet cover:

- An incoming connection accepted via `LISTEN ON` (all status-line wording
  and ordering on the accepting side is unconfirmed).
- A failed connect (`CONNECT FAILED` wording/timing unconfirmed).
- `DISCONNECT` as distinct from `ABORT` (only `ABORT` was exercised).
- Compression and WINLINK-session commands.
- `CHAT OFF`, and any data-port framing differences under chat mode versus
  plain ARQ data mode.
- The exact meaning and range of `BUFFER <n>` beyond the single `0` value
  observed.

Follow-up captures targeting these cases should be folded into this document
before it's treated as a complete implementation target.
