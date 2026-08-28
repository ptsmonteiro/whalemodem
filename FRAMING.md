# Modulation, coding, and framing

This document describes the modulation, coding, and framing implemented by the
current `whale` package. It defines how a link packet becomes an over-the-air
audio waveform. See [`LINK.md`](LINK.md) for connection management,
mode negotiation, ARQ, and the local TCP interface.

The checked header and connection envelope contain explicit version fields.
The current connection protocol is version 4 and checked air-header version is
2; older versions are rejected.

## Conventions

- Multi-bit integers and bytes are transmitted most-significant bit first.
- One radio keying carries exactly one link packet.
- One keying carries one complete link packet in one waveform. Control packets
  use the robust control mode; DATA packets use the negotiated data mode.

## Processing chain

```text
link packet
  -> checked header and optional body (`whale.framing`)
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
| `0` | `300baud` | 300 | 1200 Hz | 1800 Hz | 88 bytes |
| `1` | `600baud` | 600 | 1200 Hz | 1800 Hz | 193 bytes |
| `2` | `1200baud` | 1200 | 1200 Hz | 2200 Hz | 402 bytes |

Mode 0 is the mandatory robust control profile. Control packets use it for the
complete keying. DATA packets use the negotiated profile for both header and
body. The two directions
are negotiated and adapted independently as described in
[`LINK.md`](LINK.md#mode-adaptation).

The receiver detects the known sync word using normalized correlation. The
current confidence threshold is 0.7. This is a receiver implementation detail,
not an encoded field.

## Coding and error detection

The current profiles do not use forward-error correction or interleaving. A
CRC detects corrupted frames; recovery is performed by the link layer through
acknowledgements and retransmission.

Both CRC fields use CRC-16/CCITT-FALSE. The header CRC covers
`Length || Header`; the optional body CRC covers `Body`. The parameters are
polynomial `0x1021`, initial value `0xffff`, no reflected input or output, and
no final XOR. The CRC byte containing bits 15..8 is sent first.

## Checked packet format

After one mode-specific sync, every keying carries a fixed 10-byte header,
its CRC, an optional body, and a separate body CRC. The header CRC protects
every decoder-control field before the receiver collects the body:

| Field | Size | Meaning |
| --- | ---: | --- |
| Magic | 2 bytes | ASCII `WH` |
| Header version | 1 byte | `0x02` |
| Link packet type | 1 byte | Type defined in `LINK.md` |
| Mode ID | 1 byte | Mode used for this complete keying |
| Inline length | 1 byte | `0..2` |
| Body length | 2 bytes | Decoded optional-body bytes, big-endian |
| Inline bytes | 2 bytes | First packet-body bytes, zero-padded |

Padding after the declared inline bytes must be zero. An unknown version or
mode, invalid inline length, or nonzero inline padding invalidates the header.
The body length is trusted only after the header CRC succeeds. The current
semantic limits require DATA bodies not to exceed the selected mode's chunk
size and management-body remainders not to exceed 128 bytes; impossible
type/mode/length combinations are rejected before waiting for a body.

A zero-length body and its CRC are omitted. Empty controls are header-only.
The first two DATA_ACK bytes are inline; its received-mode and absolute
requested-head bytes form a two-byte control-mode body after the header.
DATA's flags and sent-head-duration bytes are inline. Longer
packets place their first two link-body bytes inline and their remainder after
the checked header in the same waveform.

## Generic mode packet

Each keying uses this structure continuously in its selected mode:

| Field | Size | Description |
| --- | ---: | --- |
| Head pad | `ceil(head_seconds * baud)` bits | Fixed head PN sequence; discarded after measurement |
| Sync | Baud-dependent | One full PN sequence, approximately 0.21 seconds |
| Length | 16 bits | Total header-plus-body bytes, big-endian |
| Header | 10 bytes | Fixed header above |
| Header CRC | 16 bits | CRC over `Length || Header` |
| Body | Declared length | Optional non-inline body bytes |
| Body CRC | 16 bits | CRC over Body; omitted for an empty body |

The head pad is an order-15 maximal-length PN sequence. It uses the same LFSR
step convention described below for sync, seed `0x5a5a`, and these
protocol-fixed taps:

| Pad | Polynomial taps (one-based) | Full period |
| --- | --- | ---: |
| Head | 1, 15 | 32,767 bits |

Calibration heads take the first `ceil(1.0 * baud)` bits. An ordinary head is
the suffix of that calibration sequence whose length is
`ceil(head_seconds * baud)`, using the per-connection duration described in
`ADAPTIVE_TIMING.md`. Anchoring every duration to the same sequence end keeps
the PN phase beside sync unchanged when adaptive feedback changes the head
length. The long period makes symbol slips unambiguous, and the portions used
by the supported profiles exercise both FSK tones approximately equally.

The built-in receiver measures the head backward from sync with a 16-symbol
sliding window. Up to two symbol errors per window
are tolerated. On the third error, the whole failing window is excluded from
the received count, keeping a noisy boundary estimate conservative.

The sync word is an m-sequence selected to last approximately 0.21 seconds at
the mode's baud. This keeps the acquisition interval, and therefore tolerance
of fixed-duration receiver startup loss, approximately constant across modes:

| Baud | LFSR order | Taps (one-based) | Sync bits | Duration |
| ---: | ---: | --- | ---: | ---: |
| 300 | 6 | 1, 6 | 63 | 210.0 ms |
| 600 | 7 | 1, 7 | 127 | 211.7 ms |
| 1200 | 8 | 1, 3, 4, 8 | 255 | 212.5 ms |

The implementation also defines order 9, taps 1, 3, 5, 9, for a 511-bit sync
at 2400 baud. Every keying contains exactly one sync in its selected mode. On
each LFSR step the least-significant state bit is emitted,
the configured one-based tap positions are XORed for feedback, the state shifts
right, and the feedback bit enters the highest position. Every sequence starts
with seed 1 and spans the full `2^order - 1` period.

The control profile's order-6 sequence is:

```text
100000111111010101100110111011010010011100010111100101000110000
```

The length field can represent a 65,535-byte payload, but the built-in profiles
do not send frames remotely that large. Useful framed audio is capped at 3.0
seconds across the complete packet. Here, useful audio is sync, length,
header, body, and CRCs; it excludes the outer head throwaway
symbols and transport startup. Each profile's DATA chunk size is the largest
value that fits this budget. Total keying time is calculated separately and
will vary with the selected timing protection.

The generic length becomes trusted when the fixed header CRC arrives, before
the optional body. The header's body length must agree with the generic length.
To prevent a false sync and random 16-bit length from making the streaming
decoder retain or repeatedly process an impossible amount of audio, CPFSK
rejects a declaration whose frame
would exceed eight seconds from sync through CRC. The receive audio buffer is
capped at ten seconds, and searched audio is pruned while retaining enough for
the longest currently expected frame.

## On-air timing

There is no fixed radio-turnaround sleep. A reply may key as soon as the peer's
final checked CRC has been observed. The calibrated head sequence absorbs
effective clipping caused by peer unkeying, both radios changing direction,
transmitter startup, receiver recovery, and audio buffering.

The transport asserts PTT and immediately opens and fills the output stream;
there is no configured carrier-only lead or tail sleep. Stream startup still
contributes a measured worst-case 0.16 seconds before the first sample leaves
the audio device. Leading protection is carried by a throwaway head sequence:
one second during calibration and the derived per-session duration afterward.
Valid DATA observations can monotonically increase it up to 1.00 second.
There is no tail sequence or guard; audio ends at the final CRC. Receive capture remains open during
transmission, but captured self-audio is cleared around each local keying.

Control acknowledgement timeouts include the complete robust management
packet. DATA acknowledgement timeouts include the negotiated DATA packet, the
robust small DATA_ACK, and a three-second margin.
