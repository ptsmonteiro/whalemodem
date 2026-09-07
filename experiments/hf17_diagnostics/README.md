# HF17 retained-capture receiver diagnosis

Goal: identify receiver improvements toward 7 kbit/s application throughput
over IC-7300 TX -> IC-705 RX, 300--2700 Hz. Nine frames captured in this
campaign; no full-frame successes. All generated TX peaks were 1.0.

Artifacts live in `logs/mode_qualification/hf-ssb/hf17_ofdm64/`:
`diagnostic16`, `diagnostic64`, and `diagnostic64-coded-dense`. Each includes
`result.json`, three `captures/trialNN.npy` files at 12 kHz, and a
`diagnostics.json`. Payloads are reconstructed exactly from retained seed,
trial index, and packet size using the hardware runner's generator.

## Results

| Waveform | Original mean raw BER | Three-carrier smoothing | Delivery |
| --- | ---: | ---: | --- |
| 16-QAM uncoded, 0.325 s | 1.41% | 1.00% | 0/3 |
| 64-QAM uncoded, 0.325 s | 7.85% | 6.46% | 0/3 |
| 64-QAM LDPC-3/4, 1.075 s, pilot interval 8 | 7.66% | 5.94% | 0/3 |

Smoothing uses only received training symbols and improves all nine frames.
Widths 5, 9, and 15 are generally worse; the channel has useful frequency
structure that broad averaging removes. Width 3 was selected on these
captures, so independent validation remains necessary.

Known-payload (oracle) common-phase correction barely improves 64-QAM.
Common gain correction helps more; static per-carrier oracle gain correction
reduces short-frame BER to 4.1--6.1%. These fits use the evaluation data and
are optimistic diagnostic bounds, not operational decodes. Symbol-level
amplitude ratios vary roughly 0.76--1.24 in the short frames; phase errors
are mostly below two degrees. This favors investigating channel/amplitude
tracking and training accuracy ahead of common-phase tracking alone.

Repeated-preamble noise estimation avoids the existing pilot self-fit's
zero residual, but did not rescue any coded frame, with or without smoothing.
It remains a rough estimate: temporal changes between repetitions contribute
to the difference, and it does not measure all subsequent data distortion.
The default receiver and legacy channel_snr_db metric are preserved.
Experimental keyword noise_estimator='repeat' changes LLR weighting and
per-bin diagnostics. gain_smoothing=3 enables the observed improvement.

The coded candidate carries 966 PHY payload bytes, including the ten-byte
air header, in 1.075 seconds: 956*8/1.075 = 7,114.4 application bit/s if
decoded. No measured delivered throughput at that rate is established.

Receive floats now exceed 1.0 (peaks 4.18--4.73), unlike earlier captures.
The transport applies only the receive decimator; the source of this scale
change was not established. The harness's CLIP flag counts samples beyond
0.999, which is not sufficient to locate or prove ADC clipping in this
floating-point path. These data should not be used to claim a controlled
RF-level comparison with previous sessions.

## Reproduction

Run from the repository root (radio commands transmit on the 7300 only):

```powershell
python experiments/hf10_ofdm49_v6/hardware_test.py --a ic7300 --b ic705 --bps 4 --packet-bytes 245 --pilot-interval 20 --drive-scale 1 --trials 3 --save-captures --output-dir <fresh-directory>
python experiments/hf10_ofdm49_v6/hardware_test.py --a ic7300 --b ic705 --bps 6 --packet-bytes 350 --pilot-interval 20 --drive-scale 1 --trials 3 --save-captures --output-dir <fresh-directory>
python experiments/hf10_ofdm49_v6/hardware_test.py --a ic7300 --b ic705 --bps 6 --packet-bytes 972 --fec-rate 3/4 --pilot-interval 8 --drive-scale 1 --trials 3 --save-captures --output-dir <fresh-directory>
python -m experiments.hf17_diagnostics.replay <directory>/result.json
python -m pytest -q tests/test_hf17_diagnostics.py tests/test_hf6_mode.py
```

Replay emits per-symbol, per-carrier and constellation-energy error metrics,
oracle phase/gain/ramp comparisons, training-only smoothing comparisons, and
coded-frame CRC delivery under both noise estimators. Oracle results never
set the decoder success flag. No radios are opened by replay.

Next focused experiment: training that better measures the data-time channel
gain, with amplitude tracking from distributed pilots. Compare against the
saved-capture baseline before investing in higher-rate FEC. Remaining error
may include analog distortion, inter-carrier interference, or channel change;
this experiment does not uniquely distinguish those causes.

## Distributed pilots

Implemented `comb_tracking='common'` and `'residual'` as opt-in receiver
methods on the existing comb-pilot waveform; `'off'` is a matched-waveform
ablation. The legacy method remains available for reproducing old results.
TX rotates comb pilots twice (the stored reference already includes the
carrier phase, and TX rotates the entire grid). The new receiver accounts
for both rotations; the legacy receiver only accounts for one. Thus earlier
legacy comb failures do not establish that distributed pilots are unsuitable.

