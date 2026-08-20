# VF3 — 58-carrier QPSK OFDM frame

VF3 is the full-density counterpart to VF2. It keeps the same 48 kHz sample
rate, 24 ms symbol period, 214-symbol structure, coherent QPSK header,
interleaved rate-1/2 convolutional code and exact 5.200 s frame.

| Parameter | VF3 value |
|---|---:|
| Frame audio | 249,600 samples = 5.200 s |
| Symbol | 128-sample cyclic prefix + one 1,024-sample OFDM core |
| Carrier spacing | 46.875 Hz |
| Carriers | 58, FFT bins 10–67 |
| Carrier centers | 468.75–3140.625 Hz |
| Modulation | Differential QPSK payload, constant modulus on every carrier |
| Header | 5 repeated sync + 10 varying coherent training symbols |
| Payload grid | 199 × 58 × 2 = 23,084 gross bits |
| FEC input | 11,542 bits |
| User payload | 1,436 bytes plus 16-bit length and CRC32 |

Compared with VF2, VF3 doubles gross and user capacity without changing frame
duration. It does so by removing the repeated 512-sample core: there is no
two-core 3 dB combine and timing tolerance falls from VF2's 640-sample repeat
window to the ordinary 128-sample cyclic prefix.

Payload symbols encode QPSK phase increments independently on each carrier.
This prevents a slowly moving audio-channel phase from making a carrier lock
permanently to the wrong 90-degree quadrant. The receiver feeds max-log QPSK
reliabilities, weighted by per-carrier header SNR, to a soft Viterbi decoder.

Software validation:

```powershell
python experiments/vf3/test_vf3.py
```

On-air test:

```powershell
python experiments/vf3/run_air.py --direction both --trials 3
```

The runner leaves radio frequency and mode untouched, opens only the active
transmitter's PTT port, and stores captures/results beneath this directory.

## Validated result

On 2026-08-20 the final differential-QPSK mode passed three independent
full-capacity frames in each direction, 6/6 total. Each frame carried 1,436
user bytes in 5.200 s of audio (about 2,209 net user bit/s). See `RESULTS.md`
and `results/final_dqpsk_both_3.json`; all six captures are retained under
`results/captures/final_dqpsk_both_3/`.
