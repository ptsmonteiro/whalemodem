# HR0 HC0 baseline: exploratory frame boundaries

## Handoff

**Go to HR0-A's oracle and bounded receiver work.**  The HC0 baseline now has
a reusable matched-trial harness and an exploratory edge under every
registered Watterson preset.  The result is not promotion evidence: every
point has 30 trials, so even 30/30 has a 95% Wilson lower bound of only
0.886.  No HR0 waveform or production registry behavior was added.

The measurements support the existing design direction:

- HC0 delivered 30/30 full frames at -16 dB canonical AWGN and 12/30 at
  -17 dB.  Payload/FEC failure, not acquisition, caused all 18 losses at
  -17 dB.  HR0-A's -24 dB AWGN target is therefore about 8 dB below the
  last perfect exploratory HC0 point.
- In canonical `mid_latitude_disturbed`, HC0 delivered 30/30 at -11 dB and
  24/30 at -12 dB.  Acquisition remained 28/30 at -12 dB; four of those
  acquisitions failed checked payload decode.
- HC0 did not approach reliable delivery in either 7 ms stress preset even
  after noise became negligible.  `mid_latitude_disturbed_nvis` delivered
  20/30 at +30 dB, and `high_latitude_disturbed` delivered 14/30 at +30 dB.
  Acquisition was 30/30 at these points, so the ceiling is in checked-body
  demodulation/decoding under delay/Doppler, not the preamble threshold.
  There is no honest SNR boundary to report for those presets: this campaign
  found an impairment floor instead.
- There were zero `error` outcomes and zero wrong checked payloads across
  3,390 trials.  Every failed seed has a one-command replay; representative
  failures also have compressed captures.

The point-3 stop remains: redesign HR0-A's geometry if the guarded receiver
still loses at least 3 dB on disturbed NVIS or disturbed high latitude
relative to disturbed mid latitude.  HC0's results do not justify weakening
that criterion; they make the 7 ms/30 Hz test more important.

## Frozen workload and measured HC0 waveform

All trials used HC0's full 64-byte physical payload.  The comparison counts
the first 10 bytes as the checked air header and the remaining 54 bytes
(432 bits) as the DATA body.  Payload bytes are deterministic random data;
they exercise the physical frame, not a link session.

| Quantity | Measured value |
| --- | ---: |
| Physical payload | 64 B |
| Checked air header allocation | 10 B |
| DATA-body comparison | 54 B |
| Keyed samples / rate | 164,288 / 48,000 Hz |
| Keyed time | 3.4226667 s |
| DATA-body frame useful rate | 126.217 bit/s |
| Whole-keying mean-square power | 0.0167848 |
| RMS / peak / crest factor | 0.129556 / 0.183848 / 1.41906 |
| Whole-keying energy | 0.0574486 mean-square-seconds |
| Energy per DATA-body bit | 0.000132983 mean-square-seconds |
| 99% cumulative-power interval | 699.75--2208.22 Hz |
| 99% occupied bandwidth | 1508.47 Hz |
| Waveform-SNR to useful `Eb/N0` offset | +22.7909 dB |

The exact values, candidate source hash, and bandwidth definition are in
every artifact's `mode_metadata`.  The bandwidth uses the interval between
0.5% and 99.5% cumulative one-sided real-FFT power over the complete keying.
It is a reproducible experiment measure, not a regulatory occupied-bandwidth
measurement.

## SNR and channel contracts

Every curve uses waveform SNR.  Signal power is mean square over the exact
half-open interval `[0, 164288)`, including the common lead, body, and tail.
Noise is real AWGN across the 0--24 kHz Nyquist band.  The ordered channel is:

- `awgn`: transmit waveform -> AWGN;
- `watterson_canonical`: transmit waveform -> seeded Watterson -> AWGN whose
  power is normalized from that frame's post-Watterson reference interval;
- `watterson_fixed_n0`: transmit waveform -> seeded Watterson -> local AWGN
  whose variance is calibrated from the unfaded transmit waveform.

The fixed-N0 runner is an extensibility check and a useful independent-frame
diagnostic.  It resets Watterson for every frame and is explicitly described
as `independent_reset_per_frame_not_continuous`; it is **not** the continuous
300-second fixed-N0 campaign required before a deep-fade claim.  Its point
seeds are independent of the canonical curve, so differences between the
two 30-trial samples are not paired estimates of normalization gain.

