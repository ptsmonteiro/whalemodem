# HF4 design record

HF4 is a from-scratch HF-SSB waveform targeting **Level 4** of the HF SSB
speed ladder in `SPEED_LADDERS.md`: maximum speed inside a deliberately
narrow envelope (benign/static fading at +13 dB waveform SNR and above). It
is designed independently of HC0, HC1, HF2, HF3, HR0, and every VF/OFDM/MFSK
experiment in this repository -- no code or geometry from those directories
was read or reused while designing it. The only things this design reuses
are project-wide, mode-agnostic infrastructure that every mode is free to
build on: the generic OFDM/acquisition/equalization/FEC kernels in
`whale.dsp`, the air-header contract in `whale.framing`, and the documented
project-wide conventions in `FRAMING.md` / `MODE_QUALIFICATION.md` /
`SPEED_LADDERS.md` / `CHANNELS.md`.

This is a standalone experiment. It is **not** registered in
`whale.mode_qualification.MANIFEST` and is not offered by any
`ModeRegistry`; it exists as a directly-importable `WaveformMode`
(`whale/modes/hf4_mode.py`) for development, matching how
`experiments/hc2_32qam` exists with no manifest entry.

## Requirements this design was built against

Per the project owner's brief for this mode (overriding the project's usual
2,300 Hz HF ceiling for this waveform specifically):

- Occupied passband must fit within 300-2,700 Hz (2,400 Hz wide).
- Net application throughput per data frame (the exact
  `MODE_QUALIFICATION.md` section 4 formula) must exceed 7,000 bit/s,
  ideally clearing the SPEED_LADDERS.md Level 4 floor of 7,050 bit/s.
- Real IC-7300/IC-705 SSB path: conservative near the band edges, mindful of
  ALC/AGC and typical SSB TX/RX audio passband ripple/rolloff.
- Level 4 envelope only: benign/static fading, +13 dB waveform SNR and
  above (SPEED_LADDERS.md's definition: <=0.1 ms differential delay spread,
  <=0.005 Hz Doppler spread). No design budget was spent on Watterson-class
  fading robustness.

## Why no inner FEC (superseded 2026-09-01 -- see "Inner FEC and
## interleaving" below)

The project's own reference numbers made the shape of a Level-4 waveform
look uncoded. VARA HF's fastest published row is 42 baud/49 carriers 32-QAM
for 7,050 bit/s claimed net; VARA FM's fastest rows run up to 256-QAM.
Comparing raw (uncoded) bits/second implied by carrier count/baud/
modulation order against the *claimed net rate* for VARA HF's fastest row
gives a ratio near 0.68-0.7 -- much lighter redundancy than a rate-1/2 code
(0.5), consistent with a "trust the clean channel, spend the budget on
speed" design at the top of a ladder. This project's own VF6 (the fastest
declared experimental VHF mode, also explicitly a maximum-speed/narrow-
envelope design) makes the same trade even more starkly: no convolutional
inner code at all, only a high-rate outer block code.

HF4's first pass followed that same logic in the simplest way available:
**no inner convolutional code at all**, keeping only a 16-bit length field
and a CRC32, both whitened against a PN sequence (`whale.dsp.bits.pn_bits`)
so a run of identical payload bytes could not look like a long run of one
constellation point. This was a real design trade with a real, and it
turned out fatal, cost: a 2026-09-01 Monte Carlo campaign found this
no-FEC design plateaus at 70-83% frame decode even at 35 dB waveform SNR
against the required benign/static channel -- see "Inner FEC and
interleaving" below for the root cause and the fix, and RESULTS.md's
"second design gap" section for the full evidence. The reasoning above is
kept here, struck through in spirit, because it explains why no-FEC was
tried first and how it failed, not because it still describes this design.

## Carrier plan

Coherent OFDM, one real (not upconverted/mixed) audio-domain IFFT per
symbol, using `whale.dsp.ofdm.Geometry` / `build_symbol` / `symbol_carriers`
/ `carrier_bank` -- the same generic library any OFDM mode in this project
may use, parameterized independently here:

