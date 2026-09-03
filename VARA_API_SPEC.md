# VARA API: observed protocol notes

This document records what a real VARA modem was observed to do over its two
TCP ports (command and data), captured with `scripts/vara_api_sniffer.py`
proxying between a VARA terminal client and a real VARA instance. It is not a
transcription of an official specification — EA5HVK has not published one —
it is inferred from three sessions between VARA-HF stations at 2300 Hz: two
successful connect/data/disconnect sessions (`F4JAW-2` connecting to
`F4JAW-1`) in the original mixed/binary `capture1.log` and the short,
human-readable `capture-chat.log`, plus an unsuccessful call from `F4JAW-2`
to `F4JAW-11` in `capture-conn-fail.log`.

Treat every entry below as **observed, not guaranteed**. Anything not
exercised in these captures (incoming LISTEN-side connect, compression,
WINLINK extensions) is marked as such
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
| `CHAT ON` | `CHAT ON` | Selects chat/keyboard mode. Both captures used it. The pure-chat capture exposed a length-prefixed application record format on the data port; see "Chat records." `CHAT OFF` remains unobserved. |
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
| `REGISTERED` | `REGISTERED` | Sent once per captured command-port session, about 5s after setup, paired with `ENCRYPTION DISABLED` in the same packet. In `capture1.log` an `IAMALIVE` happened first; in the chat capture it did not. Likely a licensing/registration status push, not a reply to any command. |
| `ENCRYPTION DISABLED` | `ENCRYPTION DISABLED` | See above; sent alongside `REGISTERED`. |
| `PTT ON` / `PTT OFF` | `PTT ON` | Brackets every transmit burst, including the modem's own control/ACK frames during nominal receive. Matches whale's current format exactly. |
| `UNENCRYPTED LINK` | `UNENCRYPTED LINK` | Sent once, right after `CONNECT` succeeds, before the `CONNECTED` line. |
| `BITRATE (n)  <val> bps <TX|RX>` | `BITRATE (3)  82 bps TX` | Reports the modem's current bitrate index/value/direction. **Note the double space** between `(n)` and the value — verbatim in both captures. Associated with data/setup activity in both directions; many short connected-idle PTT cycles have no `BITRATE` line. |
| `CONNECTED <mycall> <dstcall> <bandwidth>` | `CONNECTED F4JAW-2 F4JAW-1 2300` | **Three arguments**: local call, peer call, and bandwidth in Hz. Whale's current adapter now emits this observed shape; it uses the preceding `BW<n>` value or `0` if none was supplied. |
| `SN <x.y>` | `SN 11.4` | Signal-to-noise readout, one decimal place, varies per burst. Usually appears immediately before a `BITRATE (...) RX` line, sometimes in the same packet. |
| `BUFFER <n>` | `BUFFER 0` | Observed after each of four outbound chat messages, as well as after the outbound binary write in `capture1.log`. Only value `0` was observed; it arrived after the data-bearing TX burst and before a following short PTT cycle. The unit and exact semantics (bytes queued? frames queued?) remain unconfirmed. |
| `DISCONNECTED` | `DISCONNECTED` | Sent once established-session teardown completes, and also when an outbound connection attempt exhausts its retries without ever connecting. Matches whale's current API format. |

No `CONNECT FAILED` line was observed. The targeted failed-call capture instead
ended with `DISCONNECTED`; any conditions under which real VARA might use the
widely cited `CONNECT FAILED` spelling remain unknown. Also not exercised:
any status specific to an incoming/LISTEN-side connect, WINLINK, or compression.

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
4. **Data phase**: inbound payloads are preceded by
   `SN <x.y>`, `BITRATE (...) RX`, and `PTT ON`; decoded bytes reach the data
   port about 0.18-0.20s after that status packet and before `PTT OFF`.
   Outbound data-port writes precede the relevant TX activity; the exact
   mapping is less direct because short PTT cycles occur before the
   `BITRATE (...) TX` burst. `BUFFER 0` follows each completed outbound
   transfer observed in both captures.
5. **Idle-but-connected**: once no application data is flowing, `BITRATE`/
   `SN` reporting stops but bare `PTT ON`/`PTT OFF` pairs continue every
   ~12s (link-idle polling), interleaved with the ongoing `IAMALIVE` every
   60s.
6. **Locally requested teardown (`ABORT`, `capture1.log`)**: client sends
   `ABORT`. VARA immediately acks with
   `OK`. One more `PTT ON`/`PTT OFF` cycle follows (~3.6s later, presumably
   the actual disconnect handshake with the peer), then VARA sends
   `PTT OFF`, `DISCONNECTED`, `BUSY OFF`, `BUSY ON` together, and finally a
   trailing `BUSY OFF`.
7. **Failed outbound connection (`capture-conn-fail.log`)**: VARA acknowledges
   `CONNECT F4JAW-2 F4JAW-11` with `OK` about 0.09s later, in the same TCP
   write as the first `PTT ON` and `BUSY ON`. It makes 15 observed transmit
   attempts, each bracketed by `PTT ON`/`PTT OFF`, over about 47.2s. An
   `IAMALIVE` occurs normally between attempts. About 1.86s after the final
   `PTT OFF` (49.07s after the command), VARA sends
   `DISCONNECTED\rBUSY OFF\r`; `BUSY ON` follows about 1.0s later and
   `BUSY OFF` about 1.75s after that. It never emits `CONNECTED`,
   `UNENCRYPTED LINK`, or `CONNECT FAILED` during this attempt.

