# Modulation, coding, and framing

This document describes the modulation, coding, and framing implemented by the
current `whale` package. It defines how a link packet becomes an over-the-air
audio waveform. See [`LINK.md`](LINK.md) for connection management,
mode negotiation, ARQ, and the local TCP interface.

This is a description of the present implementation, not a compatibility
promise: there is no on-air version field or formal interoperability guarantee
yet.

## Conventions

- Multi-bit integers and bytes are transmitted most-significant bit first.
- One radio keying carries exactly one link packet.
- Every keying starts with one fixed-size robust bootstrap header. A packet
  may additionally have one body frame; packets are not bundled.

## Processing chain

```text
link packet
  -> robust bootstrap header and optional body frame (`whale.framing`)
  -> waveform audio (`whale.afsk` for the built-in modes)
  -> keyed, half-duplex radio (`whale.transport`)
```

## Physical-layer interface

The link protocol depends on the `WaveformMode` contract in
`whale.waveform`, not on CPFSK functions directly. A mode supplies its own
packet encoder, streaming-buffer decoder, airtime calculation, sample rate,
DATA chunk size, synchronization confidence threshold, and stable on-air mode
ID. `ModeRegistry` provides the ordered set used for negotiation/adaptation
and identifies the robust control-plane mode.

The modes below are `whale.afsk.Profile` instances backed by `CpfskCodec`.
`Profile` retains the CPFSK-specific tone and baud settings, while satisfying
the generic link-facing contract through `encode()`, `decode()`, and
`airtime()`. A waveform with different modulation, synchronization, framing,
or FEC can implement the same contract and be installed in a registry without
changing connection management or ARQ.

## Modulation profiles

Audio is mono, 48 kHz, continuous-phase binary FSK. A zero or one selects the
corresponding profile tone for one symbol. The complete waveform has a 5 ms
amplitude ramp at each end and a nominal amplitude of 0.6.

| Mode ID | Name | Baud | 0 tone | 1 tone | DATA chunk size |
| ---: | --- | ---: | ---: | ---: | ---: |
| `0` | `300baud` | 300 | 1200 Hz | 1800 Hz | 49 bytes |
| `1` | `600baud` | 600 | 1200 Hz | 1800 Hz | 102 bytes |
| `2` | `1200baud` | 1200 | 1200 Hz | 2200 Hz | 208 bytes |

