# HF2 occupied-bandwidth campaign -- 2026-09-01

## Gate and method

This campaign evaluates MODE_QUALIFICATION.md section 4's HF 99%-power
occupied-bandwidth gate for HF2 (hf-ssb policy, mode ID 7, Level 2
"general-purpose data"). It measures the complete keyed transmit waveform
(HF lead-in, OFDM frame, and tail), not merely the nominal carrier span. The
occupied interval is the equal-tail interval between 0.5% and 99.5% of
cumulative unwindowed real-FFT bin power.

For each payload class, 300 independently generated random payloads were
measured. The sample maximum is a distribution-free, one-sided upper
confidence bound on the population 99th percentile: its coverage is
`1 - 0.99^300 = 0.95096`. This makes no normality or equal-variance
assumption. Channel realizations are not applied because this gate measures
the waveform transmitted into the radio audio chain, rather than receiver
bandwidth after a channel has filtered or spread it.

## Command and provenance

```text
python -m experiments.hf2.measure_bandwidth --trials 300 --seed 20260901 --out logs/mode_qualification/hf-ssb/hf2/2026-09-01-bandwidth/hf2_occupied_bandwidth_300.json
```

- Started (UTC): `2026-09-01T08:07:52Z`
- Finished (UTC): `2026-09-01T08:08:18Z`
- Git commit: `9376a0c9583763091fcb2cf2a5bfae0adc1005b4`
- Tree: dirty (this qualification work and preceding user work were
  uncommitted; see `git status --porcelain` at run time)
- Python 3.13.2; NumPy 2.5.1; SciPy 1.18.0;
  Windows-11-10.0.26200-SP0
- Master seed: `20260901`
- Trials: 300 per payload class, 600 total measurements
- Raw artifact: `hf2_occupied_bandwidth_300.json` (all 600 measurements,
  method, environment, and verdict)

## Results

| Payload | Trials | Minimum | Median | 95.1% upper bound on population P99 |
| ---: | ---: | ---: | ---: | ---: |
| 58 bytes (representative half-capacity) | 300 | 3,043.71 Hz | 3,644.15 Hz | 4,212.11 Hz |
| 117 bytes (maximum) | 300 | 2,866.36 Hz | 3,462.74 Hz | 4,218.98 Hz |

The worst upper confidence bound is **4,218.98 Hz**, 1,918.98 Hz above the
2,300 Hz ceiling. The gate **fails** for both representative and maximum
payloads.

This exceeds the ceiling by a wide, non-marginal margin at both payload
classes (both classes' minimum observed width, 2,866-3,044 Hz, already
clears 2,300 Hz), so the shortfall is not sampling noise. HF2's carrier plan
(`CARRIER_HZ` 656.25-2,343.75 Hz across 19 carriers at 93.75 Hz spacing, see
`experiments/hf2/hf2.py`) places the nominal top carrier at 2,343.75 Hz
before OFDM sidelobes and the HF lead-in/tail are even added, which by
itself is already above the 2,300 Hz ceiling; the measured 99%-power width
compounds that with the OFDM sidelobe skirt and the lead-in/tail content.
Narrowing the carrier band (or lowering `CARRIER_HZ`'s span) would be
required before this gate can pass; that is a waveform-geometry change, out
of scope for this measurement artifact.

This artifact establishes only occupied bandwidth. It does not measure
fixed-mode useful throughput, radio delivery, resource use, ladder behavior,
or complete modem behavior. HF2's Monte Carlo FER/acquisition qualification
is recorded separately in `experiments/hf2/RESULTS.md`; this occupied-
bandwidth gate is a distinct, currently unmet requirement of
MODE_QUALIFICATION.md section 4.
