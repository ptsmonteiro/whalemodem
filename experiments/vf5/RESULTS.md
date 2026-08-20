# VF5 results

## 2026-08-20 — software validation

`python -m pytest experiments/vf5/test_vf5.py -q`: **12/12 passed**.

The suite covers standard Gray square-16-QAM mapping, full 2,381-byte round
trips, exact pilot removal of per-carrier linear phase drift, shortened
Reed–Solomon correction through 16 damaged bytes per block, recovery from 80
consecutive post-Viterbi error bytes through outer interleaving, cyclic-prefix
recovery, dispersive AWGN, a 75 ppm sound-card mismatch, and false-sync
rejection.

## Development probes

The first candidate attempted to carry differential quadrant and absolute
rotation-orbit shape bits without payload pilots. It passed **0/2**: raw BER
was 23.34% HT to IC-705 and 21.29% IC-705 to HT. Saved captures showed channel
phase moving by tens to more than one hundred degrees through the frame, which
made the square-shape labels ambiguous.

Adding ten full-band pilot symbols made IC-705 to HT pass, but the original
orbit labeling still gave 13.65% raw BER on HT to IC-705. Conventional Gray
I/Q labeling reduced the next HT trial to 10.60%, but contiguous post-Viterbi
errors overloaded two RS blocks with 21 and 22 damaged bytes.

The final candidate interleaves RS codeword bytes round-robin before the inner
convolutional code. Its first bidirectional probe passed **2/2**. HT to IC-705
had 9.39% raw BER, 19 corrected RS bytes total and at most three in one block;
the reverse frame had 2.17% raw BER and needed no RS correction. All diagnostic
JSON files and raw captures are retained under `results/`.

## Final bidirectional confirmation

Command:

```powershell
python experiments/vf5/run_air.py --direction both --trials 3 --seed 20260854 --capture-dir experiments/vf5/results/captures/final_both_3 --out experiments/vf5/results/final_both_3.json
```

Result: **6/6 full-capacity random frames decoded byte-for-byte, RS-valid and
CRC-valid**.

| Direction | Trial | Payload | Keyed | Sync | Raw errors | BER | RS corrected | Max/block | Median SNR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| HT → IC-705 | 1 | 2,381 B | 5.635 s | 0.9937 | 4,375 | 9.9777% | 26 | 4/16 | 12.38 dB |
| HT → IC-705 | 2 | 2,381 B | 5.643 s | 0.9935 | 4,273 | 9.7450% | 22 | 3/16 | 12.40 dB |
| HT → IC-705 | 3 | 2,381 B | 5.646 s | 0.9944 | 4,382 | 9.9936% | 34 | 5/16 | 12.37 dB |
| IC-705 → HT | 1 | 2,381 B | 5.634 s | 0.9988 | 1,166 | 2.6592% | 0 | 0/16 | 14.79 dB |
| IC-705 → HT | 2 | 2,381 B | 5.652 s | 0.9989 | 1,066 | 2.4311% | 0 | 0/16 | 16.69 dB |
| IC-705 → HT | 3 | 2,381 B | 5.642 s | 0.9989 | 1,108 | 2.5269% | 0 | 0/16 | 16.54 dB |

Independent offline replay of all six stored captures also passed **6/6**. The
harder direction used at most five of the sixteen available corrections in any
one RS block. Detailed metrics are in `results/final_both_3.json`; raw audio
and matching payloads are under `results/captures/final_both_3/`.
