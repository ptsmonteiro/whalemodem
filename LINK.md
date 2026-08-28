# Link protocol

This document describes the link protocol and VARA-shaped local TCP interface
implemented by the current `whale` package. Modulation, coding, framing, and
on-air timing are specified separately in [`FRAMING.md`](FRAMING.md).
Except where a section is explicitly marked as a proposed format, this is a
description of the present implementation and not a compatibility promise.

## Conventions

- Multi-bit integers and bytes are transmitted most-significant bit first.
- Byte offsets in the tables start at zero.
- `NUL` means the byte `0x00`.
- Callsigns are ASCII byte strings. The implementation does not impose a
  length, case, or character-set policy beyond encoding outbound values as
  ASCII. Received invalid ASCII is replaced while decoding.
- ISS (Information Sending Station) is the endpoint that currently owns the
  floor and may originate DATA. IRS (Information Receiving Station) is the
  other endpoint.

## Layering

```text
local application bytes
  -> messages and stop-and-wait ARQ (`whale.link`)
  -> modulation, coding, and framing (`FRAMING.md`)
  -> keyed, half-duplex radio (`whale.transport`)
```

The checked header and optional body consumed by the link layer are
defined in [`FRAMING.md`](FRAMING.md#checked-packet-format).

## Link packets

Every keying's checked header carries the packet type. Up to two packet-body
bytes are inline; any remainder follows in the same waveform.

| Type | Name | Body | Profile |
| ---: | --- | --- | --- |
| `0x01` | CONNECT | Connection proposal | control |
| `0x02` | CONNECT_ACK | Connection acceptance | control |
| `0x03` | DISC | Empty | control |
| `0x04` | DISC_ACK | Empty | control |
| `0x05` | DATA | Flags/sequence and sent-head duration inline, then chunk body | negotiated body |
| `0x06` | DATA_ACK | Answered sequence, next expected sequence, received mode, requested head | control |
| `0x07` | reserved | Must be ignored | control |
| `0x08` | reserved | Must be ignored | control |
| `0x09` | FLOOR_REQ | Empty | control |
| `0x0a` | FLOOR_GRANT | Empty | control |
| `0x0b` | TIMING_ACK | Reverse-direction timing measurement | control |
| `0x0c` | TIMING_CONFIRM | Timing-handshake confirmation | control |

### Connection bodies

CONNECT and CONNECT_ACK use the following length-delimited format. This is the
only connection-body format; the former NUL-delimited development format is
not accepted as a compatibility mode. All sizes are unsigned byte counts and
all multi-byte integers are big-endian.

Both packet types begin with this envelope:

| Field | Size | Value or meaning |
| --- | ---: | --- |
| Magic | 4 bytes | `ff 57 48 4c` (`0xff` followed by ASCII `WHL`) |
| Format version | 1 byte | `0x04` |
| Content length | 2 bytes | Bytes after this field |
| Content | `content_length` bytes | The version-specific fields below |

The frame payload must end immediately after Content. A decoder rejects a
body with the wrong magic, an unsupported format version, a content length
that does not equal the remaining body size, a truncated field, or extra
bytes.

CONNECT v4 Content is:

| Field | Size | Meaning |
| --- | ---: | --- |
| Source length | 1 byte | `1..15` |
| Source call | `source_length` bytes | Uppercase ASCII `A..Z`, `0..9`, or `-` |
| Destination length | 1 byte | `1..15` |
| Destination call | `destination_length` bytes | Same encoding as Source call |
| Session ID | 1 byte | Random value `1..255` for this attempt |
| Mode count | 1 byte | Number of following supported mode IDs |
| Supported mode IDs | `mode_count` bytes | Unique one-byte IDs in preference order |
| Proposed transmit mode | 1 byte | One of the advertised IDs |

CONNECT_ACK v4 Content is:

| Field | Size | Meaning |
| --- | ---: | --- |
| Source length | 1 byte | `1..15` |
| Source call | `source_length` bytes | Listener callsign |
| Destination length | 1 byte | `1..15` |
| Destination call | `destination_length` bytes | Caller callsign |
| Session ID | 1 byte | CONNECT session ID, echoed unchanged |
| Mode count | 1 byte | Number of following supported mode IDs |
| Supported mode IDs | `mode_count` bytes | Unique one-byte IDs in preference order |
| Accepted caller transmit mode | 1 byte | Mode accepted for caller-to-listener traffic |
| Listener transmit mode | 1 byte | Mode selected for listener-to-caller traffic |

Mode count may be zero only if both selected/proposed mode fields are mode 0;
mode 0 is always a valid fallback. Callsigns are compared according to the
existing link addressing policy after their encoding has been validated.
The limits above bound all variable fields before allocation.

The format version defines the complete handshake feature set. Version 4
includes calibration, ordinary-frame head feedback, and ACK-embedded mode confirmation, and therefore
requires the calibration handshake
described in `ADAPTIVE_TIMING.md`. Its CONNECT carries the protocol-fixed
calibration head before the frame. The decoder retains enough leading audio to
measure it. CONNECT_ACK begins the two-probe exchange. An endpoint that does
not implement every required version-4 behavior rejects version 4 rather than
accepting a reduced feature set. Version 3 included tail sequences and
three-byte timing reports; version 2 lacks the DATA/DATA_ACK feedback bytes.
Version 1 used the
removed MODE_REQ/MODE_ACK exchange.

The session ID and the complete encoded CONNECT body identify an attempt. A
listener answers an identical duplicate CONNECT using the same format version
and mode choices. Adaptive-timing measurements may change only according to
the conservative aggregation rule in `ADAPTIVE_TIMING.md`. A CONNECT with the
same session ID but different bytes is invalid. This prevents the handshake
feature set from changing across retries.

The caller randomly chooses one session identifier in the range `1..255` for
the complete retry sequence. The caller ignores acknowledgements with a
different destination or session identifier. A different CONNECT must not
replace a live session. If establishment fails after the retry limit, the
caller sends one best-effort DISC in case the peer reached a later handshake
state.

### DATA and DATA_ACK

The first two DATA body bytes are:

```text
bit 7       EOF: this is the final chunk of the current message
bits 6..0   sequence number, modulo 128
byte 1      transmitted head duration in unsigned 10 ms units
```

The duration is rounded upward and ranges from 10 ms through the documented
1.00 s maximum. The remainder is the chunk, including possibly zero bytes. Sequence numbers
run across message boundaries for the entire connection. They do not reset at
each message. This makes a retransmitted final chunk distinguishable from the
first chunk of the following message.

Only the ISS may originate DATA. Each DATA frame is sent using stop-and-wait
ARQ and must be acknowledged before the next sequence is sent. DATA_ACK has
exactly four significant body bytes:

```text
answered_sequence next_expected_sequence received_mode_id requested_head_duration
```

The sequence values use their low seven bits. An ACK accepts the outstanding frame
only when `answered_sequence` equals that frame's sequence and
`next_expected_sequence` is one step ahead modulo 128, and the mode ID equals
the mode in which the sender transmitted it. An ACK for an older frame or a
different mode is ignored while the sender continues waiting.

The requested duration uses the same 10 ms units as DATA. It is absolute, not
a delta, and is applied only from an otherwise acceptable ACK and only when it
exceeds the current connection value. Retries, duplicates, stale sequence
numbers, floor transfers, and delayed smaller requests therefore cannot
repeatedly inflate or decrease padding.

The receiver appends a chunk only when its sequence equals the expected
sequence, then advances the expectation. A duplicate is discarded. Every
decoded DATA frame, including a duplicate, receives an ACK. A message is
delivered only after an accepted chunk with EOF set; partial reassembly
survives receive polling timeouts.

The sender makes at most six attempts per chunk. Exhaustion raises a link
error; there is no session-level recovery or resynchronization packet.

### Floor transfer

An IRS that has data to send transmits FLOOR_REQ and waits for FLOOR_GRANT
before sending DATA. Upon handling the request, the current ISS becomes IRS,
sends FLOOR_GRANT, and the requester becomes ISS. Requests are retried up to
six times. A repeated request is safe: the receiver grants it again, covering
a lost grant.

The implementation handles FLOOR_REQ while polling for an incoming message.
A request received while the peer is busy with another blocking operation may
be discarded and must succeed on a later retry.

### Mode adaptation

Each endpoint adapts only its own transmit direction from ARQ outcomes:

- Three unanswered attempts change one step down before retrying the same chunk.
- Three consecutive first-attempt chunks change one step up before the next chunk.
- Steps follow registry order and are limited to modes the peer advertised.
  The shipped order is 0, 1, 2; `registry_with_vf3()` appends mode 3 above
  them (see [`FRAMING.md`](FRAMING.md#mode-3-vf3-a-non-cpfsk-data-mode)).

There is no separate mode-change exchange. While connected, a receiver tries
the control mode and every mutually advertised DATA mode. A decoded DATA frame
is authoritative: the receiver adopts that mode and returns it as
`received_mode_id` in DATA_ACK. Consequently an ISS can recover from complete
silence at one speed by retransmitting the same sequence at a lower speed; the
first successful ACK confirms both delivery and the new mode. DATA_ACK remains
in the robust control mode and does not describe the reverse-direction mode.
Head fields encode duration rather than symbols, so a mode change preserves
the protection and rounds it upward at the new baud.

### Disconnect

Either endpoint may send DISC. The peer answers with DISC_ACK, records the
current transmit mode as good in its in-memory history, enters IDLE, and emits
a disconnected event. A locally initiated disconnect tries DISC up to three
times, accepts either DISC_ACK or a simultaneous DISC as completion, and then
enters IDLE even if no response arrived. Its return value is `True` when the
handshake was acknowledged (or the link was already idle) and `False` when all
attempts timed out.

A connected endpoint also abandons the session after 150 seconds without
decoding any frame from its peer. This is a crash/out-of-range backstop, not a
keepalive protocol: a completely idle connection therefore expires after that
interval. Teardown sends one best-effort DISC before returning to IDLE.

## Local TCP interface

`whale.vara_server` exposes two listening TCP sockets, bound to
`127.0.0.1` by default. This interface resembles VARA but is not a complete
VARA implementation.

### Command port

Commands are ASCII and terminated by CR, LF, or CRLF. Command names and the
ON/OFF value are case-insensitive; callsign values are preserved.

```text
MYCALL <call>
LISTEN ON
LISTEN OFF
CONNECT <mycall> <destination_call>
DISCONNECT
ABORT
```

`ABORT` is currently identical to `DISCONNECT`. Unknown or malformed commands
are logged and receive no error response. `LISTEN ON` starts an incoming
session worker if one is not already running. After the radio connection is
established, that worker accepts one data-port TCP connection.

The command port can emit these CR-terminated status lines:

```text
PTT ON
PTT OFF
CONNECTED <peer> <mycall>
CONNECT FAILED
DISCONNECTED
```

Although `BUFFER <n>` appears in the server module's introductory docstring,
the current implementation never emits it.

### Data port

Once the radio link is connected, the server accepts a TCP client on the data
port. Bytes read from TCP are coalesced from currently queued reads and passed
to one link message; the link splits them into profile-sized DATA chunks.
Completed inbound link messages are written to TCP as raw bytes.

TCP itself is a byte stream, so message boundaries are not exposed to the
local client. The mapping between TCP read batches and over-the-air messages
is an implementation detail and must not be used for application framing.

## Current limitations and compatibility notes

- Connection format version 4 is the only implemented format. Versions 1, 2,
  and 3 and the former NUL-delimited development format are not accepted. The current
  format adds magic, versioning, and lengths, but not authentication or
  encryption.
- There is one outstanding DATA frame at a time; no windowing or cumulative
  multi-frame ACK is implemented.
- The link is point-to-point and has no channel addressing outside callsigns
  in the connection handshake.
- Control frames are retried by the operation that needs them, not by a common
  reliable control channel. Unexpected packet types are often discarded.
- Sequence numbers wrap after 128 accepted DATA chunks. Correctness assumes
  stop-and-wait ordering and no extremely delayed frame surviving a full wrap.
- Callsigns, supported-mode lists, and packet-body lengths are only lightly
  validated. This protocol currently assumes two trusted implementations of
  this code rather than hostile input.
- Compression, bandwidth commands, WINLINK extensions, and most of the real
  VARA command/status surface are not implemented.