| Parameter | Value |
| --- | ---: |
| Native/RX sample rate | 12,000 Hz |
| TX sample rate | 48,000 Hz (4x upsample via `scipy.signal.resample_poly`) |
| FFT core length | 768 samples |
| Cyclic prefix (guard) | 64 samples (5.33 ms) |
| Carrier spacing | 15.625 Hz |
| Carrier bins | 22-170 inclusive (149 carriers) |
| Carrier frequency range | 343.75-2,656.25 Hz |
| Symbol length | 832 samples (69.33 ms); 14.42 symbols/s |

**2026-09-01 dense-carrier redesign.** FFT core length doubled from 384 to
768 samples (spacing halved from 31.25 Hz to 15.625 Hz, carrier count
doubled from 75 to 149, same 343.75-2,656.25 Hz Hz band) while
`GUARD_SAMPLES` stayed fixed at 64 samples -- see "Guard interval" below for
why that duration is load-bearing and untouched. The point of this change is
purely the standard OFDM CP-overhead-vs-spacing tradeoff: a fixed guard
duration is a smaller *fraction* of a longer symbol, so this halves the
guard's relative overhead from 14.3% (64/448) to 7.7% (64/832) of every
symbol's airtime, freeing raw-bit budget for a stronger inner code (see
"Inner FEC and interleaving" below). This was done to fix the
2026-09-01-fec campaign's finding (only 14/300, 4.67%, frames decoding at
the +13 dB benign/static boundary): `_derive_fec_sizes`'s throughput search
had found no code rate below ~0.895 could clear 7,000 bit/s on the old,
sparser carrier plan, and that thin a code did not survive the frame Monte
Carlo gate. See RESULTS.md's "Dense-carrier redesign" section for the full
rate/frame-length search this motivated and its outcome (a real, ~15x
improvement in +13 dB decode rate that still falls short of the 90% gate).

**2026-09-01 update.** The guard interval was 12 samples (1.0 ms) and the
carrier plan was 12-83 (72 carriers) until a Monte Carlo qualification
campaign found the frame Monte Carlo gate failed completely (0/300
decoded at every tested SNR, 8-20 dB) --
`logs/mode_qualification/hf-ssb/hf4/2026-09-01/INDEX.md`. Root cause: the
guard interval was sized only against `SPEED_LADDERS.md`'s *propagation*
delay-spread figure for benign/static (<=0.1 ms), but a benign/static
qualification channel is also required to retain a real 250-3,100 Hz
bandpass filter, and that filter's own impulse-response memory (several
ms -- an order of magnitude longer than the propagation figure) was the
actual dominant impairment, corrupting every symbol via ISI regardless of
SNR. The fix, applied and reverified in
`logs/mode_qualification/hf-ssb/hf4/2026-09-01-fix/INDEX.md`: the guard
interval was lengthened to 64 samples (5.33 ms, comfortably above the
empirically-measured failure boundary -- see the note by `GUARD_SAMPLES`
in `hf4.py`), and three carriers (two top, one bottom) were added to the
carrier plan, pilot symbols were thinned from 9 to 3, and the lead-in was
trimmed from 0.128 s to 0.096 s, all to recover the airtime the longer
guard now spends and keep net throughput above the 7,000 bit/s floor. See
RESULTS.md for the full before/after numbers.

The native processing rate is 12,000 Hz, matching the shared receive front
end every mode's decoder is handed (`whale/waveform.py`, `FRAMING.md`); no
separate downsample step is needed inside `demodulate()`. Encode upsamples
by an exact integer factor of 4 to the project's standard 48,000 Hz radio
I/O rate.

**Edge margin.** The carrier band runs 343.75-2,656.25 Hz against a
300-2,700 Hz ceiling: 43.75 Hz (2.8 carrier spacings at the current
15.625 Hz spacing; was 1.4 spacings before the 2026-09-01 dense-carrier
redesign halved the spacing without moving the Hz band) of margin at the
bottom and the same 43.75 Hz at the top, on top of the explicit edge
taper below. (Originally 375.0-2,593.75 Hz, 75/106.25 Hz of margin, with
72 carriers; three carriers were added -- one bottom, two top -- in the
2026-09-01 guard-interval fix to recover throughput, narrowing but not
eliminating this margin; see RESULTS.md's occupied-bandwidth campaign for
the measured, binding confirmation this still clears the ceiling.) This
remains deliberately conservative, per the brief's instruction to stay
conservative near the edges of a real SSB radio's TX/RX audio passband,
where ripple and rolloff are worst. HF2's qualification failure
(`MODE_QUALIFICATION.md`) is the cautionary example this design is built
to avoid: an over-ceiling top carrier plus unwindowed OFDM sidelobe leakage
blew its occupied bandwidth to nearly double its 2,300 Hz ceiling. HF4
answers both halves of that failure directly -- carriers stop well short of
both edges, and see "Windowing" below for the leakage half.

