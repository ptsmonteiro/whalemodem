# Modulation, coding, and framing

This document describes the modulation, coding, and framing implemented by the
current `whale` package. It defines how a link packet becomes an over-the-air
audio waveform. See [`LINK.md`](LINK.md) for connection management,
mode negotiation, ARQ, and the local TCP interface. See
[`CHANNELS.md`](CHANNELS.md) for the simulated-channel boundary, trial result
schema, and SNR conventions used to qualify these waveforms, and
[`MODE_QUALIFICATION.md`](MODE_QUALIFICATION.md) for registry promotion gates
and the status of the current modes.

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
  -> 48 kHz waveform audio (`whale.afsk` for the CPFSK modes)
  -> keyed, half-duplex radio (`whale.transport`)

radio capture at 48 kHz
  -> one stateful anti-aliased 4:1 decimator (`whale.rx_audio`)
  -> 12 kHz receive buffer
  -> candidate waveform decoders
```

## Physical-layer interface

The link protocol depends on the `WaveformMode` contract in
`whale.waveform`, not on CPFSK functions directly. A mode supplies its own
packet encoder, streaming-buffer decoder, airtime calculation, separate
transmit and receive sample rates,
DATA chunk size, synchronization confidence threshold, and stable on-air mode
ID. `ModeRegistry` provides the ordered set used for negotiation/adaptation
and identifies the robust control-plane mode.

The modes below are `whale.afsk.Profile` instances backed by `CpfskCodec`.
`Profile` retains the CPFSK-specific tone and baud settings, while satisfying
the generic link-facing contract through `encode()`, `decode()`, and
`airtime()`. A waveform with different modulation, synchronization, framing,
or FEC can implement the same contract and be installed in a registry without
changing connection management or ARQ.

### Experimental HC2 coherent-32QAM rate proof

`experiments/hc2_32qam/` is a speed-first prototype rather than a
`WaveformMode`. It uses 49 carriers, a 1,024-sample FFT, 128-sample cyclic
prefix, 41.667 transmitted symbols/s, coherent rectangular 32QAM, and rate-3/4
punctured convolutional coding. Its full 2,749-byte payload occupies 2.928
seconds including two known training symbols -- two *different* full-band QPSK
sequences, so frame acquisition has one unambiguous correlation peak -- for
7,510.93 bit/s of user payload.

It is intentionally absent from every registry and from HF negotiation. It has
an oracle-aligned receiver and a real acquisition/CFO/equalization/phase-tracking
one, an AWGN FER/EVM screen, and a Watterson boundary sweep
(`experiments/hc2_32qam/RESULTS.md`), but no sample-clock tracking, no
radio-linearity evidence, and no link air header, ARQ, or PTT accounting.

The fading sweep is what bounds it: HC2 delivered 0 of 300 frames against the
`mid_latitude_quiet` preset at every SNR from 11.5 dB to 40 dB, and
parametrically it needs better than about 0.1 ms of differential delay and
0.005 Hz of frequency spread. Differential delay binds first, well inside the
2.67 ms cyclic prefix, because one front-loaded channel estimate cannot ride
out frequency-selective nulls across a 2,250 Hz band. Its results therefore
establish codec and waveform rate feasibility, a benign-channel SNR floor, and
an operating envelope narrower than any standard HF fading preset; they are
not evidence that the mode works over a real HF path.

## Modulation profiles

Transmitted audio is mono, 48 kHz, continuous-phase binary FSK. A zero or one selects the
corresponding profile tone for one symbol. The complete waveform has a 5 ms
amplitude ramp at its start and a nominal amplitude of 0.6. There is no
waveform tail guard; ordinary streaming capture and radio turnaround retain
the receive-filter response after the final symbol.

All current receive decoders consume the shared 12 kHz buffer. The sound card
still captures at 48 kHz; `whale.rx_audio` low-pass filters it and decimates it
once before the link tries any candidate mode. Consequently decoder sample
indices, buffer consumption, and trailing-audio timing are in 12 kHz units,
while encoder lengths and documented on-air sample counts remain in 48 kHz
units. The filter has a 64-sample input/16-sample output group delay; ordinary
radio turnaround provides its tail, and the adaptive head absorbs its startup.

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
that ladder. On the HF SSB ladder it is mode `5` (HC0, below), with mode `4`
(HC1) above it as the fast rung; the CPFSK profiles are not offered at all,
because they carry no carrier-frequency estimate and so on SSB they are not a
robust fallback but a mode that stops working as soon as the two stations
disagree about frequency. Mode `7` (HF2, below) is a further, experimental-
only rung above HC1, not offered by default. See `whale/policy.py`, which
pairs each `ChannelPolicy` with its ladder.

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
is the fastest current rung of `whale.modes.default_registry()`, which is what
a station on the VHF FM channel actually runs. VF3 passed 6/6 full-capacity frames in
each direction on the bench with ARQ bypassed (`experiments/vf3/RESULTS.md`)
and has since carried acceptance sessions over the air in both directions.

Two consequences of it being a DATA mode only. The control plane stays on mode
0, so a station that cannot decode VF3 still completes CONNECT and still
receives DATA once the ISS steps down. And because a VF3 keying is 5.2 s
whatever it carries, a short packet would waste the difference -- which is
harmless, since control packets never ride a DATA mode.

### Mode 6: VF6, the fastest current experimental VHF data mode

Mode `6` keeps VF5's 58-carrier, 5.200-second pilot-assisted OFDM geometry,
uses Gray square 256-QAM, and deliberately targets excellent FM channels. Its
87,696-bit grid holds 10,962 bytes. Forty-three byte-interleaved shortened
RS(254,238) codewords use 10,922 bytes, leaving 40 unused; their 10,234 data
bytes contain length, up to 10,228 packet bytes, and CRC32. Removing the
link's ten-byte air header gives a 10,218-byte DATA chunk. VF6 has no
convolutional inner code and is offered only by the experimental VHF registry.

### Mode 4: HC1, the fast HF data mode

Mode `4` is `whale.modes.hc1_mode.HC1`, the upper rung of
`whale.modes.hf_registry()`. It carries five times HC0's payload rate on a
path that can hold it, and gives out about 19 dB sooner, which is why it sits
above HC0 rather than replacing it.

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
| Frame | 6,144 common HF lead + 47 x 640 + 960 tail = 37,184 samples = 0.775 s |
| Offset tolerance | +-46.875 Hz (half a carrier spacing) |

Like VF3 it brings its own framing: acquisition is the OFDM header rather than
a PN correlation, and the length, CRC32 and FEC live inside the payload grid,
so nothing under "Framing" below applies to it.

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

A consequence worth stating: an HC1 keying is 0.775 s whatever it carries, so
a 12-byte packet costs the same air as a 64-byte chunk. Both HF modes are
fixed-length for the same reason: a variable-length frame needs the receiver
to learn the length before it can decode, which means a separately coded
header, and the airtime it would save is small next to what an HF keying
already spends on PTT, ALC settling and turnaround.

**HC1 is not the control mode**, and the measurement that decided that
belongs here rather than only in the code. Its `confidence` is the normalized
self-correlation of its repeated sync symbols, whose expected value is exactly
`SNR/(SNR+1)`; the 0.70 threshold it inherited from VF3 is therefore a 3.7 dB
SNR floor, and lengthening the preamble does not move it, because that shrinks
the correlation's variance and not its mean. Handed the true frame start,
HC1's payload decodes down to -4 dB. As actually decoded it needs +3.5 dB. On
the bench's weak leg -- about -8 dB -- it decoded 0 frames out of 10.

The link wrapper replaces HC1's original sync-core guard with the common HF
lead described below. HC1 acquisition still locks on its own OFDM header, so a
clipped or uncertain lead hint cannot prevent checked-frame recovery.

The receiver ranks at most 32 distinct lead boundary hypotheses. At each
boundary it offers both HC0 and HC1 interpretations in correlation-score
order; neither the label nor the boundary is authoritative. A result is used
only when the waveform's FEC/CRC succeeds and the checked air header names the
same mode and a valid packet shape. After the ranked attempts, the receiver
always performs one ordinary whole-buffer acquisition per eligible HF mode,
so an erased, wrong, or sub-threshold lead cannot hide a valid body. This also
bounds one poll to 64 hinted attempts plus one fallback attempt per mode.

### Mode 5: HC0, the HF control mode

Mode `5` is `whale.modes.hc0_mode.HC0`, the bottom rung and the control mode
of the HF SSB ladder: every CONNECT, CONNECT_ACK, DISC, DATA_ACK,
floor-control and timing frame rides it, and a DATA transfer falls back to it
when nothing faster holds. It is what mode 0 is on FM.

It is not OFDM and not coherent. Information is **which of 16 tones is
present**, detected as energy, so no part of the receive path holds a phase
reference: not the demodulator, not the synchronizer, and not the frequency
estimator, which measures the offset but is never gated on it.

| Property | Value |
| --- | --- |
| Symbol | 512 samples, 10.667 ms |
| Symbol rate | 93.75 baud -- also the tone spacing and the FFT bin width |
| Tones | 16, FFT bins 8-23, 750-2156.25 Hz, Gray coded |
| Occupied bandwidth | 1.5 kHz |
| Modulation | Non-coherent orthogonal 16-FSK, phase-continuous, constant envelope |
| Preamble | 24 symbols: 12 PN-drawn tones, each sent twice |
| Payload grid | 283 symbols x 4 = 1,132 coded bits |
| FEC | Interleaved rate-1/2, K=7 convolutional code, soft-decision Viterbi |
| Error detection | 16-bit length + CRC32, inside the coded payload |
| User payload | 64 bytes, of which 54 are a DATA chunk after the air header |
| Frame | 6,144 common HF lead + 307 x 512 + 960 tail = 164,288 samples = 3.423 s |
| Offset tolerance | +-46.875 Hz (half a tone spacing) |

Measured against HC1 at equal transmitted RMS, white noise across the whole
band: **HC1 fails below +3.5 dB and HC0 decodes to -16 dB.** About 7 dB of
that is spending five times the airtime; the rest is not paying for coherence.
And that comparison is at equal RMS -- HC0's waveform is constant-envelope,
crest factor 1.41 against HC1's 3.9, so through the same peak-limited
transmitter it delivers roughly 8 dB more average power again.

Three details follow from being non-coherent:

- **Detection is a correlation against a known tone pattern**, not against the
  signal itself, so its processing gain grows with preamble length in the
  ordinary way. The tone magnitudes have the across-tone mean removed at each
  instant before correlating; without that subtraction the statistic scores
  the magnitudes' common component against itself and reads pure noise as a
  lock, which is the failure `experiments/mfsk` records measuring at 0.73
  against a 0.70 threshold. Centred, a real preamble scores 0.20 at -16 dB
  where noise, a bare carrier and an HC1 frame all sit at 0.06-0.08. The
  threshold is 0.12.
- **The preamble is 12 tones each sent twice.** A symbol's measured phase
  carries a symbol-timing term that depends on which tone it used, so between
  two different tones a timing error leaks straight into the frequency
  estimate -- 48 samples of it, well inside what the detector tolerates, read
  a zero offset as -13.5 Hz. Across a pair sharing a tone that term is
  identical and cancels, so the estimate is exact at any timing.
- **Symbol timing is refined on matched tone energy**, not on the detection
  score. That score saturates: once the right tone dominates every symbol it
  reads its ceiling whatever the timing, flat to within 1e-9 across +-48
  samples.

### Mode 7: HF2, an experimental faster HF data mode

Mode `7` is `whale.modes.hf2_mode.HF2`, a pilot-assisted coherent 16-QAM OFDM
mode built and qualified in `experiments/hf2/` as a from-scratch design
independent of HC0/HC1/VF6/HR0 (see `experiments/hf2/DESIGN.md`), then wired
into `whale/modes/` as a thin `WaveformMode` adapter over the unchanged
experiment module, the same shape `hc1_mode.py` uses over `hc1.py`. It
targets Level 2 of the HF SSB speed ladder (`SPEED_LADDERS.md`):
general-purpose data, quiet Watterson fading at +5 dB and above, moderate at
+10 dB and above. 19 carriers (656.25-2343.75 Hz, 93.75 Hz spacing) carry 8
comb pilots and 11 16-QAM data carriers grouped into 5 logical carriers with
physical frequency diversity (each logical value repeated on 2-3 carriers
spread across the band, LLR-combined at the receiver) to survive persistent
Watterson notches; framing is the same rate-1/2 K=7 convolutional code,
CRC32 and length field used inside the payload grid as HC0/HC1/VF3. A frame
carries 117 payload bytes (107 after the link's air header) in 109 symbols /
0.775 s of frame body plus the common HF lead.

`experiments/hf2/RESULTS.md` records the qualifying Monte Carlo evidence: a
>=300-trial confirmed boundary at both required Level 2 envelope points
(`mid_latitude_quiet` +5 dB and `mid_latitude_moderate` +10 dB), clearing
`MODE_QUALIFICATION.md`'s FER/acquisition gate with useful throughput of
about 577-585 bit/s -- above the 500 bit/s floor, but by a thin margin (see
that document's caveats). HF2 is registered as EXPERIMENTAL only
(`whale/mode_qualification.py`); it is not offered by any default or
optional registry and carries no hardware, session, or ARQ evidence yet.

### Common HF lead and frame signature

Every HC0, HC1 and HF2 keying begins with the same lead modulation and
symbol rate: HC0's constant-envelope non-coherent 16-FSK bank at 93.75 baud.
The minimum lead is a six-symbol identity block repeated twice, or 12
symbols / 6,144 samples / 128 ms. Adaptive protection extends it in complete
six-symbol blocks, repeating the same identity block throughout.

| Following mode | Tone indices |
| --- | --- |
| HC0 | `9 6 12 15 0 3` |
| HC1 | `12 3 15 6 9 0` |
| HF2 | `2 11 5 14 8 0` |

The detected label may order decoder attempts, but is not authenticated.
Leading clipping can erase it and noise can produce a wrong candidate, so all
eligible decoders remain fallbacks and only the following checked payload
selects the frame. After a checked decode, the receiver counts matching blocks
backward from the waveform body to measure surviving lead duration.

This lead is HF-specific. VHF/FM CPFSK and VF3 leads remain unchanged; a
future FM-optimized common lead belongs at the same channel-level composition
boundary rather than changing this HF geometry.

For the CPFSK profiles, the receiver detects the known sync word using
normalized correlation. The current confidence threshold is 0.7. This is a
receiver implementation detail, not an encoded field. VF3 and HC1 use their
own acquisition thresholds against a different measure -- the normalized
self-correlation of their repeated sync symbols -- and report it through the
same `confidence` key.

Every successfully decoded packet produces a link log entry with its receive
SNR. HC0 reports winning-tone power against the mean power of the other 15
tones. VF3 and HC1 report the median of their finite per-carrier header SNR
estimates. CPFSK fits the two known tones over the confirmed sync word and
reports fitted signal power against residual power. That residual includes
noise, interference, distortion, clipping, and timing error, so the log calls
it `effective sync` SNR. Normalized sync confidence is not relabeled as SNR
because the two quantities are not interchangeable.

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