Mode 0 is the mandatory bootstrap and robust-body profile. Every keying begins
in mode 0. CONNECT and CONNECT_ACK bodies also use it. DATA bodies use the
negotiated profile; DATA_ACK is wholly contained in the robust header. The two directions
are negotiated and adapted independently as described in
[`LINK.md`](LINK.md#mode-adaptation).

The receiver detects the known sync word using normalized correlation. The
current confidence threshold is 0.7. This is a receiver implementation detail,
not an encoded field.

## Coding and error detection

The current profiles do not use forward-error correction or interleaving. A
CRC detects corrupted frames; recovery is performed by the link layer through
acknowledgements and retransmission.

The CRC is CRC-16/CCITT-FALSE over `Length || Payload`. Its parameters are
polynomial `0x1021`, initial value `0xffff`, no reflected input or output, and
no final XOR. The CRC byte containing bits 15..8 is sent first.

## Keying and bootstrap-header format

Every keying is `robust bootstrap frame | optional body frame`. The bootstrap
is the fixed 10-byte payload of the generic mode-0 frame described below. Its
CRC therefore protects every decoder-control field:

| Field | Size | Meaning |
| --- | ---: | --- |
| Magic | 2 bytes | ASCII `WH` |
| Header version | 1 byte | `0x01` |
| Link packet type | 1 byte | Type defined in `LINK.md` |
| Body mode ID | 1 byte | Mode used for the optional body |
| Inline length | 1 byte | `0..2` |
| Body length | 2 bytes | Decoded optional-body bytes, big-endian |
| Inline bytes | 2 bytes | First packet-body bytes, zero-padded |

Padding after the declared inline bytes must be zero. An unknown version or
mode, invalid inline length, or nonzero inline padding invalidates the header.
The body length is trusted only after the bootstrap CRC succeeds. The current
semantic limits require DATA bodies not to exceed the selected mode's chunk
size and management-body remainders not to exceed 128 bytes; impossible
type/mode/length combinations are rejected before waiting for a body.

A zero-length body is omitted. Empty controls, one-byte mode messages, and the
two-byte DATA_ACK therefore use only the bootstrap. Longer packets place their
first two body bytes inline and their remainder in the announced body mode.
Currently that body is a complete self-synchronizing generic frame with its own
length and CRC. This deliberately pays a second sync so existing waveform
decoders can be dispatched unchanged. A later optimization may replace it with
short mode-specific training whose exact extent is derived from the header.

## Generic mode frame

The bootstrap and, currently, every non-empty body use this frame:

| Field | Size | Description |
| --- | ---: | --- |
| Head pad | `round(0.15 * baud)` bits | Alternating `0,1,...`; discarded |
| Sync | Baud-dependent | One full PN sequence, approximately 0.21 seconds |
| Length | 16 bits | Number of payload bytes, `0..65535`, big-endian |
| Payload | `Length * 8` bits | Bootstrap header or body bytes |
| CRC | 16 bits | CRC-16/CCITT-FALSE over `Length || Payload` |
| Tail pad | `round(0.03 * baud)` bits | Alternating `0,1,...`; discarded |

The sync word is an m-sequence selected to last approximately 0.21 seconds at
the mode's baud. This keeps the acquisition interval, and therefore tolerance
of fixed-duration receiver startup loss, approximately constant across modes:

| Baud | LFSR order | Taps (one-based) | Sync bits | Duration |
| ---: | ---: | --- | ---: | ---: |
| 300 | 6 | 1, 6 | 63 | 210.0 ms |
| 600 | 7 | 1, 7 | 127 | 211.7 ms |
| 1200 | 8 | 1, 3, 4, 8 | 255 | 212.5 ms |

The implementation also defines order 9, taps 1, 3, 5, 9, for a 511-bit sync
at 2400 baud. A non-empty keying currently contains one mode-0 sync for its
bootstrap and one body-mode sync. On each LFSR step the least-significant state bit is emitted,
the configured one-based tap positions are XORed for feedback, the state shifts
right, and the feedback bit enters the highest position. Every sequence starts
with seed 1 and spans the full `2^order - 1` period.

The control profile's order-6 sequence is:

```text
100000111111010101100110111011010010011100010111100101000110000
```

The length field can represent a 65,535-byte payload, but the built-in profiles
do not send frames remotely that large. A complete keying is capped at 3.0
seconds, including framing audio and transport overhead. Each profile's DATA
chunk size is the largest value that fits this budget after the 10-byte robust
bootstrap and body framing, as listed above. At current settings production
DATA keyings occupy 2.566-2.569 seconds of audio and 2.997-2.999 seconds of
total keying time.

The generic length is untrusted until the trailing CRC arrives. A bootstrap
must decode to exactly 10 bytes. A body must equal the length announced by its
already checked bootstrap. To prevent a false sync
and random 16-bit length from making the streaming decoder retain or repeatedly
process an impossible amount of audio, CPFSK rejects a declaration whose frame
would exceed eight seconds from sync through CRC. The receive audio buffer is
capped at ten seconds, and searched audio is pruned while retaining enough for
the longest currently expected frame.

## On-air timing

Before every keying, the sender waits for radio turnaround. The nominal delay
is 0.4 seconds after the estimated end of the peer transmission. When a frame
decodes, its end position anchors that estimate; 0.08 seconds is added for the
peer's nominal 30 ms tail pad and 50 ms keyed carrier hold. Time already spent
capturing and decoding after that point counts toward the delay. Without a
fresh receive-time anchor, the full 0.4 seconds is waited.

The transport keys PTT 0.22 seconds before opening and filling the output
stream. Stream startup contributes a measured worst-case 0.16 seconds before
the first sample leaves the audio device. PTT is held for 0.05 seconds after
playout. Together these add 0.43 seconds to frame audio when calculating total
keying time. Receive capture remains open during transmission, but captured
self-audio is cleared around each local keying. These values are implementation
timing assumptions, not fields negotiated on air.

Control acknowledgement timeouts include the robust bootstrap and any robust
management body. DATA acknowledgement timeouts include the outbound bootstrap
plus negotiated DATA body, the inbound header-only DATA_ACK, two 0.4-second
turnaround allowances, and a three-second margin.
