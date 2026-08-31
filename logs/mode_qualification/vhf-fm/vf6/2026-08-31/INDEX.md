# VF6 synthetic excellent-channel smoke evidence

- Artifact: `flat_nbfm_frame_smoke.json`
- Command: `python scripts/benchmark_simulated_channels.py --model fm
  --policy vhf-fm --mode-level experimental --fm-profile flat_nbfm --points
  30 35 40 --trials 20 --workers 4 --modes vf6 --seed 20260831 --out
  logs/mode_qualification/vhf-fm/vf6/2026-08-31/flat_nbfm_frame_smoke.json`
- Payload: 10,218 DATA bytes plus the 10-byte air header (maximum capacity)
- Airtime/useful rate: 5.200 s; 15,720 bit/s before ARQ turnaround
- Results: 0/20 at 30 dB, 20/20 at 35 dB, 20/20 at 40 dB RF C/N

The channel is the project `flat_nbfm` complex-IQ FM recipe. This is a
deterministic smoke campaign and operating-boundary anchor, not the 100/300
trial promotion campaign and not evidence through a measured radio preset.

## Fine C/N evidence

- `flat_nbfm_cn_discovery_30.json`: 30 trials at 0.5 dB spacing from 30 to
  35 dB, master seed 20260831.
- `flat_nbfm_cn_confirmatory_300.json`: 300 trials at 31.25, 31.5, 31.75,
  33, 33.5, 34, 34.5, 35, 35.5, and 36 dB, master seed 20260901.
- `flat_nbfm_cn_35_75_300.json`: 300 trials at 35.75 dB, master seed
  20260901.
- `flat_nbfm_cn_high_400.json`: 400 trials at 36.5 and 37 dB, master seed
  20260902.

All used `python scripts/benchmark_simulated_channels.py --model fm --policy
vhf-fm --mode-level experimental --fm-profile flat_nbfm --workers 8 --modes
vf6` with the points, trials, seed, and output file named above. Payloads were
the maximum 10,218 DATA bytes plus the ten-byte air header. Intervals in the
artifact summaries are pointwise two-sided Wilson 95% intervals.

The confirmatory evidence bounds 50% delivery to 31.25--31.75 dB RF C/N and
90% to 33--34.5 dB. It bounds 95% only to 33.5--36 dB; the raw rate was 95%
at 35 dB. Both 36.5 and 37 dB produced 393/400, so 99% delivery is not
established. These are synthetic full-Nyquist complex-IQ RF C/N values, not
audio SNR or decoder-estimated carrier SNR.
