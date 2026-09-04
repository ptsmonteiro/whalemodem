# VF4 (vhf-fm mode ID 8) -- 2026-08-31 simulated frame Monte Carlo

## Target rung and disposition

VF4 declares VHF FM speed-ladder **Level 3** ("Fast data", SPEED_LADDERS.md):
minimum useful application throughput 6,000 bit/s, required envelope "measured
clean path at 25 dB RF C/N and above". This evidence supports **Experimental**
registration only (`mode_qualification.MANIFEST` entry
`("vhf-fm", 8, EXPERIMENTAL)`). It does **not** support Optional or Default
promotion.

## Honest gap: no measured "clean path" preset exists

CHANNELS.md's "Measured VHF bench presets" section currently defines only
`vhf_bench_conservative` (the Level 0-2 preset, used because it takes the
*more adverse* of two measured directional legs) and the two directional
measured legs `ic705_to_kg_uv9d` / `kg_uv9d_to_ic705` themselves, which are
also conservative-bench legs, not a distinct "clean" characterization.
Nothing in the current preset registry is documented, measured, or named as
a clean 25 dB-class VHF path per SPEED_LADDERS.md's "VHF FM ladder" prose
("Level 3 uses a measured clean data-capable path... Synthetic profiles may
locate boundaries but cannot by themselves satisfy a target rung").

No radios were available for this work, and none were fabricated. This
campaign therefore uses `FM_SYNTHETIC_PROFILES["flat_nbfm"]` -- an explicit
project-recipe synthetic NBFM profile already used for VF6's own experimental
regression point -- as the best available proxy for a clean path, run through
the same `ComplexFmChannel` complex-IQ RF model used for measured presets.
This is **diagnostic/boundary-locating evidence only**, exactly as
MODE_QUALIFICATION.md section 3 and SPEED_LADDERS.md's shared interpretation
require it to be treated. VF4's Experimental disposition is simulation-only
pending a real measured clean-path fixture (a bench/off-air characterization
producing a named `FM_RADIO_PRESETS` entry) and hardware evidence; it must
not be read as satisfying the Level 3 rung, and it is not represented as
Optional- or Default-grade evidence anywhere in this record.

## Design

`whale/modes/vf4.py`: 58-carrier OFDM at 46.875 Hz spacing (468.75-3140.625
Hz), 48 kHz sample rate, 1152-sample symbols (128-sample cyclic prefix + 1024
core), 214 symbols/frame (15 header + 199 payload, 10 of which are full-band
pilots), 5.2 s/frame -- the same qualified frame geometry, acquisition,
timing, and pilot-tracking machinery as VF6 (`whale/modes/vf6.py`). The only
substantive design change from VF6 is spending 4 bits/carrier/symbol
(Gray-labelled square 16-QAM) instead of VF6's 8 (square 256-QAM), which
trades throughput for the ~15-20 dB less per-carrier SNR margin needed to
close cleanly well below 25 dB RF C/N instead of VF6's 35-40 dB. Reed-Solomon
RS(254,238) over GF(256) protects the payload in 21 byte-interleaved
codewords (same code as VF6, sized to the smaller payload grid).

| Parameter | Value |
| --- | --- |
| Modulation | Square 16-QAM, Gray-labelled, coherent (pilot-tracked, non-differential) |
| Carriers | 58, 46.875 Hz spacing, 468.75-3140.625 Hz |
| Frame | 214 OFDM symbols (15 header incl. 5 sync + 199 payload incl. 10 pilots), 5.2 s |
| FEC | RS(254,238) over GF(256), 21 interleaved codewords, 8-byte correction/codeword |
| Payload grid | 43,848 bits = 5,481 bytes; 147 unused/padding bytes |
| Max application payload | 4,992 bytes (RS packet minus 2-byte length + 4-byte CRC32) |
| Link chunk size (minus 10-byte air header) | 4,982 bytes |
| Nominal frame-level throughput | 4,982*8/5.2 = 7,664.6 bit/s |

## Campaign

- Command: `python scripts/benchmark_simulated_channels.py --model fm
  --policy vhf-fm --mode-level experimental --fm-profile flat_nbfm
  --modes vf4 --points <...> --trials <...> --seed 20260831`
- Channel: `ComplexFmChannel.from_profile(48_000, "flat_nbfm",
  carrier_to_noise_db=<point>, seed=<derived>)` -- synthetic, not measured
  (see gap above).
- Master seed: `20260831`. Payload: default full-capacity DATA chunk
  (4,982 bytes requested, 4,992-byte encoded VF4 payload).
- Git commit at run time: `93b9a03607852372796b9527c6ad3e75ad588c6f`, dirty
  tree (this qualification work itself is uncommitted; per
  MODE_QUALIFICATION.md, default promotion requires a clean tree -- another
  reason this is Experimental-only evidence).
- Python 3.13.2, NumPy 2.5.1, SciPy 1.18.0, Windows-11-10.0.26200-SP0.
- Artifacts: `vf4_flat_nbfm_monte_carlo.json` (15-30 dB, 100 trials/point),
  `vf4_flat_nbfm_boundary.json` (16-19 dB, 100 trials/point, boundary
  location), `vf4_flat_nbfm_25db_300trial.json` (25 dB, 300 trials, the
  Level 3 boundary point at promotion-sized count), `vf3_flat_nbfm_comparison.json`
  (VF3 on the same channel/points, for the adjacent-rung overlap check).

## Results

| RF C/N (dB) | Trials | Delivered | Wilson 95% LB-UB | In envelope? |
| ---: | ---: | ---: | --- | --- |
| 15 | 100 | 11/100 | 6.3-18.6% | below envelope (boundary location) |
| 16 | 100 | 96/100 | 90.2-98.4% | below envelope (boundary location) |
| 17 | 100 | 100/100 | 96.3-100.0% | below envelope (boundary location) |
| 18 | 100 | 100/100 | 96.3-100.0% | below envelope (boundary location) |
| 19 | 100 | 100/100 | 96.3-100.0% | below envelope (boundary location) |
| 20 | 100 | 100/100 | 96.3-100.0% | below envelope (extra margin point) |
| 23 | 100 | 100/100 | 96.3-100.0% | below envelope (extra margin point) |
| **25** | **300** | **300/300** | **98.7-100.0%** | **in envelope -- Level 3 edge** |
| 27 | 100 | 100/100 | 96.3-100.0% | in envelope |
| 30 | 100 | 100/100 | 96.3-100.0% | in envelope |

Every trial at every point acquired (100% acquisition in all 1,300 trials
across both artifacts); all non-decoded outcomes were `payload_failed`
(RS/CRC failure after successful acquisition), and there were **zero
`error` outcomes** anywhere in the campaign. The transition is a sharp
cliff between 15 and 17 dB rather than a gradual slope, consistent with the
coherent 16-QAM/pilot-tracking design; 25 dB and above clears the gate with
roughly 8-13 dB of margin above the observed cliff on this synthetic
channel.

**Gate check (MODE_QUALIFICATION.md section 3), at and above 25 dB RF C/N
on this synthetic `flat_nbfm` proxy:**
- 95% Wilson upper bound on FER: <=1.3% at 25 dB (300 trials), 0.0% (exact)
  at 27 and 30 dB -- clears the <=10% requirement with large margin.
- 95% Wilson lower bound on acquisition: 100% observed at every point
  25 dB and above -- clears the >=90% requirement.
- Zero `error` outcomes anywhere in the campaign.
- The grid brackets the envelope edge with more than two points comfortably
  inside (25/27/30 dB), the boundary itself at promotion-sized count
  (300 trials at 25 dB), and multiple points below/near the observed cliff
  (15-23 dB) to locate it.

## Throughput and adjacent-rung (VF3) comparison

VF4's nominal frame-level useful payload rate is 4,982*8/5.2 = **7,664.6
bit/s**, which clears the Level 3 floor of 6,000 bit/s with about 28%
margin. This is a nominal frame-payload figure (chunk bytes / frame airtime),
not the full `benchmark_sessions.py` bulk-transfer figure that
MODE_QUALIFICATION.md section 6 defines as the authoritative useful-throughput
number; that full-session run is still outstanding (see Remaining gaps).

At 20, 25, and 30 dB RF C/N on the same `flat_nbfm` synthetic channel, VF3
(`vf3_flat_nbfm_comparison.json`) also delivers 100/100 frames, so these are
valid shared passing points for the adjacent-rung check. VF3's chunk size is
1,426 bytes over the same 5.2 s frame, i.e. 1,426*8/5.2 = 2,194.5 bit/s
nominal. VF4/VF3 = 7,664.6/2,194.5 = **3.49x**, which clears the required
>=25% improvement by a very large margin at every shared passing point in
this campaign.

## Remaining gaps (not closed by this artifact)

- **Measured clean-path preset** (the central gap): no bench/off-air
  measurement exists to characterize a genuine "clean" VHF leg; this
  campaign is entirely synthetic per the disclosure above.
- Full-stack session/ARQ/adaptation Monte Carlo (`benchmark_sessions.py`),
  needed for both the section 4 gate and the authoritative section 6
  useful-throughput number.
- Bidirectional hardware frame and session gates (section 5) -- no radios
  available.
- Development-host and minimum-target CPU/RSS (section 7);
  `scripts/benchmark_rx.py` currently only iterates the default-registry
  decoders and was not extended to cover experimental modes in this pass.
- Clean-tree artifact requirement for any promotion above Experimental.

None of these gaps block Experimental registration under
MODE_QUALIFICATION.md's promotion table ("Unit/malformed-input suite and a
bounded clean loopback pass; unique stable mode ID; declared target rung...;
provisional measured envelope; decoder resource use is bounded by
construction/test"), all of which this artifact plus
`tests/test_vf4_mode.py`, `tests/test_mode_conformance.py`, and
`tests/test_channel_regressions.py` satisfy. They do block Optional and
Default promotion, which is why the manifest entry is Experimental only.
