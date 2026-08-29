# CPFSK VHF FM qualification campaign, 2026-08-29

Command:

```powershell
python scripts/benchmark_simulated_channels.py --model fm --policy vhf-fm --fm-preset vhf_bench_conservative --points 5 10 15 20 25 30 --trials 100 --modes 300baud 600baud 1200baud --out logs/mode_qualification/vhf-fm/cpfsk/2026-08-29/fm_frame_monte_carlo.json
```

Result: failed as a CPFSK-group qualification gate. The 300-baud rung
delivered 600/600 frames and the 600-baud rung 598/600. The 1200-baud rung
delivered 454/600 and missed the FER bound at all six points. Acquisition was
600/600 for every rung and the run had no exception outcomes.

The JSON contains the exact per-trial seeds, expanded channel descriptions,
decoder and channel measurements, Wilson intervals, commit, and dirty state.
The tree was dirty because this campaign immediately followed implementation
of the qualification manifest; this artifact is initial evidence and is not
eligible to support default promotion.
