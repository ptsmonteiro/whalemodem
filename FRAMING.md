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
- A frame carries exactly one link packet.
- One radio keying contains exactly one frame. Multiple frames are not bundled
  into a burst.

## Processing chain

```text
link packet
  -> length and CRC frame (`whale.framing`)
  -> continuous-phase binary FSK audio (`whale.afsk`)
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
| `0` | `300baud` | 300 | 1200 Hz | 1800 Hz | 75 bytes |
| `1` | `600baud` | 600 | 1200 Hz | 1800 Hz | 157 bytes |
| `2` | `1200baud` | 1200 | 1200 Hz | 2200 Hz | 320 bytes |

Mode 0 is the control profile. CONNECT, CONNECT_ACK, DISC, DISC_ACK, MODE_REQ,
MODE_ACK, FLOOR_REQ, and FLOOR_GRANT always use it. DATA and DATA_ACK use the
negotiated profile for the direction in which they travel. The two directions
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

## Frame format

Every link packet is carried as the payload of this frame:

| Field | Size | Description |
| --- | ---: | --- |
| Head pad | `round(0.15 * baud)` bits | Alternating `0,1,...`; discarded |
| Sync | Baud-dependent | One full PN sequence, approximately 0.21 seconds |
| Length | 16 bits | Number of payload bytes, `0..65535`, big-endian |
| Payload | `Length * 8` bits | One link packet |
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
at 2400 baud. On each LFSR step the least-significant state bit is emitted,
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
chunk size is the largest value that fits this budget after the two-byte DATA
packet overhead (packet type plus flags/sequence), as listed above. At current
settings the production DATA frames occupy 2.55-2.566 seconds of audio and
2.98-2.996 seconds of total keying time.

The length is untrusted until the trailing CRC arrives. To prevent a false sync
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

Control acknowledgement timeouts are the mode-0 airtime of an estimated
32-byte frame plus three seconds. DATA acknowledgement timeouts are the
outbound full DATA-frame airtime plus the inbound three-byte DATA_ACK airtime,
two 0.4-second turnaround allowances, and a three-second margin. The two
airtimes use the independently negotiated profile for their respective
directions.