Important: `ABORT`'s `OK` is an immediate command acknowledgment, not a
completion signal — `DISCONNECTED` only arrives after the real teardown
finishes several seconds later. A client (or a whale reimplementation) must
not treat `OK` after `ABORT` as "already disconnected."

The chat capture also demonstrates **asynchronous teardown**. Its local API
client sent no `ABORT` or `DISCONNECT`; after the final incoming chat record
and another PTT cycle, VARA emitted `DISCONNECTED\rBUSY OFF\r`, followed by
`BUSY ON` and then `BUSY OFF`. The capture cannot distinguish a peer-requested
disconnect from another over-the-air/session cause, but it proves that a
client must accept `DISCONNECTED` without a preceding local command or `OK`.

## Data port

At the TCP layer this is a byte stream in both directions: TCP writes and
`recv()` calls do not preserve record boundaries. The modem API did not add
any separately visible binary envelope around the bytes in either successful
capture.
Any higher-level record format must therefore be parsed across arbitrary TCP
chunk boundaries.

### Chat records (`capture-chat.log`)

Every human-readable message used this application-level format in both
directions:

```text
<decimal payload byte count><space><payload bytes>
```

Examples observed verbatim include `12 Hello Alice!`, `10 Hello Bob!`, and
`7 bye bye`. In all seven records, the decimal number exactly equals the
number of bytes after the single space (observed range: 6 through 29). No CR,
LF, NUL, or other record terminator was present. Consequently, a compatible
parser reads decimal digits up to the space and then exactly that many bytes;
it must not rely on one record arriving in one TCP read. Empty payloads,
leading zeroes, non-ASCII/UTF-8 text, multiple records in one write, and
malformed lengths were not exercised.

This capture establishes the bytes used by the chat clients on the VARA data
port, but not which component owns the convention. The prefix was already
present in client-to-VARA writes and was also present in VARA-to-client data;
the proxy cannot tell whether VARA interprets it because of `CHAT ON` or
merely transports an end-to-end framing convention implemented by the chat
applications. Implementations should not strip or synthesize this prefix in
the generic byte-stream adapter without further evidence.

### Binary/mixed-session observations (`capture1.log`)

- Payload sizes clustered at fixed values per observed transfer (38 bytes
  outbound; 89, 171, or 178 bytes inbound). These are application-visible
  TCP chunks, not proven VARA FEC/ARQ block boundaries; TCP segmentation and
  application framing are confounding factors.
- Timing: an outbound data-port write happens *before* the corresponding
  `PTT ON`/`BITRATE ... TX` status appears on the command port (the client
  hands off bytes, then the modem reports it is transmitting them).
  Inbound, it's the reverse: the `SN`/`BITRATE ... RX`/`PTT ON` status
  appears first, and the decoded payload lands on the data port ~0.1-0.2s
  later.

## Known gaps in this document

This spec is built from two successful outbound-connect sessions and one
failed outbound-connect session on one bandwidth (2300 Hz), all using
`CHAT ON`. It does not yet cover:

- An incoming connection accepted via `LISTEN ON` (all status-line wording
  and ordering on the accepting side is unconfirmed).
- Failure modes other than an unanswered outbound call. That observed case
  ends with `DISCONNECTED`, not `CONNECT FAILED`; whether real VARA emits the
  latter for rejection or another failure class remains unconfirmed.
- `DISCONNECT` as distinct from `ABORT` (only `ABORT` was exercised).
  Whale provisionally treats `DISCONNECT` as a graceful drain of already
  accepted outbound bytes and `ABORT` as discarding those bytes before the
  same radio teardown; this is an implementation safety policy, not captured
  VARA behavior.
- Compression commands. None of the captures contains even a command token, so
  the spelling, arguments, acknowledgement, session lifetime, and ownership
  of the byte transformation are all unknown. In particular, names such as
  `COMPRESSION ON` must be treated as hypotheses, not protocol facts. A
  follow-up capture must include the command-port exchange and identical
  data-port payloads with the setting disabled and enabled before this can be
  implemented as a compatibility feature.
- WINLINK-session commands. None of the captures contains a command or status line
  identifiable as WINLINK-specific, so even the token spelling is unknown.
  Whale consequently leaves every unrecognized extension on its existing
  unknown-command path: it changes no state and sends no `OK`. This boundary
  is covered by an adapter test; acknowledging a guessed command would tell a
  client that unsupported session semantics were active. A usable follow-up
  capture must record the complete command port from TCP connection through
  teardown for otherwise equivalent non-WINLINK and WINLINK sessions, plus
  both directions of the data port. It must identify the client and VARA
  versions and exercise reconnect (or a second connection) so command
  arguments, acknowledgements/status lines, ordering, scope, reset behavior,
  and any data-stream transformation can all be distinguished.
- `CHAT OFF`, a session without any `CHAT` command, and whether the observed
  chat length prefix is interpreted by VARA or belongs entirely to the chat
  applications. Whale accepts exact `CHAT ON`/`CHAT OFF` commands but keeps
  the data port byte-transparent in every setting (including when no `CHAT`
  command was sent); tests enforce that conservative boundary. This is not a
  claim that the unobserved modes match real VARA, only that an unproven
  record transformation will not corrupt application data.
- The exact meaning, unit, and nonzero range of `BUFFER <n>`; all five
  observations were `BUFFER 0`. Whale implements only the evidence-backed
  empty boundary: it emits `BUFFER 0` after a successful link send drains the
  accepted application-data queue, never merely on TCP receipt, and does not
  claim or synthesize uncaptured nonzero values.

Follow-up captures targeting these cases should be folded into this document
before it's treated as a complete implementation target.
