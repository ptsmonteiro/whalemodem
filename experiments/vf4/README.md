# VF4 — 58-carrier star-8-QAM OFDM frame

VF4 keeps VF3's 48 kHz sample rate, 24 ms symbols, 58-carrier layout and exact
5.200 s waveform. It replaces QPSK with aligned two-ring star 8-QAM, carrying
two differential phase bits and one amplitude-ring bit per carrier.

| Parameter | VF4 value |
|---|---:|
| Frame audio | 249,600 samples = 5.200 s |
| Symbol | 128-sample cyclic prefix + one 1,024-sample OFDM core |
| Carrier spacing | 46.875 Hz |
| Carriers | 58, FFT bins 10–67 |
| Carrier centers | 468.75–3140.625 Hz |
| Modulation | Differential two-ring star 8-QAM |
| Normalized radii | 0.541196 and 1.306563 |
| Header | 5 repeated sync + 10 varying coherent 8-QAM training symbols |
| Payload grid | 199 × 58 × 3 = 34,626 gross coded bits |
| Inner FEC | Interleaved rate-1/2, K=7 convolutional code |
| Outer FEC | 10 shortened RS(216,192) blocks, 12 byte corrections each |
| Raw convolutional packet | 2,163 bytes plus 3 spare and 6 trellis-tail bits |
| RS-protected packet | 1,920 bytes |
| User payload | 1,914 bytes plus 16-bit length and CRC32 |

The constellation's ring ratio is `1 + sqrt(2)`. This equalizes radial spacing
with adjacent-point spacing on the inner ring while keeping average carrier
energy equal to one. The aligned rings are invariant under 90-degree rotation,
so the payload retains VF3's differential protection against persistent
quadrant slips. Ring amplitude is detected independently from phase, and the
receiver blindly fits both received radii on each carrier.

The 34,626-bit grid has a gross modulation rate of about 6,659 bit/s over the
complete frame. After both FEC layers and framing, 1,914 user bytes yield about
2,945 net user bit/s. That is 478 bytes, or 33%, more user capacity than VF3 in
the same waveform duration and audio bandwidth.

Software validation:

```powershell
python experiments/vf4/test_vf4.py
```

On-air test:

```powershell
python experiments/vf4/run_air.py --direction both --trials 3
```

The runner leaves radio frequency and mode untouched, opens only the active
transmitter's PTT port, and stores captures and JSON results under `results/`.

## Validated result

On 2026-08-20 the final mode passed three independent full-capacity frames in
each direction, **6/6 total**. All frames decoded byte-for-byte and passed the
CRC. See `RESULTS.md` and `results/final_both_3.json`; the six raw captures and
matching payloads are retained under `results/captures/final_both_3/`.