### Documentation/code discrepancies resolved

- `PLAN.md` freezes the SNR reference at `[0, tx_samples)`.  The production
  `whale.qualification.channel_factory()` leaves `SnrSpec` bounds unset, so
  after Watterson its AWGN stage includes the delayed output extension in the
  default full-array reference (up to 336 extra samples for a 7 ms preset).
  This harness follows the newer HR0 protocol's explicit interval while
  retaining the production stage order and per-frame post-fade normalization.
  Artifacts record the bounds, so these results must not be silently merged
  with old default-bound artifacts.
- `whale.modes.hc0` reports 3.38 seconds for its old core waveform, while the
  public `Hc0Mode` adapter now uses the common 128 ms HF lead and reports
  3.4226667 seconds.  The harness uses the public mode boundary, matching
  `DESIGN.md` and the current link.
- The requested `/Users/pedro/miniconda3/bin/pytest` runs Python 3.10.20 and
  cannot import the repository's `enum.StrEnum` channel modules.  The usable
  installed environment is
  `/Users/pedro/miniconda3/envs/gnuradio/bin/python` (Python 3.11.15, NumPy
  2.4.6, SciPy 1.17.1, pytest 9.0.3).  Commands below name that environment
  exactly; production code was not changed to accommodate the stale runner.
- The production `TrialRun` schema provides the four shared outcome labels
  and bounded decoder metrics, but `TrialResult` has no per-trial derived-seed
  field and cannot describe fixed-N0 calibration.  The local schema therefore
  reuses `TrialOutcome`, `classify_decode()`, and `common_decoder_metrics()`
  while adding the missing replay/channel fields.  It is versioned separately
  instead of overloading production schema version 2.

## Exploratory results

These are delivery counts, not gates.  Different SNR points use independent
derived seeds, so small non-monotonic changes at 30 trials are sampling
variation rather than evidence of a non-monotonic decoder.

### AWGN

| Waveform SNR | Delivery | Acquisition | Payload failures | Acquisition failures | Delivery Wilson 95% |
| ---: | ---: | ---: | ---: | ---: | :--- |
| -10 dB | 30/30 | 30/30 | 0 | 0 | 0.886--1.000 |
| -12 dB | 30/30 | 30/30 | 0 | 0 | 0.886--1.000 |
| -14 dB | 30/30 | 30/30 | 0 | 0 | 0.886--1.000 |
| -15 dB | 30/30 | 30/30 | 0 | 0 | 0.886--1.000 |
| -16 dB | 30/30 | 30/30 | 0 | 0 | 0.886--1.000 |
| -17 dB | 12/30 | 30/30 | 18 | 0 | 0.246--0.577 |
| -18 dB | 1/30 | 30/30 | 29 | 0 | 0.006--0.167 |
| -20 dB | 0/30 | 20/30 | 20 | 10 | 0.000--0.114 |

This brackets the exploratory clean edge between -16 and -17 dB and agrees
with HC0's implementation note.  It does not qualify -16 dB.

### Priority Watterson presets

`mid_latitude_disturbed` was refined in one-dB steps around its transition.

| Preset / SNR | Delivery | Acquisition | Payload failures | Acquisition failures |
| :--- | ---: | ---: | ---: | ---: |
| disturbed mid, -10 dB | 29/30 | 30/30 | 1 | 0 |
| disturbed mid, -11 dB | 30/30 | 30/30 | 0 | 0 |
| disturbed mid, -12 dB | 24/30 | 28/30 | 4 | 2 |
| disturbed mid, -13 dB | 23/30 | 29/30 | 6 | 1 |
| disturbed mid, -14 dB | 9/30 | 28/30 | 19 | 2 |
| disturbed mid, -15 dB | 1/30 | 27/30 | 26 | 3 |
| disturbed mid, -16 dB | 0/30 | 27/30 | 27 | 3 |
| disturbed NVIS, -5 dB | 13/30 | 30/30 | 17 | 0 |
| disturbed NVIS, 0 dB | 15/30 | 30/30 | 15 | 0 |
| disturbed NVIS, +20 dB | 18/30 | 30/30 | 12 | 0 |
| disturbed NVIS, +30 dB | 20/30 | 30/30 | 10 | 0 |
| disturbed high, -5 dB | 6/30 | 30/30 | 24 | 0 |
| disturbed high, 0 dB | 12/30 | 30/30 | 18 | 0 |
| disturbed high, +20 dB | 17/30 | 30/30 | 13 | 0 |
| disturbed high, +30 dB | 14/30 | 30/30 | 16 | 0 |