The common tracker predicts received pilot values from the interpolated
block-training channel response, fits one complex correction by least squares
on the pilots in each data symbol, and applies it across all carriers. This
tracks amplitude and common phase without flattening the estimated channel.
The residual variant interpolates pilot gain ratios over frequency; it was
inferior in this campaign. Neither method uses transmitted payload bits.

Geometry: 49 active carriers, five pilots at 300, 900, 1500, 2100, 2700 Hz,
44 data carriers, 64-QAM, LDPC-3/4, 1944 packet bytes, 20 data symbols between
full-band training symbols, 2.125 s. PHY payload is 1938 bytes and application
capacity is 1928 bytes: 7,258.4 bit/s arithmetic, excluding ACK/turnaround.

Artifacts: `distributed-common` (seed 20260901) and `distributed-confirm`
(seed 777) under the same log directory, three retained captures each.
Generated TX peak is 1.0 throughout. RX float-level caveats above still apply.

On identical captures from the first batch:

| Receiver | Mean raw BER | CRC delivery |
| --- | ---: | --- |
| Tracking off, no smoothing | 8.15% | 0/3 |
| Legacy comb, no smoothing | 35.06% | 0/3 |
| Common tracking, no smoothing | 6.27% | 0/3 |
| Common tracking, smoothing width 3 | 4.67% | 0/3 |
| Residual interpolation, no smoothing | 7.79% | 0/3 |

The fresh-seed confirmation, common tracking + width 3 + repeated-preamble
noise estimate, achieved 3.90--5.52% raw BER (mean 4.70%) but still 0/3 CRC
delivery. Matched replay with tracking OFF gave 5.76% without smoothing and
4.14% with smoothing. Thus the tracker improved the first batch but degraded
the second relative to smoothing alone; a repeatable tracking benefit is not
established. Pilot-estimation noise or biased references may outweigh the
amplitude-tracking benefit. Remaining coded-bit errors still overwhelm some
LDPC blocks. No registry/default-waveform promotion is made here.

Synthetic tests alternate data-symbol amplitudes between 0.72 and 1.18 while
leaving block training unchanged: both new trackers recover the exact payload,
whereas tracking disabled fails. Seven targeted tests pass, including existing
HF6 round trips. Subsequent investigation should quantify pilot prediction
errors and use uncertainty-weighted tracking before assuming that every pilot
correction helps. Residual frequency-selective error and codeword diversity
also remain relevant; phase-only tracking was already weak.

```powershell
python experiments/hf10_ofdm49_v6/hardware_test.py --a ic7300 --b ic705 --bps 6 --packet-bytes 1944 --fec-rate 3/4 --pilot-interval 20 --pilot-comb-stride 12 --comb-tracking common --gain-smoothing 3 --noise-estimator repeat --drive-scale 1 --seed 777 --trials 3 --save-captures --output-dir <fresh-directory>
python -m experiments.hf17_diagnostics.distributed <directory>/result.json
```

The comparison script reconstructs each payload and compares tracking off,
legacy, common, and residual receivers with smoothing widths 1 and 3 on the
same samples. It records true per-codeword syndrome outcomes and full payload
equality, using the repeated-preamble noise estimator for every replay.

## Confidence-weighted pilot tracking

`comb_tracking='confidence'` estimates the variance of the per-symbol complex
gain fit from disagreement among the five pilots. With fitted correction c
and estimated variance v, the applied correction is
`1 + max(0, 1 - v / |c-1|^2) * (c-1)` (with numerical floors).
This shrinks uncertain corrections toward the existing channel estimate.
It assumes pilot residuals give useful evidence of estimation error; shared
systematic pilot bias is not detected by disagreement alone.

At smoothing width 3 on the two retained batches:

| Seed | Tracking off | Full common correction | Confidence weighted |
| --- | ---: | ---: | ---: |
| 20260901 | 7.02% | 4.67% | 5.29% |
| 777 | 4.14% | 4.70% | 3.89% |
| 424242 (fresh radio confirmation) | 4.39% | 4.71% | 4.03% |

All are mean coded-bit BER; no full frames decoded. The first two rows are
development-set evidence; the third uses a fresh seed captured after selecting
the algorithm. Confidence weighting improves over
tracking off in both batches, sacrificing some of the first batch's gain to
avoid the second batch's regression. Synthetic amplitude-tracking tests also
pass for this option. The default remains legacy for compatibility.

Confirmation artifacts are in `distributed-confidence`, under the same log
root. Three new 7300 -> 705 frames, unchanged geometry, digital peak 1.0,
seed 424242. Confidence-weighted raw BER ranges 3.41--5.25%, mean 4.03%;
post-LDPC payload BER averages 2.16%, and CRC delivery remains 0/3.
Matched replay supports a modest benefit over both alternatives in this
batch. Combined evidence is still a small smoke campaign, not qualification
or a 7 kbit/s delivered-rate result. Eight targeted software tests pass.

Replay with `python -m experiments.hf17_diagnostics.distributed
<directory>/result.json --confidence` writes `confidence-replay.json` and
compares off/common/confidence on identical captures with smoothing width 3
and the repeated-preamble noise estimator. Hardware runner accepts
`--comb-tracking confidence`; other settings match the preceding command.
