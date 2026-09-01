# HF3 (hf-ssb mode ID 9) -- 2026-08-31/09-01 simulated frame Monte Carlo

## Target rung and disposition

HF3 declares HF SSB speed-ladder **Level 3** ("Fast data", SPEED_LADDERS.md):
minimum useful application throughput 2,000 bit/s, required envelope
benign/static at +8 dB waveform SNR and above, quiet Watterson fading
(`mid_latitude_quiet`) at +10 dB and above. This evidence supports
**Experimental** registration only (`mode_qualification.MANIFEST` entry
`("hf-ssb", 9, EXPERIMENTAL)`). Both required frame Monte Carlo points (benign/static +8 dB, quiet
Watterson +10 dB) clear their statistical gates at the confirmed tier
(see "Results" below). It does **not** support Optional or Default
promotion -- see `experiments/hf3/RESULTS.md`'s "What is not yet
established" section for the current gap list. A later campaign clears the
occupied-bandwidth gate; see
`logs/mode_qualification/hf-ssb/hf3/2026-09-01-bandwidth/INDEX.md`.

## Design

`experiments/hf3/hf3.py`: 36-carrier OFDM at 46.875 Hz spacing
(421.875-2,062.5 Hz), 48 kHz sample rate, 1,152-sample symbols (128-sample
cyclic prefix + 1,024 core), 126 symbols/frame (6 header incl. 3 sync +
120 payload, 9 of which are comb pilots), ~2.99 s/frame including the shared
HF lead-in and tail. Coherent 16-QAM (4 bits/carrier) on 27 data carriers,
rate-1/2 K=9 convolutional code (`whale.dsp.fec.K9`), CRC32 + length field
via `whale.dsp.framing.PacketCodec`. Per-symbol pilot tracking: each of the
9 pilots' own complex gain, smoothed across an 11-symbol window, then
interpolated in polar form (magnitude/phase separately) across the carrier
axis; soft bits are weighted by the same interpolated gain's power. See
`experiments/hf3/DESIGN.md` for the full iteration record -- several
earlier candidates (64-QAM, a 64-sample guard interval, smaller pilot
combs, shorter frames) were measured and dropped; the two most consequential
findings were that 64-QAM did not have enough per-carrier SNR margin at
this envelope, and that a too-small (64-sample) guard interval let
`whale.dsp.timing.estimate`'s cyclic-prefix search occasionally lock onto a
spurious symbol-clock drift under Watterson fading, corrupting whole-frame
carrier extraction -- doubling the guard to 128 samples fixed this and was
the single largest reliability improvement found.

| Parameter | Value |
| --- | --- |
| Modulation | 16-QAM, Gray-coded, coherent (pilot-tracked, non-differential), generic M-PAM builder |
| Carriers | 36, 46.875 Hz spacing, 421.875-2,062.5 Hz, 9 comb pilots / 27 data |
| Frame | 126 OFDM symbols (6 header incl. 3 sync + 120 payload), ~2.99 s incl. lead-in/tail |
| FEC | Rate-1/2 K=9 convolutional code, soft-decision Viterbi |
| Payload grid | 12,960 coded bits = 1,620 bytes payload capacity before length/CRC |
| Max application payload | 803 bytes (packet minus 2-byte length + 4-byte CRC32) |
| Link chunk size (minus 10-byte air header) | 793 bytes |
| Nominal (100%-success) frame-level throughput | 803*8/3.172 = 2,025.2 bit/s |
| Measured 99%-power occupied bandwidth | This campaign's deterministic checks measured ~1,758 Hz; the later 300-trial/class statistical campaign establishes a 1,774.59 Hz 95.1% UCB vs. the 2,300 Hz ceiling. |

## Campaign

- Command:
  `python -m experiments.hf3.benchmark_hf3 --model watterson
  --watterson-preset mid_latitude_quiet --points 6 8 10 14 --trials 300
  --seed 20260901 --out experiments/hf3/results/hf3_quiet_watterson_confirmed_final.json`
  and
  `python -m experiments.hf3.benchmark_hf3 --model benign_static
  --points 4 6 8 12 --trials 300 --seed 20260901 --out experiments/hf3/results/hf3_benign_static_confirmed_final.json`
- Channels:
  - Quiet Watterson: `ChannelChain((WattersonChannel.from_preset(48_000,
    "mid_latitude_quiet", seed), AwgnChannel(48_000, SnrSpec(point_db),
    seed ^ 0x5A5A)))`, matching `whale.qualification.channel_factory`'s
    `"watterson"` recipe.
  - Benign/static: `experiments/hf3/benchmark_hf3.py`'s
    `benign_static_channel` -- a full measured/reproducible SSB path, not
    identity/AWGN-only, per SPEED_LADDERS.md's requirement: bandpass filter
    (250-3,100 Hz) -> frequency offset (0.4 Hz) + drift (0.002 Hz/s) ->
    sample-clock error (3 ppm) -> a two-path Watterson model at
    SPEED_LADDERS.md's benign/static tolerances (0.05 ms differential
    delay, 0.002 Hz Doppler, second path at -17 dB relative power so the
    two-ray model does not itself introduce an unrealistic notch -- see
    DESIGN.md's dated note) -> voltage gain (-2 dB) -> light clipping
    (0.97 limit) -> waveform-referenced AWGN -> bandpass filter again.
