# HC0/HC1 HF-SSB qualification campaign, 2026-08-30

Commands:

```powershell
python scripts/benchmark_simulated_channels.py --model watterson --policy hf-ssb --watterson-preset mid_latitude_quiet --points -5 0 5 10 15 20 --trials 100 --modes hc0 hc1 --out logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-30/watterson_mid_latitude_quiet_frame_monte_carlo.json
python scripts/benchmark_simulated_channels.py --model watterson --policy hf-ssb --watterson-preset mid_latitude_moderate --points -5 0 5 10 15 20 --trials 100 --modes hc0 hc1 --out logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-30/watterson_mid_latitude_moderate_frame_monte_carlo.json
python scripts/benchmark_simulated_channels.py --model watterson --policy hf-ssb --watterson-preset mid_latitude_disturbed --points -5 0 5 10 15 20 --trials 100 --modes hc0 hc1 --out logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-30/watterson_mid_latitude_disturbed_frame_monte_carlo.json
```

Run against commit `c553b94` with a clean tree, 100 independent trials per
point, full-capacity payloads, waveform SNR points -5/0/5/10/15/20 dB, across
the three standard mid-latitude Watterson presets (`whale/channel.py`:
quiet = 0.5 ms / 0.1 Hz, moderate = 1 ms / 0.5 Hz, disturbed = 2 ms / 1.0 Hz
delay/Doppler spread -- CCIR Good/Moderate/Poor).

## HC0: passed

100/100 (95% CI 96.3-100.0%) at every SNR point, in every preset, including
disturbed. HC0's redundancy margin absorbs the worst-case simulated HF path
tested here with no measurable degradation. This clears the "Qualified frame
Monte Carlo" gate for HC0.

## HC1: passed in quiet and moderate conditions; failed in disturbed

- **Quiet**: near-total failure only at -5 dB (2/100); 99/100 at 0 dB and
  100/100 from 5 dB up. Passes the gate everywhere except the -5 dB point.
- **Moderate**: -5 dB 1/100, 0 dB 66/100, 5 dB 89/100, 10 dB 91/100,
  15 dB 96/100, 20 dB 93/100. Clears the Wilson-bound FER gate (upper bound
  <=10% FER) from 10 dB up; fails below that.
- **Disturbed**: -5 dB 0/100, 0 dB 19/100, 5 dB 34/100, 10 dB 50/100,
  15 dB 43/100, 20 dB 61/100. Never clears the gate at any tested point,
  including 20 dB.

### Disturbed-preset investigation

The disturbed-preset ceiling (never above 61/100, even at 20 dB) was
investigated as a possible repeat of the CPFSK mode-2 channel-boundary
artifact (`logs/mode_qualification/vhf-fm/cpfsk/2026-08-29/INDEX.md`). It is
not: every trial at every point from 10-20 dB acquired successfully (no
`acquisition_failed` outcomes); every failure was a payload/CRC mismatch
after successful acquisition, and the channel-drain contract used here is the
same one already fixed for the FM boundary case.

Comparing HC1's per-carrier SNR diagnostics between decoded and
`payload_failed` trials at the same 20 dB point shows failed frames carry
roughly twice as many sub-0 dB subcarriers on average (2.6 vs 1.2 out of 19).
This is frequency-selective fading, not a thermal-noise floor -- raising the
nominal waveform SNR does not un-fade a specific notched carrier, which is
why the delivery rate does not climb monotonically toward 100% as SNR
increases (10/15/20 dB read 50/100, 43/100, 61/100, all with overlapping
Wilson intervals -- consistent with sampling noise around a real ~45-55%
floor, not a second boundary bug).

The likely cause is structural: HC1's 93.75 Hz carrier spacing
(`whale/modes/hc1.py`) is comparable to the coherence bandwidth implied by
the disturbed preset's 2 ms delay spread, so several adjacent carriers fade
together rather than independently. HC1's multiplicative interleaver
(stride 693) spreads each carrier's bits evenly across the 1,292-bit
codeword, but a correlated multi-carrier fade still costs more coded bits at
once than the rate-1/2 K=7 Viterbi decoder reliably recovers. HC0 uses more
redundancy and passed the same disturbed-preset trials at 100/100, consistent
with this being a margin difference between the two modes rather than a
channel-model or harness defect.

This is retained as a genuine qualification result, not a bug to fix in the
channel simulator or the direct-frame runner: HC1 does not presently qualify
for CCIR-Poor/disturbed HF conditions, and HC0 is the intended fallback for
that case. It clears "Qualified frame Monte Carlo" for HC1 only under quiet
(above -5 dB) and moderate (10 dB and up) conditions; it fails the gate for
disturbed conditions at every tested point.
