# HF4 (hf-ssb mode ID 11, unregistered/experimental) -- 2026-09-01 simulated qualification campaign

## Target rung and disposition

HF4 declares HF SSB speed-ladder **Level 4** ("Maximum speed",
`SPEED_LADDERS.md`): minimum useful net application throughput 7,050 bit/s
(project owner's floor: 7,000 bit/s), required envelope benign/static at
+13 dB waveform SNR and above. This campaign runs the frame Monte Carlo
gate (`MODE_QUALIFICATION.md` section 3) at that boundary and the
occupied-bandwidth gate (section 4) against the project owner's
300-2,700 Hz ceiling for this mode.

**Result: the frame Monte Carlo gate fails completely.** At the confirmed
tier (300 trials, +13 dB waveform SNR, benign/static channel), HF4 decoded
**0 of 300 frames** (95% Wilson-UB FER = 1.000, far above the <=0.10 gate).
This is not a marginal miss: a scouting sweep at 8/10/11/13/15/20 dB (100
trials each) also decoded 0/100 at every point, including 20 dB, 7 dB above
the declared boundary, with acquisition succeeding 100% of the time at
every point. **Root cause identified below -- this is a real design gap in
HF4's guard interval, not a channel-construction artifact or a marginal
SNR effect.**

The occupied-bandwidth gate **passes**: 300-trial statistical campaign
puts the 95.1%-confidence upper bound on the top edge at 2,615.81 Hz (vs.
the 2,700 Hz ceiling) and the lower bound on the bottom edge at 356.63 Hz
(vs. the 300 Hz floor), for both representative and maximum payloads.

Per `MODE_QUALIFICATION.md`'s explicit rule, this is recorded as a gate
failure at the exact envelope HF4 claims -- the claim is not narrowed after
the fact to match what was actually measured (see HF2's writeup in
`MODE_QUALIFICATION.md` for the project's precedent on recording failures
honestly). HF4 remains unregistered; nothing here changes that.

## Root cause: cyclic-prefix guard interval shorter than the required
## benign/static channel's filter settling time

`DESIGN.md`'s guard-interval rationale sizes HF4's 12-sample (1.0 ms at
12 kHz) cyclic prefix against `SPEED_LADDERS.md`'s **propagation**
delay-spread definition for benign/static (<=0.1 ms differential delay
spread) -- "about ten times" that figure, called "ample margin." But
`SPEED_LADDERS.md` separately requires that a benign/static qualification
channel "retain its complete filter, frequency-offset, drift, level, and
nonlinearity description" (the same requirement HF3's benchmark channel
implements, reused verbatim here for `benchmark_hf4.py`'s
`benign_static_channel`). The 250-3,100 Hz Butterworth bandpass filter
stage this requires has a settling/impulse-response tail measured at
~6.9 ms (99.9% energy) at the 48 kHz TX rate this campaign measured it at
-- roughly 1.7 ms once referred to the 12 kHz RX rate HF4's guard interval
is sized against, itself already ~1.7x the entire 1.0 ms guard. This is
channel-filter group delay/memory, an entirely different quantity from
propagation multipath delay spread, and it is not covered by the
propagation-delay-spread margin `DESIGN.md` reasoned about.

Diagnostic isolation (ad hoc, not saved as an artifact; reproducible from
the description below): applying only the required `FilterChannel`
(250-3,100 Hz bandpass, no noise, no fading, no frequency offset) to
HF4's own encoder output and decoding it already produces `crc_ok: False`
with a garbage decoded length -- the same failure signature seen at every
point of this campaign, including 20 dB SNR. Acquisition and the coarse
frame layout still succeed (the filter does not move the sync
self-correlation peak enough to fail threshold), but the filter's
impulse-response tail smears energy across symbol boundaries by more than
the 12-sample cyclic prefix can absorb, corrupting the OFDM per-carrier
orthogonality that `whale.dsp.equalize.fit_header`'s flat per-carrier
gain/offset fit assumes and cannot correct (a single per-carrier complex
gain models a channel with memory shorter than the guard interval, not
longer). This reproduces regardless of waveform SNR, matching this
campaign's flat 0/N result across the whole 8-20 dB sweep.

This is a genuine, board-level design gap in HF4 as built, not a tuning
nit: `ACQUISITION_THRESHOLD` or the SNR range does not matter here, because
the failure occurs before noise is added. Fixing it (out of scope for this
qualification pass, which measures the shipped design and does not modify
`hf4.py`) would require either a longer cyclic prefix sized against a real
SSB filter's impulse response length rather than only propagation delay
spread, or a receive-side channel-shortening/equalization step this design
does not currently have.

