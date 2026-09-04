# HF3 occupied-bandwidth campaign -- 2026-09-01

## Gate and method

This campaign clears MODE_QUALIFICATION.md section 4's HF 99%-power
occupied-bandwidth gate for HF3. It measures the complete keyed transmit
waveform (HF lead-in, OFDM frame, and tail), not merely the nominal carrier
span. The occupied interval is the equal-tail interval between 0.5% and
99.5% of cumulative unwindowed real-FFT bin power.

For each payload class, 300 independently generated random payloads were
measured. The sample maximum is a distribution-free, one-sided upper
confidence bound on the population 99th percentile: its coverage is
`1 - 0.99^300 = 0.95096`. This makes no normality or equal-variance
assumption. Channel realizations are not applied because this gate measures
the waveform transmitted into the radio audio chain, rather than receiver
bandwidth after a channel has filtered or spread it.

## Command and provenance

```text
python -m experiments.hf3.measure_bandwidth --trials 300 --seed 20260901 --out logs/mode_qualification/hf-ssb/hf3/2026-09-01-bandwidth/hf3_occupied_bandwidth_300.json
```

- Git commit: `9376a0c9583763091fcb2cf2a5bfae0adc1005b4`
- Tree: dirty (this qualification work and preceding user work were
  uncommitted)
- Python 3.13.2; NumPy 2.5.1; SciPy 1.18.0;
  Windows-11-10.0.26200-SP0
- Master seed: `20260901`
- Raw artifact: `hf3_occupied_bandwidth_300.json` (all 600 measurements,
  method, environment, and verdict)

## Results

| Payload | Trials | Minimum | Median | 95.1% upper bound on population P99 |
| ---: | ---: | ---: | ---: | ---: |
| 401 bytes (representative half-capacity) | 300 | 1,740.54 Hz | 1,755.04 Hz | 1,767.65 Hz |
| 803 bytes (maximum) | 300 | 1,740.23 Hz | 1,755.67 Hz | 1,774.59 Hz |

The worst upper confidence bound is **1,774.59 Hz**, 525.41 Hz below the
2,300 Hz ceiling. The gate **passes** for both representative and maximum
payloads.

This artifact establishes only occupied bandwidth. It does not measure
fixed-mode useful throughput, radio delivery, resource use, ladder behavior,
or complete modem behavior.
