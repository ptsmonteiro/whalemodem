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

The two training symbols carry different known full-band QPSK sequences, so
the acquisition correlation has a single unambiguous peak.

The net figure includes the two training symbols, length, CRC32, trellis tail,
byte rounding, and padding. It excludes acquisition preamble, PTT/turnaround,
ARQ, and link air headers. The milestone-1 oracle receiver
(`demodulate_oracle`) assumes the exact frame start and an identity channel; it
has no CFO, sample-clock, phase, amplitude, fading, or noise recovery.
Moderate/disturbed Watterson performance is deliberately not a gate for this
top-mode rate milestone.

Run the deterministic proof with:

```sh
pytest -q experiments/hc2_32qam/test_hc2_32qam.py
```

Milestone 2 adds `demodulate`: bounded matched-filter carrier-offset and frame
acquisition, training-pair CFO refinement, per-carrier complex equalization,
and decision-directed common-phase tracking. It adds no airtime, so the
full-frame net rate remains 7,510.93 bit/s. Deterministic tests cover carrier
offsets through +/-15 Hz, leading samples, static frequency-selective complex
gain, and smooth phase wobble. This is benign-channel evidence, not Watterson
qualification; sample-clock/multipath tracking and soft 32QAM metrics remain
future work.

Milestone 3 adds `benchmark_hc2_snr.py`, an AWGN FER/EVM sweep, and
`test_hc2_snr.py` for the harness itself. Its 7,800-trial campaign found that
every failure above 12.5 dB was a single acquisition defect rather than a
noise limit: the two training symbols were identical, so the acquisition
matched filter had two near-tied peaks one OFDM symbol apart and the receiver
sometimes locked onto the second one. Per the milestone constraint the
receiver was left unchanged and the defect was written up instead.

The fix followed and is what the package now ships. Training symbol 2 carries
a **different** full-band QPSK sequence (LFSR seed `0x00C3A`, picked by a scan
of seeds 1..4095 for low PAPR and near-zero correlation against the
acquisition template), the earliest-lag tie-break is gone in favour of the
plain matched-filter maximum, and CFO refinement divides the known training
values out before taking the inter-symbol phase advance. This changes 1,152
samples of the waveform and nothing else: frame structure, airtime, capacity,
frame PAPR and the 7,510.93 bit/s net rate are unchanged, and the payload half
of the frame is sample-identical.

Re-running the sweep over 8,300 full-capacity trials, the mode clears the
project's existing FER gate (Wilson 95% upper bound at most 10%) from
**11.5 dB** waveform-referenced SNR and reaches FER at most 1e-2 at **13 dB**
(12.5 dB misses only on the Wilson upper bound, 1.0033% against a 1%
criterion). No frame failed anywhere at 13 dB or above, over 4,300 trials from
13 to 25 dB. Realized net payload passes the 7,050 bit/s reference at
11.5 dB. Not one trial in 8,300 mis-acquired: no start error exceeded one
sample at any SNR down to 0 dB. Post-equalization decision-directed EVM still
supports a fallback trigger at 10%. `RESULTS.md` records both campaigns side
by side -- current and superseded -- with the full curves, the EVM overlap
regions, the estimated ~2.4 dB hard-decision demapping penalty that remains
out of scope, and what is left to do. AWGN only; Watterson is milestone 4.

Run the harness tests with:

```sh
pytest -q experiments/hc2_32qam/
```