**Guard interval.** 64 samples (5.33 ms) at 12 kHz, up from an original
12 samples (1.0 ms). The original figure was sized only against the
benign/static envelope's 0.1 ms differential-delay-*spread* definition in
`SPEED_LADDERS.md` ("about ten times" that figure) and ignored a separate
requirement in the same envelope definition: a benign/static qualification
channel must also retain a real filter/frequency-offset/drift/level/
nonlinearity description, not an identity/AWGN-only channel. The required
250-3,100 Hz 6th-order Butterworth bandpass filter's own impulse-response
memory (several ms -- see the note by `GUARD_SAMPLES` in `hf4.py` for the
measurement) is unrelated to propagation delay spread and turned out to be
the dominant impairment: a 2026-09-01 Monte Carlo campaign found the
original guard left HF4 decoding 0 of 300 frames at every tested SNR from
8-20 dB, confirmed via a noiseless diagnostic (the required filter alone,
no noise) to be pure inter-symbol interference from filter memory the
guard could not absorb, not a marginal SNR effect. 64 samples was chosen
empirically (see `hf4.py`) as comfortably past the point where that
diagnostic starts decoding cleanly and repeatably. A guard this length
still means `whale.dsp.timing`'s cyclic-prefix-based per-symbol
sample-clock estimator (tuned for the much longer guards other HF modes
use) is not reliable here -- see "Timing and frequency tracking" below for
what HF4 does instead.

## Modulation: 16-QAM, Gray-coded

Every one of the 149 carriers on every non-pilot payload symbol carries one
independent 16-QAM point (4 bits/carrier/symbol), using a mapping this
module defines itself (`bits_to_16qam` / `qam16_to_bits`):

```text
LEVELS    = [-3, -1, 1, 3] / sqrt(10)      # unit average energy
BIT_PAIRS = [(0,0), (0,1), (1,1), (1,0)]   # Gray-coded per LEVELS index
```