## Campaign

- Commands:
  - `python -m experiments.hf4.benchmark_hf4 --model benign_static --points 8 10 11 13 15 20 --trials 100 --seed 20260901 --out logs/mode_qualification/hf-ssb/hf4/2026-09-01/hf4_benign_static_scout_100.json`
  - `python -m experiments.hf4.benchmark_hf4 --model benign_static --points 13 --trials 300 --seed 20260901 --out logs/mode_qualification/hf-ssb/hf4/2026-09-01/hf4_benign_static_confirmed_13db.json`
  - `python -m experiments.hf4.measure_bandwidth --trials 300 --seed 20260901 --out logs/mode_qualification/hf-ssb/hf4/2026-09-01/hf4_occupied_bandwidth_300.json`
- Harness: `experiments/hf4/benchmark_hf4.py` and
  `experiments/hf4/measure_bandwidth.py`, both written for this campaign,
  mirroring `experiments/hf3/benchmark_hf3.py` /
  `experiments/hf3/measure_bandwidth.py` byte-for-byte in method (the
  shared `scripts/benchmark_simulated_channels.py` only offers
  `--model {awgn,watterson,fm}`, none of which alone satisfy
  `SPEED_LADDERS.md`'s benign/static definition, which explicitly rejects
  identity/AWGN-only evidence; HF3's qualification record used the same
  custom-harness approach for the same reason). HF4 is not registered in
  `whale.mode_qualification.MANIFEST` and was not registered for this
  campaign; the harness imports `experiments.hf4.hf4` directly and wraps it
  in a bench-only `WaveformMode`-shaped adapter (`mode_id=244`,
  unregistered placeholder, following HF3's 243/HF2's 242/HR0's 240-241
  convention), so no MANIFEST change was needed or made.
- Channel (benign/static): `experiments/hf4/benchmark_hf4.py`'s
  `benign_static_channel` -- identical stage recipe and tolerances to
  `experiments/hf3/benchmark_hf3.py`'s: bandpass filter (250-3,100 Hz) ->
  frequency offset (0.4 Hz) + drift (0.002 Hz/s) -> a two-path Watterson
  model at SPEED_LADDERS.md's benign/static tolerances (0.05 ms
  differential delay, 0.002 Hz Doppler, second path at -17 dB relative
  power) -> voltage gain (-2 dB) -> light clipping (0.97 limit) ->
  waveform-referenced AWGN -> bandpass filter again.
- Payload: `hf4.MAX_PAYLOAD_BYTES` (3,882 bytes) full-capacity random
  payload per trial, deterministically seeded via
  `whale.qualification.trial_seed`.
- Bandwidth campaign: complete keyed `hf4.modulate` waveform (lead-in +
  OFDM frame + tail), no channel applied (this gate measures the
  transmitted waveform, not a post-channel receive bandwidth); 300
  independent random payloads per class; sample maximum/minimum are
  distribution-free one-sided 95.1% confidence bounds on the population
  99th/1st percentile (`1 - 0.99**300 = 0.95096`).
- Master seed: `20260901` for all three artifacts.
- Git commit at run time: `14c20147b55ebce804281fc1f4468c71b938c840`, dirty
  tree (this qualification work, plus pre-existing unrelated untracked
  files, were uncommitted at run time).
- Python 3.13.2, NumPy 2.5.1, SciPy 1.18.0, Windows-11-10.0.26200-SP0.
- Artifacts (this directory): `hf4_benign_static_scout_100.json` (8/10/11/
  13/15/20 dB, 100 trials/point), `hf4_benign_static_confirmed_13db.json`
  (13 dB, 300 trials, confirmed tier), `hf4_occupied_bandwidth_300.json`
  (300 trials/payload class, representative half-capacity and maximum).

## Results

### Frame Monte Carlo -- benign/static (scouting sweep, 100 trials/point)

| Waveform SNR (dB) | Trials | Acquired (Wilson 95% LB) | Decoded (Wilson 95% FER UB) | Errors | In envelope? |
| ---: | ---: | ---: | ---: | ---: | --- |
| 8 | 100 | 100/100 (0.963) | 0/100 (1.000) | 0 | below envelope |
| 10 | 100 | 100/100 (0.963) | 0/100 (1.000) | 0 | below envelope |
| 11 | 100 | 100/100 (0.963) | 0/100 (1.000) | 0 | below envelope (extra margin point) |
| **13** | **100** | **100/100 (0.963)** | **0/100 (1.000)** | **0** | **Level 4 boundary -- fails** |
| 15 | 100 | 100/100 (0.963) | 0/100 (1.000) | 0 | in envelope (extra margin point) -- fails |
| 20 | 100 | 100/100 (0.963) | 0/100 (1.000) | 0 | far above envelope -- fails |

