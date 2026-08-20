# VF5 — 58-carrier square-16-QAM OFDM frame

VF5 keeps VF3/VF4's 48 kHz sample rate, 24 ms symbol period, 58-carrier
layout, audio band and exact 5.200 s waveform. It uses conventional normalized
square 16-QAM with Gray-coded in-phase and quadrature levels.

| Parameter | VF5 value |
|---|---:|
| Frame audio | 249,600 samples = 5.200 s |
| Symbol | 128-sample cyclic prefix + one 1,024-sample OFDM core |
| Carrier spacing | 46.875 Hz |
| Carriers | 58, FFT bins 10–67 |
| Carrier centers | 468.75–3140.625 Hz |
| Modulation | Pilot-assisted coherent square 16-QAM |
| I/Q levels | ±1/sqrt(10), ±3/sqrt(10), Gray labeled |
| Header | 5 repeated sync + 10 varying coherent 16-QAM training symbols |
| Payload region | 199 symbols: 10 full-band pilots + 189 data symbols |
| Pilot positions | 18, 38, 58, …, 198 (zero-based within payload) |
| Coded data grid | 189 × 58 × 4 = 43,848 bits |
| Inner FEC | Interleaved rate-1/2, K=7 convolutional code |
| Raw convolutional packet | 2,739 bytes plus 6 spare and 6 trellis-tail bits |
| Outer FEC | 11 shortened RS(249,217) blocks, 16 byte corrections each |
| RS packet | 2,387 bytes |
| User payload | 2,381 bytes plus 16-bit length and CRC32 |

The receiver estimates phase independently on every carrier from the final
header symbol and ten known outer-corner 16-QAM pilots. It unwraps and linearly
interpolates those phase measurements across the payload, then applies
coherent max-log 16-QAM metrics weighted by header SNR.

The RS codewords are byte-interleaved round-robin before convolutional coding.
This distributes the bursty error events left by Viterbi decoding: an observed
73-byte failure that had put 21–22 errors into individual blocks becomes only
4–9 errors per block after interleaving, without changing capacity.

The 43,848-bit data grid carries about 8,432 coded bit/s over the complete
frame. The 2,381-byte user payload yields about 3,663 net user bit/s. This is
467 bytes, or 24%, more user capacity than VF4 in the same duration and band.

Software validation:

```powershell
python experiments/vf5/test_vf5.py
```

On-air test:

```powershell
python experiments/vf5/run_air.py --direction both --trials 3
```

The runner leaves radio frequency and mode untouched, opens only the active
transmitter's PTT port, and stores captures and JSON results under `results/`.

## Validated result

On 2026-08-20 the final mode passed three independent full-capacity frames in
each direction, **6/6 total**. All frames decoded byte-for-byte and passed the
outer RS code and CRC. See `RESULTS.md` and `results/final_both_3.json`; all six
raw captures and payloads are under `results/captures/final_both_3/`.
