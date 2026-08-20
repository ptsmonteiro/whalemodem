# VF2 fixed 29-carrier QPSK frame

VF2 is a standalone implementation of the requested 214-symbol OFDM frame.
Nothing outside this directory is changed, and the DSP imports no existing
modem implementation.

| Parameter | VF2 value |
|---|---:|
| Sample rate | 48,000 sample/s |
| Frame audio | 249,600 samples = 5.200 s |
| Lead-in | 2,160 samples = 45 ms |
| Symbols | 214 × 1,152 samples = 5.136 s |
| Settle tail | 912 samples = 19 ms |
| Symbol | 128-sample guard + 512-sample core + repeated 512-sample core |
| Carriers | FFT bins 5–33, 468.75–3093.75 Hz |
| Spacing | 93.75 Hz |
| Modulation | QPSK, unit modulus on all 29 carriers |
| Header | 5 repeated sync + 10 varying training symbols, coherent QPSK |
| Payload grid | 199 × 29 × 2 = 11,542 bits |
| FEC | Interleaved rate-1/2, K=7 convolutional code |
| User payload | up to 714 bytes plus 16-bit length and CRC32 |

The arithmetic in the supplied duration has a 19 ms gap: 45 ms plus 214 ×
24 ms is 5.181 s, not 5.195–5.205 s. VF2 leaves every requested symbol intact
and adds a 19 ms silent settle tail, giving exactly 5.200 s.

The first five header symbols repeat one fixed 29-carrier QPSK vector. The
receiver correlates the received signal with itself one complete symbol apart,
so an unknown fixed audio-channel response does not prevent acquisition. The
remaining ten header symbols use a varying known QPSK sequence; a two-term fit
then separates multiplicative channel response from stationary narrowband
interference. The two 512-sample cores are phase-aligned and averaged.

The payload still places QPSK on all 29 carriers in all 199 payload symbols,
for the specified 11,542 gross bits. Those bits carry an interleaved rate-1/2,
constraint-length-7 convolutional code, so the byte-oriented user capacity is
714 bytes after the length field, CRC32 and trellis termination.

Run software validation:

```powershell
python experiments/vf2/test_vf2.py
```

Run three full-capacity frames in each direction over the configured bench:

```powershell
python experiments/vf2/run_air.py --direction both --trials 3
```

The runner does not tune either radio. It uses their current frequency and
mode, keys each through the repository's established PTT interface, saves raw
captures and matching payloads beneath `experiments/vf2/results`, and exits
nonzero unless every payload matches byte-for-byte. Close VARA FM, rig-control
software, and other programs holding either radio's serial PTT port before the
run. Only the transmitting radio's PTT port is opened for a one-way test.

## Validated result

On 2026-08-20, three independent full-capacity frames passed byte-for-byte in
each direction (6/6 total) on the IC-705/HT bench. See `RESULTS.md` and
`results/final_both_3.json`; all six raw captures and payloads are retained in
`results/captures/final_both_3/`.
