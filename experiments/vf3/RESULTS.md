# VF3 results

## 2026-08-20 — software validation

`python experiments/vf3/test_vf3.py`: **8/8 passed**.

The suite covers the 58-carrier geometry, full 1,436-byte round trips,
constant-modulus QPSK, cyclic-prefix recovery, dispersive AWGN, 75 ppm
sound-card mismatch, and noise/tone false-sync rejection.

## Initial coherent-payload trial

The first VF3 payload used coherent QPSK plus independent per-carrier phase
loops. One-frame probes passed each direction, but the longer confirmation was
only **4/6**: HT to IC-705 passed 3/3; IC-705 to HT passed 1/3.

The failed captures showed permanent 90-degree cycle slips. One lost six
carriers and the other fourteen, each beginning partway through the payload.
Soft Viterbi metrics recovered the six-carrier failure but not the fourteen-
carrier failure. These captures and `results/final_both_3.json` are retained as
the evidence for changing the payload to differential QPSK.

## Final differential-QPSK bidirectional confirmation

Command:

```powershell
python experiments/vf3/run_air.py --direction both --trials 3 \
  --seed 20260828 \
  --capture-dir experiments/vf3/results/captures/final_dqpsk_both_3 \
  --out experiments/vf3/results/final_dqpsk_both_3.json
```

Result: **6/6 full-capacity random frames decoded byte-for-byte and CRC-valid**.

| Direction | Trial | Payload | Keyed | Sync | Pre-FEC errors | BER | Median carrier SNR |
|---|---:|---:|---:|---:|---:|---:|---:|
| HT → IC-705 | 1 | 1,436 B | 5.630 s | 0.9982 | 479 | 2.0750% | 10.86 dB |
| HT → IC-705 | 2 | 1,436 B | 5.631 s | 0.9983 | 657 | 2.8461% | 9.18 dB |
| HT → IC-705 | 3 | 1,436 B | 5.646 s | 0.9982 | 506 | 2.1920% | 11.14 dB |
| IC-705 → HT | 1 | 1,436 B | 5.632 s | 0.9990 | 30 | 0.1300% | 11.69 dB |
| IC-705 → HT | 2 | 1,436 B | 5.657 s | 0.9990 | 31 | 0.1343% | 11.81 dB |
| IC-705 → HT | 3 | 1,436 B | 5.643 s | 0.9990 | 34 | 0.1473% | 12.01 dB |

Independent offline replay of all six saved captures also passed 6/6. Detailed
metrics are in `results/final_dqpsk_both_3.json`; raw audio and matching
payloads are under `results/captures/final_dqpsk_both_3/`.