No transition is visible across the entire tested range: HF4 decodes zero
frames from 8 dB through 20 dB alike, consistent with the noiseless
filter-only diagnostic above -- the failure mode does not respond to SNR at
all in this range.

### Frame Monte Carlo -- benign/static (confirmed tier, +13 dB boundary, 300 trials)

| Waveform SNR (dB) | Trials | Acquired (Wilson 95% LB) | Decoded (Wilson 95% FER UB) | Errors |
| ---: | ---: | ---: | ---: | ---: |
| **13** | **300** | **300/300 (0.987)** | **0/300 (1.000)** | **0** |

**This point fails the frame Monte Carlo gate.** 95% Wilson-UB FER = 1.000
against the <=0.10 gate; acquisition clears its own 95% Wilson-LB >=0.90
gate (0.987) but that is immaterial when the FER gate fails outright.
Zero `error` outcomes (no exceptions; every trial cleanly reports
`payload=None` via CRC rejection), so this is a real, well-behaved decode
failure, not a harness fault.

### Occupied bandwidth (300 trials/payload class, statistical campaign)

| Payload | Trials | Width min/median/max | Low-edge min/median/max | High-edge min/median/max |
| --- | ---: | --- | --- | --- |
| 1,941 bytes (representative half-capacity) | 300 | 2,245.76 / 2,250.00 / 2,254.48 Hz | 356.63 / 358.98 / 361.34 Hz | 2,605.21 / 2,609.45 / 2,612.52 Hz |
| 3,882 bytes (maximum) | 300 | 2,241.76 / 2,247.64 / 2,257.77 Hz | 356.86 / 361.10 / 365.34 Hz | 2,604.51 / 2,608.75 / 2,615.81 Hz |

Worst 95.1%-confidence upper bound on the top edge: **2,615.81 Hz**, 84.19 Hz
below the 2,700 Hz ceiling. Worst 95.1%-confidence lower bound on the
bottom edge: **356.63 Hz**, 56.63 Hz above the 300 Hz floor. **Both bounds
clear their gates** -- this is a real statistical upper-confidence-bound
result (not the single-trial sanity check in the pre-existing
`RESULTS.md`), and it is somewhat tighter than that single-trial check
suggested (which measured 90-92 Hz of top-edge margin; the worst-case 95.1%
UCB here still leaves 84.19 Hz, essentially consistent, not materially
worse).

## Gates this evidence does/does not clear

| MODE_QUALIFICATION.md gate | Status |
| --- | --- |
| Section 1 (unit/malformed-input tests) | Cleared previously -- `experiments/hf4/test_hf4.py`, 21/21 passing (see `RESULTS.md`); not re-run in this campaign. |
| Section 2 (bounded CI regression) | Not run -- HF4 has no `tests/test_channel_regressions.py` entry and none was added. |
| Section 3 (frame Monte Carlo, confirmed tier) | **Fails at the required point**: benign/static +13 dB, 0/300 decoded, FER Wilson-UB 1.000, far outside the <=0.10 gate. Acquisition alone (0.987 Wilson-LB) would have cleared its own sub-gate, but the FER gate is the binding one and it fails completely. |
| Section 4 (occupied bandwidth) | **Cleared** for both representative and maximum payloads against the project owner's 300-2,700 Hz ceiling: worst top-edge UCB 2,615.81 Hz, worst bottom-edge LCB 356.63 Hz. |
| Section 4 (fixed-mode useful transfer) | Not run -- moot while the frame Monte Carlo gate fails; there is no useful throughput to measure when 0% of frames decode. |
| Section 5 (hardware) | Not run -- out of scope for this pass; no radios used. |
| Section 6/7 (ladder/system) | Not measured; not applicable to an unregistered single-waveform experiment. |

## Bottom line

HF4's net-throughput figure in `RESULTS.md` (7,296.5 bit/s) remains an
accurate measurement of the noiseless encoder/decoder round trip and is
not contradicted by anything here. What this campaign establishes is that
HF4 **cannot currently deliver that throughput, or any throughput at all,
over the exact benign/static channel `SPEED_LADDERS.md` requires it to
qualify against** -- the design's guard interval is undersized for a real
SSB filter chain's memory, independent of SNR. HF4's Level 4 envelope
claim (benign/static at +13 dB and above) is **not supported** by this
evidence; the occupied-bandwidth claim (300-2,700 Hz) **is** supported.
