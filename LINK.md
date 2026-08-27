# Link protocol

This document describes the link protocol and VARA-shaped local TCP interface
implemented by the current `whale` package. Modulation, coding, framing, and
on-air timing are specified separately in [`FRAMING.md`](FRAMING.md).
This is a description of the present implementation, not a compatibility
promise: there is no on-air version field or formal interoperability guarantee
yet.

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

The frame payload consumed by the link layer is defined in
[`FRAMING.md`](FRAMING.md#frame-format).

## Link packets

The first byte of every frame payload is a packet type. The remaining bytes
are its body.

| Type | Name | Body | Profile |
| ---: | --- | --- | --- |
| `0x01` | CONNECT | Connection proposal | control |
| `0x02` | CONNECT_ACK | Connection acceptance | control |
| `0x03` | DISC | Empty | control |
| `0x04` | DISC_ACK | Empty | control |
| `0x05` | DATA | Flags/sequence byte, then chunk bytes | negotiated |
| `0x06` | DATA_ACK | Answered sequence, next expected sequence | negotiated |
| `0x07` | MODE_REQ | Proposed mode ID | control |
| `0x08` | MODE_ACK | Accepted mode ID | control |
| `0x09` | FLOOR_REQ | Empty | control |
| `0x0a` | FLOOR_GRANT | Empty | control |

### Connection bodies

CONNECT has this body:

```text
source_call NUL destination_call NUL supported_mode_ids... proposed_tx_mode_id session_id
```

The supported-mode list is zero or more one-byte mode IDs. The final two bytes
are the caller's proposed transmit mode for the caller-to-listener direction
and a session identifier. A new peer currently proposes mode 0; otherwise it
may propose the last mode recorded as good for that callsign pair. Mode history
is in memory only.

The caller randomly chooses one session identifier in the range `1..255` for
the complete CONNECT retry sequence; zero means unspecified. The identifier
distinguishes a retry of the current connection from a delayed frame belonging
to another attempt.

CONNECT_ACK has this body:

```text
source_call NUL destination_call NUL supported_mode_ids...
accepted_caller_tx_mode_id listener_tx_mode_id session_id
```

Its last three bytes contain the two independently negotiated modes followed by
the caller's session identifier echoed unchanged. The listener first states
the mode accepted for traffic from the caller, then the mode it will use for
its own transmissions. An unsupported proposal falls back to mode 0. For a
short body, the decoder falls back to mode 0 and session ID zero.

The caller ignores CONNECT_ACK packets whose destination or session identifier
does not match the active attempt. The listener stores the accepted
CONNECT_ACK body and retransmits it byte-for-byte when it receives a duplicate
CONNECT from the same peer with the same session identifier. A CONNECT carrying
a different identifier does not replace a live session. If the caller exhausts
its CONNECT attempts, it sends one best-effort DISC in case the listener entered
CONNECTED but every CONNECT_ACK was lost.

After a valid exchange both endpoints enter CONNECTED, reset transmit and
receive sequence numbers to zero, and clear partial message reassembly. The
caller starts as ISS; the listener starts as IRS.

### DATA and DATA_ACK

The first DATA body byte is:

```text
bit 7       EOF: this is the final chunk of the current message
bits 6..0   sequence number, modulo 128
```

The remainder is the chunk, including possibly zero bytes. Sequence numbers
run across message boundaries for the entire connection. They do not reset at
each message. This makes a retransmitted final chunk distinguishable from the
first chunk of the following message.

Only the ISS may originate DATA. Each DATA frame is sent using stop-and-wait
ARQ and must be acknowledged before the next sequence is sent. DATA_ACK has
exactly two significant body bytes:

```text
answered_sequence next_expected_sequence
```

Both values use their low seven bits. An ACK accepts the outstanding frame
only when `answered_sequence` equals that frame's sequence and
`next_expected_sequence` is one step ahead modulo 128. An ACK for an older
frame is ignored while the sender continues waiting.

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

- A chunk requiring at least three attempts requests one step down.
- Three consecutive first-attempt chunks request one step up.
- Steps follow mode order 0, 1, 2 and are limited to modes the peer advertised.

MODE_REQ carries the proposed one-byte mode ID. The receiver replies with the
accepted ID in MODE_ACK, using mode 0 for an absent or unsupported value, and
then changes the profile at which it expects that peer's DATA and DATA_ACK.
The requester changes its transmit profile only after receiving MODE_ACK.

Because MODE_ACK can be lost, the receiver temporarily retains the previous
profile as a decode fallback. A decoded DATA or DATA_ACK is authoritative: it
confirms whichever profile actually decoded and removes or corrects the
fallback. Thus a lost MODE_ACK leaves the requester at its old profile and the
next data-plane frame makes the receiver converge back to that profile. Control
frames do not confirm a data profile because they always use the control mode.

### Disconnect

Either endpoint may send DISC. The peer answers with DISC_ACK, records the
current transmit mode as good in its in-memory history, enters IDLE, and emits
a disconnected event. A locally initiated disconnect tries DISC up to three
times, accepts either DISC_ACK or a simultaneous DISC as completion, and then
enters IDLE even if no response arrived.

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

- There is no protocol magic, version, capability namespace, authentication,
  encryption, or forward-compatible length around link packet bodies.
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