### Coarse coverage of all F.1487 presets

The “anchor” is the lowest tested point with at least 29/30 delivery, used
only to guide the next campaign.  “Next coarse point” is the nearest lower
tested point.  A dash means no such anchor existed even after extending the
grid upward.

| Preset | Exploratory anchor | Next coarse point | Interpretation |
| :--- | :--- | :--- | :--- |
| `low_latitude_quiet` | -8: 30/30 | -12: 26/30 | edge bracketed coarsely |
| `low_latitude_moderate` | -8: 30/30 | -12: 28/30 | edge bracketed coarsely |
| `low_latitude_disturbed` | 0: 29/30 | -5: 23/30 | delay/Doppler penalty; coarse only |
| `mid_latitude_quiet` | -12: 30/30 | -16: 8/30 | edge bracketed coarsely |
| `mid_latitude_moderate` | -12: 29/30 | -16: 3/30 | edge bracketed coarsely |
| `mid_latitude_disturbed` | -11: 30/30 | -12: 24/30 | one-dB transition bracket |
| `mid_latitude_disturbed_nvis` | -- | +30: 20/30 | impairment floor, no SNR edge |
| `high_latitude_quiet` | -8: 30/30 | -12: 28/30 | edge bracketed coarsely |
| `high_latitude_moderate` | -8: 30/30 | -12: 23/30 | edge bracketed coarsely |
| `high_latitude_disturbed` | -- | +30: 14/30 | impairment floor, no SNR edge |

### Fixed-N0 independent-frame diagnostic

For `mid_latitude_disturbed`, the separately seeded local fixed-N0 curve was
30/30 at 0, -5, -8, and -10 dB; 24/30 at -12 dB; and 16/30 at -14 dB.
At -12 dB it acquired 28/30, with four payload failures and two acquisition
failures.  Do not read this small independently seeded curve as evidence that
fixed N0 is easier than canonical normalization.  Its purpose here is to
verify the model, metadata, and replay path before the required continuous
campaign exists.

## Harness and schema

[`benchmark.py`](benchmark.py) accepts `hc0` or a future
`package.module:OBJECT` mode selector.  Running multiple selectors with the
same model/preset/SNR/trial gives them the same derived workload, Watterson,
and AWGN seeds because mode identity is deliberately absent from seed
derivation.  Point identity is canonical and point-order independent.

Each schema-v1 artifact records:

- exact command, UTC bounds, commit and dirty paths, host/dependency metadata;
- master seed and every derived workload/Watterson/AWGN seed;
- full workload, airtime, useful-rate, energy, crest-factor, and SNR bounds;
- expanded ordered channel descriptions and per-stage measurements;
- per-trial acquisition, checked payload, error, timing, CPU, and capture
  outcomes;
- Wilson intervals for acquisition, conditional payload results, FER, and
  verified delivery; and
- an exact replay command for every trial plus compressed captures for a
  bounded representative set of failures.

The fixed-N0 stage is local and conspicuously named
`fixed_power_awgn_experiment_local`; it cannot be mistaken for the production
`AwgnChannel` or for continuous fading.

## Exact commands

All commands were run from the repository root with master seed `20260830`.

