# VF4 results

## 2026-08-20 — software validation

`python -m pytest experiments/vf4/test_vf4.py -q`: **10/10 passed**.

The suite covers the star-8-QAM mapping and normalized ring geometry, shortened
Reed–Solomon correction through 12 damaged bytes per block, full 1,914-byte
round trips, cyclic-prefix recovery, dispersive AWGN, a 75 ppm sound-card
mismatch, and noise/tone false-sync rejection.

## Receiver and FEC probes

The first 2,157-byte candidate retained VF3's convolutional code without an
outer code. Its first bidirectional probe passed **1/2**: IC-705 to HT decoded,
but HT to IC-705 failed at 7.27% raw BER. Separating radial and angular metrics,
blindly fitting each carrier's ring levels, and accounting for phase noise from
two adjacent differential symbols made that saved frame decode successfully.

A fresh transmission with the improved detector still passed only **1/2**.
The failed HT to IC-705 frame had 6.54% raw BER, but only seven erroneous bytes
remained after Viterbi decoding, with five in the worst packet block. This led
to the final concatenated code: ten shortened RS(216,192) blocks, each capable
of correcting twelve bytes. The resulting user payload is 1,914 bytes.

The first RS-protected probe passed **2/2**. Its HT to IC-705 frame had 6.59%
raw BER and required eight RS corrections; the reverse frame needed none.
Probe JSON and captures are retained under `results/` and
`results/captures/`.

## Final bidirectional confirmation

Command:

```powershell
python experiments/vf4/run_air.py --direction both --trials 3 --seed 20260843 --capture-dir experiments/vf4/results/captures/final_both_3 --out experiments/vf4/results/final_both_3.json
```

Result: **6/6 full-capacity random frames decoded byte-for-byte, RS-valid and
CRC-valid**.

| Direction | Trial | Payload | Keyed | Sync | Raw errors | BER | RS corrections | Median carrier SNR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| HT → IC-705 | 1 | 1,914 B | 5.628 s | 0.9960 | 2,162 | 6.2439% | 5 | 12.69 dB |
| HT → IC-705 | 2 | 1,914 B | 5.640 s | 0.9960 | 2,154 | 6.2208% | 0 | 12.53 dB |
| HT → IC-705 | 3 | 1,914 B | 5.654 s | 0.9960 | 1,995 | 5.7616% | 0 | 12.86 dB |
| IC-705 → HT | 1 | 1,914 B | 5.630 s | 0.9972 | 264 | 0.7624% | 0 | 13.97 dB |
| IC-705 → HT | 2 | 1,914 B | 5.633 s | 0.9973 | 273 | 0.7884% | 0 | 16.27 dB |
| IC-705 → HT | 3 | 1,914 B | 5.643 s | 0.9973 | 246 | 0.7104% | 0 | 14.16 dB |

Independent offline replay of all six stored captures also passed **6/6**.
Detailed metrics are in `results/final_both_3.json`; raw audio and matching
payloads are under `results/captures/final_both_3/`.