- Master seed: `20260901` for both confirmed campaigns (a `20260831`-seeded
  quiet-Watterson run against an earlier, superseded 64-sample-guard design
  is retained as `experiments/hf3/results/scout_quiet_watterson_guard64_superseded_20260831.json`
  for the DESIGN.md record, but is not cited as qualification evidence).
- Payload: `hf3.MAX_PAYLOAD_BYTES` (803 bytes) full-capacity random payload
  per trial, deterministically seeded via `whale.qualification.trial_seed`.
- Git commit at run time: `93b9a03607852372796b9527c6ad3e75ad588c6f`, dirty
  tree (this qualification work itself is uncommitted; per
  MODE_QUALIFICATION.md, default promotion requires a clean tree -- another
  reason this is Experimental-only evidence).
- Python 3.13.2, NumPy 2.5.1, SciPy 1.18.0, Windows-11-10.0.26200-SP0.
- Artifacts (also under `experiments/hf3/results/`):
  `hf3_quiet_watterson_confirmed_final.json` (6/8/10/14 dB, 300
  trials/point), `hf3_benign_static_confirmed_final.json` (4/6/8/12 dB, 300
  trials/point).

## Results

### Quiet Watterson (`mid_latitude_quiet`)

| Waveform SNR (dB) | Trials | Acquired (Wilson 95% LB) | Decoded (Wilson 95% FER UB) | Errors | In envelope? |
| ---: | ---: | ---: | ---: | ---: | --- |
| 6 | 300 | 300/300 (0.987) | 277/300 (0.112) | 0 | below envelope (boundary location) |
| 8 | 300 | 300/300 (0.987) | 287/300 (0.073) | 0 | below envelope (extra margin point) |
| **10** | **300** | **300/300 (0.987)** | **291/300 (0.056)** | **0** | **in envelope -- Level 3 edge** |
| 14 | 300 | 300/300 (0.987) | 287/300 (0.073) | 0 | in envelope (extra margin point) |

At the required +10 dB boundary: 95% Wilson-UB FER = 0.056 (<= the 0.10
gate), 95% Wilson-LB acquisition = 0.987 (>= the 0.90 gate), zero `error`
outcomes across 300 trials. **This point clears the frame Monte Carlo
gate.**

### Benign/static

| Waveform SNR (dB) | Trials | Acquired (Wilson 95% LB) | Decoded (Wilson 95% FER UB) | Errors | In envelope? |
| ---: | ---: | ---: | ---: | ---: | --- |
| 4 | 300 | 300/300 (0.987) | 300/300 (0.013) | 0 | below envelope (extra margin point) |
| 6 | 300 | 300/300 (0.987) | 300/300 (0.013) | 0 | below envelope (extra margin point) |
| **8** | **300** | **300/300 (0.987)** | **300/300 (0.013)** | **0** | **in envelope -- Level 3 edge** |
| 12 | 300 | 300/300 (0.987) | 299/300 (0.019) | 0 | in envelope (extra margin point) |

At the required +8 dB boundary: 95% Wilson-UB FER = 0.013 (<= the 0.10
gate), 95% Wilson-LB acquisition = 0.987 (>= the 0.90 gate), zero `error`
outcomes across 300 trials. **This point clears the frame Monte Carlo
gate with wide margin** (100% decoded). Benign/static shows essentially
flat, saturated performance across the whole 4-12 dB range tested -- the
grid does not locate a transition inside this range, consistent with a
near-line-of-sight-style channel this design comfortably clears well
below its required boundary.

## Gates this evidence does/does not clear

| MODE_QUALIFICATION.md gate | Status |
| --- | --- |
| Section 1 (unit/malformed-input tests) | Cleared -- `experiments/hf3/test_hf3.py` (round trips, oversize rejection, hostile-input rejection, occupied-bandwidth check) and `tests/test_mode_conformance.py`'s parameterized hostile-input sweep, all passing. |
| Section 2 (bounded CI regression) | Cleared as a smoke anchor only -- `tests/test_channel_regressions.py::test_hf3_on_benign_static_at_8db` and `::test_hf3_on_quiet_watterson_at_10db`, 2 trials/point, `>=1/2` decoded required (not a reliability claim). |
| Section 3 (frame Monte Carlo, confirmed tier) | **Cleared at both required points**: quiet Watterson +10 dB (291/300, FER Wilson-UB 0.056) and benign/static +8 dB (300/300, FER Wilson-UB 0.013), both with 300/300 acquisition and zero `error` outcomes. |
| Section 4 (fixed-mode useful transfer and HF bandwidth) | Useful transfer was not run. The bandwidth sub-gate was subsequently cleared by `2026-09-01-bandwidth/INDEX.md`. |
| Section 5 (hardware) | Not run -- unmeasured. No radios available. |
| Section 6 (ladder qualification) | Not measured against HC0/HC1/HF2; not a waveform gate. |
| Section 7 (complete system) | Not measured; not a waveform gate. |

Per MODE_QUALIFICATION.md's promotion table, HF3 clears the mandatory gates
for **Experimental** registration (unit/malformed-input suite, bounded clean
loopback, unique stable mode ID 9, declared Level 3 target rung, provisional
measured envelope at one of two required points so far, decoder resource use
bounded by construction/test). It does not clear Optional or Default gates.