Real and imaginary axes are mapped independently from 2 bits each, so a
slicing error at the decoder typically lands on a neighbouring constellation
point and costs one bit, not two. 16-QAM (not a higher order) remains the
balance point after the 2026-09-01 FEC fix below added coding: pushing to
64-QAM or higher would buy more raw bits/carrier but cost more Euclidean
distance than the thin FEC overhead budget (see "Inner FEC and
interleaving") could buy back, on top of a real radio path's phase noise,
residual timing jitter, and ALC/AGC behaviour; 16-QAM keeps sizeable
distance between constellation points while still clearing the throughput
target with room to spare (see "Throughput" below).

## Frame structure

| Block | Symbols | Purpose |
| --- | ---: | --- |
| Sync | 4 | Repeated identical known OFDM symbol; self-correlation acquisition |
| Header/training | 3 | Distinct known OFDM symbols; per-carrier channel fit + CFO estimate |
| Payload (data) | 108 | 16-QAM data (rate-11/12 coded, interleaved), all 149 carriers |
| Payload (pilot) | 3 | Full-band known symbol, one after every 36 data symbols |
| **Total** | **118** | |

Total on-air frame: 8.303 s (see "Throughput" for the exact byte/sample
accounting). **2026-09-01 FEC fix:** DATA_SYMBOLS grew from 108 to 360 (up
from 4.527 s total frame time) to buy the extra raw-bit budget the inner
FEC below spends on redundancy while still clearing the 7,000 bit/s floor;
see "Inner FEC and interleaving" for why this was needed and "Throughput"
for the exact rate-vs-frame-length search behind the choice.

**2026-09-01 dense-carrier redesign:** DATA_SYMBOLS dropped back to 108 (its
original pre-FEC-fix value, coincidentally), but for a different reason this
time. With 149 carriers doubling each symbol's raw-bit capacity, a
systematic search across DATA_SYMBOLS (72/108/144/180/216/288/360/540, each
paired with the strongest FEC rate that length could afford above
7,000 bit/s) found *shorter* frames decoding better at +13 dB, monotonically,
even when that meant a weaker code -- DATA_SYMBOLS=540 at rate 13/15 (a
39.1 s frame) scored worse (38% decoded) than DATA_SYMBOLS=108 at rate 11/12
(an 8.3 s frame, 68.3% decoded), despite the longer frame's stronger code.
The shortest length tested, DATA_SYMBOLS=72 (rate 17/18, a 5.7 s frame),
scored best in the scout sweep but was disqualified: it failed
`test_one_dead_carrier_still_decodes` outright, even with the dead
carrier's soft bits weighted to a hard zero (a full erasure) -- at that
frame length, rate 17/18's redundancy and interleaver depth cannot survive
even one fully dead carrier out of 149, regardless of weighting.
DATA_SYMBOLS=108 (rate 11/12) is the shortest frame length found that still
passes that regression test, and is the configuration this design ships
with. See RESULTS.md's "Dense-carrier redesign" section for the full search
and the diagnosis of why frame duration, not just code rate, appears to
dominate this design's +13 dB reliability.

**Sync.** Four repeated, bit-identical OFDM symbols built from one known
QPSK pattern (`whale.dsp.bits.pn_bits` + `qpsk_from_bits`) across all 75
carriers. `whale.dsp.acquire.acquire` finds the frame by normalized
self-correlation at a one-symbol lag over this repeated block -- the same
generic mechanism VF3-style modes use, parameterized on this mode's own
`Geometry` and its own sync length, with no code shared beyond that library
call.

**Header/training.** Three OFDM symbols carrying known values, used for two
things: (1) `whale.dsp.equalize.fit_header`'s two-parameter (gain, offset)
per-carrier least-squares fit, giving both a channel estimate and a
per-carrier SNR; (2) a coarse-then-fine carrier-frequency-offset estimate
(`whale.dsp.freq.coarse_offset_hz` / `fine_offset_hz`). The three header
rows are **not** three independent random draws from the 4-point QPSK
alphabet -- they are one base per-carrier value rotated by 0 deg / 120 deg /
240 deg (`HEADER_VALUES = base * exp(2j*pi*k/3)`). This was a real bug found
during development: independent random header rows collide by chance often
enough (birthday-paradox-style, only 4 constellation points and 3 draws)
that a per-carrier fit becomes exactly singular on some carriers under a
noiseless test channel, producing wildly wrong gain/offset for those
carriers. Rotating a fixed base value guarantees every row differs, for
every carrier, deterministically -- this is a correctness fix, not a
robustness nicety.

**Pilots.** Unlike a frequency-domain comb pilot (reserving some carrier
*bins* for known values on every symbol, at the cost of permanently losing
that fraction of carrier bandwidth to data), HF4 uses full-band pilot
*symbols* in time: after every 36 data symbols, one OFDM symbol carries a
single known value on every carrier. `whale.dsp.equalize.pilot_phase`
interpolates each carrier's residual phase between these anchors (plus the
last header symbol as the payload's leading anchor). This spends payload
*time* (10 of 370 payload symbols, 2.7%, the same ratio as the
guard-interval fix's 3-of-111 -- the 2026-09-01 FEC fix's longer frame
(360, up from 108, data symbols) kept the same PILOT_PERIOD=36, so pilot
count grew proportionally to 10) rather than carrier *bandwidth* on
tracking, which is why every one of the 75 carriers is available for data
on every non-pilot symbol -- the frequency-domain-comb alternative would
have sacrificed a comparable fraction of carriers on every symbol instead,
for no benefit in a benign/static channel whose Doppler spread
(<=0.005 Hz) barely moves the channel across one frame at all, even the
FEC fix's longer ~14-second one. Pilot spacing (every 36 symbols, ~1.3 s)
is deliberately sparse for exactly that reason -- there is essentially
nothing to track quickly on this envelope's channel, so the pilots exist
mainly to correct a slowly accumulating LO/clock artifact, not real
fading.

## Inner FEC and interleaving (added 2026-09-01)

The "Why no inner FEC" reasoning above turned out to be wrong about what
"the channel already meeting its declared envelope" actually looks like.
A frame Monte Carlo campaign after the guard-interval fix
(`logs/mode_qualification/hf-ssb/hf4/2026-09-01-fix/INDEX.md`) found the
no-FEC design decoding 0/300 frames at +13 dB and plateauing at 70-83%
decoded even at 35 dB waveform SNR: independent of noise, the required
benign/static two-path Watterson model puts a handful of the 75 carriers
into a real, frame-static (not transient) fade often enough that a
hard-sliced, uncoded 16-QAM carrier has no bits to recover on those
carriers -- there is no marginal SNR at which "the channel already meets
its envelope" implies "every carrier is usable." The old argument (a
Level-4 frame should trust the clean channel and let ARQ handle the rest)
conflated *aggregate* channel quality with *per-carrier* channel quality;
the required channel's own definition allows the latter to vary even when
the former is excellent.

