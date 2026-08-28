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

Control packets use the registry's control mode for the complete keying. DATA
packets use the negotiated mode for both header and body. The two directions
are negotiated and adapted independently as described in
[`LINK.md`](LINK.md#mode-adaptation).

Which mode is the control mode is a property of the registry, and therefore of
the channel. On the VHF FM ladder it is mode `0`, and modes `0..2` above are
that ladder. On the HF SSB ladder it is mode `4` (HC1, below) and the CPFSK
profiles are not offered at all: they carry no carrier-frequency estimate, so
on SSB they are not a robust fallback but a mode that stops working as soon as
the two stations disagree about frequency. See `whale/policy.py`, which pairs
each `ChannelPolicy` with its ladder.

Mode IDs are global across channels: an ID names one waveform everywhere, even
where no registry offers two of them together.

### Mode 3: VF3, a non-CPFSK DATA mode

Mode `3` is the first mode that is not CPFSK and not framed by this document.
It is `whale.modes.vf3_mode.VF3`: a 58-carrier differential-QPSK OFDM frame
over 468.75-3140.625 Hz, carrying 1,426 DATA bytes in a fixed 5.2 s waveform,
with its own acquisition header, length field, CRC32 and rate-1/2
convolutional code in place of everything under "Framing" below. It reaches
the link only through the same `encode()` / `decode()` / `airtime()` contract,
which is the point: nothing in connection management, ARQ or negotiation
changed to accommodate it.

It is not in `afsk.default_registry()`, which stays the CPFSK-only ladder; it
is the top rung of `whale.modes.default_registry()`, which is what a station
on the VHF FM channel actually runs. VF3 passed 6/6 full-capacity frames in
each direction on the bench with ARQ bypassed (`experiments/vf3/RESULTS.md`)
and has since carried acceptance sessions over the air in both directions.

Two consequences of it being a DATA mode only. The control plane stays on mode
0, so a station that cannot decode VF3 still completes CONNECT and still
receives DATA once the ISS steps down. And because a VF3 keying is 5.2 s
whatever it carries, a short packet would waste the difference -- which is
harmless, since control packets never ride a DATA mode.

### Mode 4: HC1, the HF control and data mode

Mode `4` is `whale.modes.hc1_mode.HC1`, and it is what mode 0 is on FM: the
control plane and the data plane of an HF SSB link, both at once. It is the
only mode in `whale.modes.hf_registry()`.

| Property | Value |
| --- | --- |
| Symbol | 128-sample cyclic prefix + 512-sample core = 640 samples, 13.33 ms |
| Carrier spacing | 93.75 Hz |
| Carriers | 19, FFT bins 7-25, 656.25-2343.75 Hz |
| Modulation | Differential QPSK, per carrier, across symbols |
| Header | 5 repeated sync + 8 varying training symbols |
| Payload grid | 34 symbols x 19 carriers x 2 = 1,292 coded bits |
| FEC | Interleaved rate-1/2, K=7 convolutional code, soft-decision Viterbi |
| Error detection | 16-bit length + CRC32, inside the coded payload |
| User payload | 74 bytes, of which 64 are a DATA chunk after the air header |
| Frame | 2,304 lead-in + 47 x 640 + 960 tail = 33,344 samples = 0.695 s |
| Offset tolerance | +-46.875 Hz (half a carrier spacing) |

Like VF3 it brings its own framing: acquisition is the OFDM header rather than
a PN correlation, and the length, CRC32 and FEC live inside the payload grid,
so nothing under "Framing" below applies to it. Unlike VF3 it is not a DATA
mode only, which is the whole point -- on HF the control plane needs the
frequency correction and the coding more than the data plane does.

What it does that no earlier mode does is **correct the carrier frequency
offset**. Two SSB receivers reproduce a transmitted audio frequency offset by
the difference between the stations' reference oscillators; the bench pair
(IC-7300 and IC-705 on 10.145 MHz) measures about 8 Hz, and half a ppm each
way at 14 MHz would be 14 Hz. The offset is estimated twice: coarsely from the
cyclic-prefix correlation angle, which is undone in the time domain because an
offset of tens of Hz against a 93.75 Hz spacing leaks each carrier into its
neighbours; then finely from the per-symbol phase step across the known
header, which is removed as one phase per symbol on the analyzed carriers. Both
estimators are `whale.dsp.freq`, which existed as a VF3 diagnostic before HC1
had a use for it.

A consequence worth stating: an HC1 keying is 0.695 s whatever it carries, so
a 12-byte DATA_ACK costs the same air as a 64-byte chunk. That is accepted
rather than worked around. A variable-length OFDM frame needs the receiver to
learn the length before it can decode, which means a separately coded header,
and the airtime it would save is small next to what an HF keying already
spends on PTT, ALC settling and turnaround. What the fixed frame buys is that
every frame on the link, control included, gets the full FEC, CRC32 and
frequency correction.

The head is a repeat of the 512-sample sync core, quantized to a whole number
of cores plus half a core. The half core is not decoration: a head ending on a
core boundary reproduces a complete sync symbol in its own last 640 samples,
which widens the acquisition correlation's plateau to 640 samples and leaves
the start index to numerical noise.

For the CPFSK profiles, the receiver detects the known sync word using
normalized correlation. The current confidence threshold is 0.7. This is a
receiver implementation detail, not an encoded field. VF3 and HC1 use their
own acquisition thresholds against a different measure -- the normalized
self-correlation of their repeated sync symbols -- and report it through the
same `confidence` key.

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

The length field can represent a 65,535-byte payload, but the CPFSK profiles
do not send frames remotely that large. Useful framed audio is capped at 3.0
seconds across the complete packet. Here, useful audio is sync, length,
header, body, and CRCs; it excludes the outer head throwaway
symbols and transport startup. Each profile's DATA chunk size is the largest
value that fits this budget. Total keying time is calculated separately and
will vary with the selected timing protection.

The 3.0 second figure is a property of the CPFSK profiles, not of the modem:
it is `afsk.MAX_USEFUL_FRAME_SECONDS`, and the reasons for it (retransmit
granularity, half-duplex responsiveness, and a rigid symbol grid with no
timing recovery) are CPFSK's. A mode that answers them differently may key
for longer. VF3 does: its cyclic prefix and per-carrier equalisation give it
timing tolerance CPFSK has no equivalent of, and it keys for 5.2 s. Keyings of
around five seconds are acceptable in this project.

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
