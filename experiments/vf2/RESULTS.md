# VF2 results

## 2026-08-20 — software validation

`python experiments/vf2/test_vf2.py`: **10/10 passed**.

Coverage includes exact waveform geometry, full-capacity clean round trips,
22 dB AWGN plus a delayed three-tap audio channel, 75 ppm independent-clock
resampling, CRC rejection, and noise/pure-tone false-sync rejection.

## 2026-08-20 — air-test preflight

No transmission occurred. Both PTT interfaces were exclusively held by other
programs: COM5 for the HT and COM6 for the IC-705 each returned Windows error
5 (`Access is denied`) when opened. Two VARA FM instances were running at the
time. Close the programs using the radio interfaces, then rerun:

```powershell
python experiments/vf2/run_air.py --direction both --trials 3
```

That preflight produced no RF capture; the later successful run is recorded
below and beneath `results/`.

## 2026-08-20 — development probes

The first CRC-only probe synchronized but exposed accumulated phase error and
persistent bad-carrier errors. Saved-capture replay drove three receiver/frame
changes, all within `vf2`: varying coherent training symbols, interleaved
rate-1/2 convolutional coding, and a per-carrier decision-directed phase loop.

A later IC-705-to-HT probe initially selected periodic idle receiver audio at
sample 5,813 instead of the RF frame at sample 34,111. Acquisition now ranks
repeat-correlation proposals by their fit to the varying known header. After
that correction, both probe directions replay byte-for-byte with valid CRCs.

## 2026-08-20 — final bidirectional on-air confirmation

Command:

```powershell
python experiments/vf2/run_air.py --direction both --trials 3 \
  --seed 20260823 \
  --capture-dir experiments/vf2/results/captures/final_both_3 \
  --out experiments/vf2/results/final_both_3.json
```

Result: **6/6 full-capacity random frames decoded byte-for-byte and CRC-valid**.

| Direction | Trial | Payload | Keyed | Sync | Pre-FEC errors | BER | Median carrier SNR |
|---|---:|---:|---:|---:|---:|---:|---:|
| HT → IC-705 | 1 | 714 B | 5.621 s | 0.9979 | 96 | 0.8317% | 12.12 dB |
| HT → IC-705 | 2 | 714 B | 5.622 s | 0.9982 | 82 | 0.7104% | 12.41 dB |
| HT → IC-705 | 3 | 714 B | 5.620 s | 0.9979 | 99 | 0.8577% | 12.51 dB |
| IC-705 → HT | 1 | 714 B | 5.618 s | 0.9989 | 0 | 0.0000% | 17.79 dB |
| IC-705 → HT | 2 | 714 B | 5.619 s | 0.9989 | 122 | 1.0570% | 15.24 dB |
| IC-705 → HT | 3 | 714 B | 5.629 s | 0.9989 | 0 | 0.0000% | 15.12 dB |

An independent offline replay of all six saved `.npy` captures also passed
6/6 against their saved `.bin` payloads. The detailed machine-readable record
is `results/final_both_3.json`; the twelve capture/payload files total 7.8 MB.