**The fix is a punctured, interleaved inner convolutional code** -- this
project's shared `whale.dsp.fec.ConvolutionalCode` (`K7`, the K=7 (171,133)
code every VF mode uses) with soft-decision Viterbi decoding, punctured to
the rate below, plus a block interleaver spreading the coded bit stream
across every carrier and every data symbol. At the original 75-carrier plan
this was rate 19/20 (0.95, keeping 20 of every 38 mother-code bits) -- see
"Rate choice" for why, and "Dense-carrier redesign" for what replaced it.

**Rate choice (75-carrier plan, superseded below).** The throughput budget
was the binding constraint, not coding theory: `experiments/hf4/hf4.py`'s
`_derive_fec_sizes` and a direct rate-vs-frame-length search (see
RESULTS.md) found that on the original 75-carrier/31.25 Hz plan, no code
rate below about 0.895 could clear 7,000 bit/s even with an arbitrarily long
frame (the fixed per-symbol overhead -- guard, pilots, sync/header -- sets a
ceiling on how much of the raw bit budget can go to redundancy before the
*asymptotic* throughput itself falls under the floor). Rate 19/20 (0.95) at
360 data symbols was the lightest, shortest combination found that cleared
7,000 bit/s with a real margin (~3%) and survived the synthetic
dead-carrier regression test -- but it did not survive the real benign/
static Monte Carlo campaign at +13 dB (14/300, 4.67% decoded; see
RESULTS.md's "Inner FEC/interleaving fix" section), which is what motivated
the dense-carrier redesign below.

**Rate choice (dense-carrier redesign, current).** Doubling carrier density
(see "Carrier plan" above) roughly doubled the raw-bit budget per unit
time, which raised the affordable-rate floor far below 0.895 in principle
(as low as ~0.843 at very long, multi-minute frames -- see RESULTS.md's
search table) -- but a systematic sweep found that *shorter* frames decode
better at +13 dB than longer ones at a comparable rate, so the design does
not use the lowest rate this carrier plan can afford; it uses the shortest
frame length that (a) clears 7,000 bit/s and (b) survives the synthetic
one-dead-carrier regression test. That combination is **rate 11/12
(0.917) at 108 data symbols** (an 8.3 s frame) -- weaker redundancy than the
carrier plan could technically afford at a longer frame, but empirically
the better performer once frame-duration-scaling carrier-fade sensitivity
is taken into account. See RESULTS.md's "Dense-carrier redesign" section
for the full DATA_SYMBOLS x rate search and the +13 dB Monte Carlo evidence
this choice is based on -- including the shorter, thinner-code candidate
that was tried and rejected because it failed the dead-carrier regression
test even with a hard erasure.

**Puncturing.** `_puncture_keep_indices(k, n)` selects `n` evenly-spaced
indices out of the mother code's `2*k` output bits per period
(`np.linspace(0, 2*k, n, endpoint=False)`), rather than a hand-picked
puncturing table: this design's overhead budget is set by the throughput
search above, not by chasing an optimal free distance at one specific
rate, and even spacing keeps both mother-code output streams represented
in roughly the same proportion. Depuncturing (at the receiver) reinserts
an exact-zero soft value (an erasure) at every position that was not
transmitted; `ConvolutionalCode.decode_soft` treats a zero-magnitude soft
bit as carrying no information, which is exactly what "this bit was never
sent" means.

**Interleaving, and why the obvious construction was wrong.** The coded
bit stream is spread across the `(DATA_SYMBOLS, CARRIER_COUNT *
BITS_PER_CARRIER)` grid with `whale.dsp.interleave.block`, built so that a
single carrier's bits -- concentrated on that one carrier for the *whole*
frame, since this design's failure mode is a fade that is static within
one ~14-second frame, not transient -- land on source-code positions
`CARRIER_COUNT * BITS_PER_CARRIER` (300) apart, as spread out as this
frame's coded block allows. The first construction tried
(`rows=CARRIER_COUNT*BITS_PER_CARRIER, columns=DATA_SYMBOLS`, no extra
transpose needed to lay it into the symbol grid) looked more natural but
was wrong: it maps one fixed carrier-bit lane, followed across every data
symbol, onto `DATA_SYMBOLS` *consecutive* positions in the coded stream --
a burst tens of times longer than the K=7 code's memory, which Viterbi
cannot recover from regardless of code rate. This was caught by
`test_hf4.py`'s `test_one_dead_carrier_still_decodes` regression (a single
synthetically dead carrier, unable to decode with the wrong interleaver
construction, decodes cleanly with the current one). `INTERLEAVER`'s
comment in `hf4.py` and `_to_symbol_grid`/`_from_symbol_grid` carry the
exact construction and the reasoning.

