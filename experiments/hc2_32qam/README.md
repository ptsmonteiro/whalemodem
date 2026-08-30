# HC2 49-carrier coherent-32QAM rate proof

This isolated experiment is milestone 1 for a speed-first top HF ladder mode.
It proves exact full-payload audio round trips through a clean identity channel
with an oracle-aligned receiver. It is not registered, negotiable, or claimed
to be qualified.

Geometry and rate accounting:

| Item | Value |
| --- | ---: |
| Sample rate / FFT / cyclic prefix | 48,000 / 1,024 / 128 samples |
| Carrier spacing / transmitted symbol rate | 46.875 Hz / 41.667 symbol/s |
| Carriers / modulation | 49 / coherent rectangular 32QAM |
| Raw rate | 10,208.33 bit/s |
| FEC | punctured K=7 convolutional, rate 3/4 |
| Post-FEC nominal rate | 7,656.25 bit/s |
| Payload / training symbols | 120 / 2 |
| Maximum user payload | 2,749 bytes |
| Complete oracle frame | 2.928 s |
| Sustained full-frame user-payload rate | 7,510.93 bit/s |

The net figure includes the two training symbols, length, CRC32, trellis tail,
byte rounding, and padding. It excludes acquisition preamble, PTT/turnaround,
ARQ, and link air headers. The receiver assumes the exact frame start and an
identity channel; it has no CFO, sample-clock, phase, amplitude, fading, or
noise recovery. Moderate/disturbed Watterson performance is deliberately not
a gate for this top-mode rate milestone.

Run the deterministic proof with:

```sh
pytest -q experiments/hc2_32qam/test_hc2_32qam.py
```

Milestone 2 adds `demodulate`: bounded matched-filter carrier-offset and frame
acquisition, repeated-training CFO refinement, per-carrier complex
equalization, and decision-directed common-phase tracking. It adds no airtime,
so the full-frame net rate remains 7,510.93 bit/s. Deterministic tests cover
carrier offsets through +/-15 Hz, leading samples, static frequency-selective
complex gain, and smooth phase wobble. This is benign-channel evidence, not
Watterson qualification; sample-clock/multipath tracking and soft 32QAM
metrics remain future work.

Milestone 3 adds `benchmark_hc2_snr.py`, an AWGN FER/EVM sweep against the
milestone-2 receiver as it stands, and `test_hc2_snr.py` for the harness
itself. Over 7,800 full-capacity frame trials the mode clears the project's
existing FER gate (Wilson 95% upper bound at most 10%) from 12 dB
waveform-referenced SNR, reaches FER at most 1e-2 at 16 dB, and delivered
1,100/1,100 frames at 20 dB. Realized net payload passes the 7,050 bit/s
reference at 12.5 dB and reaches 7,504 bit/s at 16 dB. Post-equalization
decision-directed EVM separates decoding from failing frames well enough to
drive a fallback trigger at 10%. The sweep also found that every failure
above 12.5 dB is one acquisition defect -- locking onto the second of the two
identical training symbols -- not a noise limit; the receiver was
deliberately left unchanged, and `RESULTS.md` records the full curve, the EVM
overlap region, the estimated ~2.4 dB hard-decision demapping penalty, and
the recommended fixes. AWGN only; Watterson is milestone 4.

Run the harness tests with:

```sh
pytest -q experiments/hc2_32qam/
```
