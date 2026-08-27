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
payload limit, synchronization confidence threshold, and stable on-air mode
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
| `0` | `300baud` | 300 | 700 Hz | 1300 Hz | 40 bytes |
| `1` | `600baud` | 600 | 700 Hz | 1500 Hz | 100 bytes |
| `2` | `1200baud` | 1200 | 1200 Hz | 2200 Hz | 100 bytes |

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
| Head pad | `round(0.08 * baud)` bits | Alternating `0,1,...`; discarded |
| Sync | 63 bits | Fixed PN sequence described below |
| Length | 8 bits | Number of payload bytes, `0..255` |
| Payload | `Length * 8` bits | One link packet |
| CRC | 16 bits | CRC-16/CCITT-FALSE over `Length || Payload` |
| Tail pad | `round(0.03 * baud)` bits | Alternating `0,1,...`; discarded |

The sync word is generated from a six-bit LFSR with polynomial
`x^6 + x + 1`, seed 1, and a period of 63. On each step the least-significant
state bit is emitted, bits at one-based positions 1 and 6 are XORed for
feedback, the state shifts right, and the feedback bit enters position 6. The
resulting on-air bit string is:

```text
100000111111010101100110111011010010011100010111100101000110000
```

The maximum framed payload is 255 bytes. Since the first payload byte is the
link packet type, a link packet body can contain at most 254 bytes. Normal DATA
chunks are deliberately smaller, as listed in the profile table.

## On-air timing

Before every keying, the sender waits for radio turnaround. The nominal delay
is 0.4 seconds after the estimated end of the peer transmission. The estimate
adds 0.08 seconds for the peer's tail pad and carrier hold after the last CRC
bit. Without a fresh receive-time anchor, the full 0.4 seconds is waited.

The transport keys PTT 0.22 seconds before playing audio and holds it for 0.05
seconds after playout. Receive capture remains open during transmission, but
captured self-audio is cleared around each local keying. These values are
implementation timing assumptions, not fields negotiated on air.

Timeouts are derived from expected frame airtime, two turnaround allowances,
and a three-second margin. Control acknowledgements use a mode-0 frame estimate
plus three seconds. DATA timeouts account independently for the outbound DATA
profile and inbound ACK profile.