**Per-carrier reliability weighting.** Interleaving alone spreads a bad
carrier's *errors* evenly, but a hard 50%-wrong bit is still a poor input
to a soft decoder if it arrives at full confidence -- what actually lets
the code correct a faded carrier is treating its bits as *unreliable*
(near-erasures) rather than as full-confidence-but-wrong. `demodulate`
already computes a per-carrier SNR from the header fit
(`whale.dsp.equalize.fit_header`'s `ChannelFit.snr_db`); the fix reuses the
existing `whale.dsp.equalize.carrier_weights` helper (widened to
`low=0.05, high=4.0`, more aggressive than its 0.5-2.0 default) to turn
that per-carrier SNR into a soft-bit confidence multiplier before
depuncturing and Viterbi decoding. Without this weighting, the synthetic
one-dead-carrier regression test still fails even with correct
interleaving, because a "confidently wrong" carrier misleads the Viterbi
metric more than an honestly-uncertain one; see
`_data_values_to_packet_bits`'s docstring for the mechanism.

**What this does and does not fix.** This is a real, if thin, margin
against a *small number* of badly-faded carriers per frame, at the rate
the throughput budget can afford (verified in isolation against 1+
synthetically dead carriers in `test_hf4.py`, and against the real
benign/static channel in the Monte Carlo campaign -- see RESULTS.md for
the actual pass/fail). It is not Watterson-class fading robustness: Level
4's envelope still does not ask for that, and a channel that puts many
more than a handful of the 75 carriers into deep fades at once is still
expected to fail, by design.

## Timing and frequency tracking

Carrier-frequency offset is corrected in two stages against the header
block, both from the shared `whale.dsp.freq` library: a coarse estimate
from the cyclic-prefix correlation angle, then a fine estimate from the
per-symbol phase step across the known header. This is a genuine per-frame
constant this waveform's own header can estimate cleanly regardless of
guard length, so it is corrected before any other analysis.

**Per-symbol sample-clock tracking is deliberately not used.** Development
testing found that `whale.dsp.timing.estimate`'s cyclic-prefix correlation
search (window width tuned for the much longer guards other HF modes carry)
is unreliable against HF4's 12-sample guard: on a perfectly clean,
zero-drift test signal it still reported a spurious ~100+ ppm "drift," and
applying that estimate's per-symbol shift to `carrier_bank` measurably
*increased* decode error rather than reducing it. Given this design's
target envelope already assumes negligible sample-clock drift over one
~4-second frame (this is what "benign/static" and its 0.005 Hz Doppler
figure mean), and acquisition already locates the frame boundary to the
sample via the sync block's self-correlation, HF4 simply does not attempt
finer per-symbol timing correction. This is a real, explicit scope
limitation: a future revision aimed at a wider envelope would need either a
longer guard interval or a timing estimator matched to this one, not this
same fixed-intercept assumption.

## Windowing / spectral containment

This is the design element HF2's qualification failure most directly calls
out (`MODE_QUALIFICATION.md`: "spectral leakage in HF2's own OFDM symbol
generation, most likely missing or insufficient inter-symbol windowing").
HF4 addresses it at two levels:

1. **No per-symbol windowing needed at internal symbol boundaries.** Every
   symbol's cyclic prefix already makes the waveform continuous
   symbol-to-symbol (this is what a cyclic prefix is for), so there is no
   internal discontinuity for a window to fix.
2. **An explicit raised-cosine taper at the two hard edges of the burst**
   (silence-to-signal at the start, signal-to-silence at the end), 32
   samples (2.67 ms) each. The straightforward implementation -- multiply
   the first/last samples of the modulated block by a ramp -- was tried
   first and rejected: those samples are the actual content of the
   first/last real symbol's core, and tapering them corrupts exactly the
   bits a decoder needs from that symbol (this was caught by the round-trip
   test: it silently degraded only the last several data symbols before the
   final pilot, growing worse toward the very end of the frame, because the
   final pilot symbol -- the phase-tracking anchor those symbols interpolate
   against -- was itself the one being windowed). The fix implemented is to
   prepend/append a **tapered copy** of the first/last `EDGE_WINDOW_SAMPLES`
   around the untouched core audio, so the burst still ramps smoothly into
   and out of silence but every sample a decoder actually analyzes is
   exactly what `build_symbol` produced. This costs 64 extra samples total
   (5.3 ms), included in the throughput accounting below.

A 99%-power FFT sanity check on the actual encoder output (see RESULTS.md)
confirms the result stays inside 300-2,700 Hz with a comfortable margin at
both edges -- this is the concrete evidence that the taper plus carrier edge
margins jointly control leakage, not just the intent.

## Peak/RMS (ALC/AGC headroom)

Seventy-two independently modulated carriers produce a high crest factor
(observed peak/RMS is dominated by the deterministic sync/header block,
which is the same every frame, and comes out around 7.3x). The target RMS
(`_TARGET_RMS = 0.115`, applied via `Geometry.scaled_to_rms`) was chosen so
the worst-case observed peak sample stays comfortably below full scale
(~0.84 of full scale in testing, about 1.5 dB of headroom) rather than
letting an IC-7300/IC-705 SSB transmitter's ALC compress the waveform's
envelope, which would reintroduce the exact spectral splatter the edge
taper is meant to prevent.

## Framing and error detection

```text
[2-byte big-endian length][payload][4-byte big-endian CRC32][zero fill]
```

whitened against a PN sequence (own fixed seed, distinct from every other
seed this module uses), matching the wire-format shape documented
project-wide for `whale.dsp.framing.PacketCodec` -- but implemented directly
here rather than through that helper, because `PacketCodec` is built around
carrying a `ConvolutionalCode`, and HF4 deliberately carries none (see "Why
no inner FEC" above).

## Acquisition and reliability

`ACQUISITION_THRESHOLD = 0.60` gates on `whale.dsp.acquire`'s normalized
self-correlation score of the four-symbol repeated sync block, in the same
units and mechanism the shared library documents. This has not yet been
tuned against a promotion-sized Monte Carlo campaign (out of scope for this
pass -- see RESULTS.md); it is set from the same proposal-threshold family
the shared acquisition library defaults to.

## What this design explicitly does not attempt

- **No Watterson-class fading robustness.** A rate-11/12 inner code
  (rate-19/20 before the 2026-09-01 dense-carrier redesign) plus
  interleaving and per-carrier reliability weighting (2026-09-01, see
  "Inner FEC and interleaving") gives real, measured margin against
  per-carrier fading -- a ~15x improvement in +13 dB frame decode rate
  after the redesign (4.67% -> 70.33%, see RESULTS.md) -- but still falls
  short of the 90%+ Monte Carlo gate at the +13 dB benign/static boundary,
  and this mode is not expected to work outside the benign/static envelope
  at all, by design.
- **No fine sample-clock tracking**, for the reason explained above.
- **No Monte Carlo qualification, hardware testing, or MANIFEST
  registration.** Those are explicitly out of scope for this pass (see
  RESULTS.md for exactly what has and has not been measured).
