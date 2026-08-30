# VF3 VHF FM qualification campaign, 2026-08-30

Command:

```powershell
python scripts/benchmark_simulated_channels.py --model fm --policy vhf-fm --fm-preset vhf_bench_conservative --points 5 10 15 20 25 30 --trials 100 --modes vf3 --out logs/mode_qualification/vhf-fm/vf3/2026-08-30/fm_frame_monte_carlo.json
```

Run against commit `c553b94` with a clean tree, using the same `fm` model and
`vhf_bench_conservative` preset as the requalified CPFSK campaign (see
`logs/mode_qualification/vhf-fm/cpfsk/2026-08-29/INDEX.md`), so the same
channel-drain contract applies here.

Result: failed as a frame Monte Carlo qualification gate. VF3 delivered
0/100 frames (95% CI 0.0-3.7%) at 5 dB RF C/N, then cleared the gate at every
higher point: 99/100 at 10 dB, and 100/100 at 15, 20, 25, and 30 dB.

Unlike the CPFSK mode-2 diagnosis, this is not a boundary artifact: every
5 dB trial failed at acquisition, not payload/CRC, and the delivered/acquired
counts are identical at every point in this dataset (no partial-acquisition,
payload-only failures). This is a hard acquisition cliff between 5 and
10 dB RF C/N for VF3 under this preset, not a gradual FER slope or a
terminal-bit artifact. It has not been root-caused further (e.g. whether the
cliff tracks a specific acquisition threshold or carrier-SNR floor in
`whale/modes/vf3.py`); that would be the next diagnostic step if VF3's
promotion requires operating margin closer to 5 dB.

This artifact is eligible evidence for the "Qualified frame Monte Carlo" row
in `MODE_QUALIFICATION.md`, and the correct verdict is `failed` at 5 dB RF
C/N: the gate requires every claimed operating point to clear the bound, and
5 dB does not.