```sh
/Users/pedro/miniconda3/envs/gnuradio/bin/python experiments/hr0/benchmark.py sweep --model awgn --points -10 -12 -14 -15 -16 -17 -18 -20 --trials 30 --workers 8 --save-failures 3 --out experiments/hr0/results/hc0_awgn_exploration_20260830.json

/Users/pedro/miniconda3/envs/gnuradio/bin/python experiments/hr0/benchmark.py sweep --model watterson_canonical --watterson-preset mid_latitude_disturbed --points -5 -8 -10 -12 -14 -16 -18 -20 -22 -24 --trials 30 --workers 8 --save-failures 3 --out experiments/hr0/results/hc0_watterson_mid_latitude_disturbed_exploration_20260830.json
/Users/pedro/miniconda3/envs/gnuradio/bin/python experiments/hr0/benchmark.py sweep --model watterson_canonical --watterson-preset mid_latitude_disturbed --points -11 -13 -15 --trials 30 --workers 8 --save-failures 2 --out experiments/hr0/results/hc0_watterson_mid_latitude_disturbed_refinement_20260830.json

/Users/pedro/miniconda3/envs/gnuradio/bin/python experiments/hr0/benchmark.py sweep --model watterson_canonical --watterson-preset mid_latitude_disturbed_nvis --points -5 -8 -10 -12 -14 -16 -18 -20 -22 -24 --trials 30 --workers 8 --save-failures 3 --out experiments/hr0/results/hc0_watterson_mid_latitude_disturbed_nvis_exploration_20260830.json
/Users/pedro/miniconda3/envs/gnuradio/bin/python experiments/hr0/benchmark.py sweep --model watterson_canonical --watterson-preset mid_latitude_disturbed_nvis --points 0 5 10 20 30 --trials 30 --workers 8 --save-failures 2 --out experiments/hr0/results/hc0_watterson_mid_latitude_disturbed_nvis_extension_20260830.json

/Users/pedro/miniconda3/envs/gnuradio/bin/python experiments/hr0/benchmark.py sweep --model watterson_canonical --watterson-preset high_latitude_disturbed --points -5 -8 -10 -12 -14 -16 -18 -20 -22 -24 0 5 --trials 30 --workers 8 --save-failures 3 --out experiments/hr0/results/hc0_watterson_high_latitude_disturbed_exploration_20260830.json
/Users/pedro/miniconda3/envs/gnuradio/bin/python experiments/hr0/benchmark.py sweep --model watterson_canonical --watterson-preset high_latitude_disturbed --points 10 20 30 --trials 30 --workers 8 --save-failures 2 --out experiments/hr0/results/hc0_watterson_high_latitude_disturbed_extension_20260830.json

for preset in low_latitude_quiet low_latitude_moderate low_latitude_disturbed mid_latitude_quiet mid_latitude_moderate high_latitude_quiet high_latitude_moderate; do
  /Users/pedro/miniconda3/envs/gnuradio/bin/python experiments/hr0/benchmark.py sweep --model watterson_canonical --watterson-preset "$preset" --points -5 -8 -12 -16 -20 -24 0 5 --trials 30 --workers 8 --save-failures 1 --out "experiments/hr0/results/hc0_watterson_${preset}_exploration_20260830.json" || exit
done

/Users/pedro/miniconda3/envs/gnuradio/bin/python experiments/hr0/benchmark.py sweep --model watterson_fixed_n0 --watterson-preset mid_latitude_disturbed --points 0 -5 -8 -10 -12 -14 --trials 30 --workers 8 --save-failures 3 --out experiments/hr0/results/hc0_watterson_fixed_n0_mid_latitude_disturbed_exploration_20260830.json
```

Example failed-seed replay, verified after the campaign:

```sh
/Users/pedro/miniconda3/envs/gnuradio/bin/python experiments/hr0/benchmark.py replay --artifact experiments/hr0/results/hc0_awgn_exploration_20260830.json --record-index 150
```

The replay returned `matched: true` and reproduced the `payload_failed`
outcome with workload seed `3770263782383537135` and AWGN seed
`1729703539387505538`.

## Artifacts and checks

Machine-readable artifacts and captures are under [`results/`](results/).
There are 15 JSON run artifacts, each strict-JSON validated, plus bounded
failure captures in adjacent `_captures` directories.

```sh
/Users/pedro/miniconda3/envs/gnuradio/bin/python -m py_compile \
  experiments/hr0/benchmark.py experiments/hr0/test_benchmark.py
/Users/pedro/miniconda3/envs/gnuradio/bin/pytest -q \
  experiments/hr0/test_benchmark.py
```

The focused result was `7 passed`.  Relevant HC0/channel/qualification tests
and the final diff check are listed in the point handoff after their final
run.
