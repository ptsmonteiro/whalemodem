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

## Mode-2 diagnostic follow-up

The `diagnostics/` subdirectory contains 18 separate JSON artifacts for mode
2: each of `ic705_to_kg_uv9d`, `kg_uv9d_to_ic705`, and
`vhf_bench_conservative` at requested DATA sizes 88, 193, 255, 300, 350, and
402 bytes. Every artifact uses master seed `20260829`, RF C/N points 10, 20,
and 30 dB, and 20 trials per point. These 1,080 trials are diagnostic only,
not promotion evidence.

All frames acquired. Of them, 851 delivered and 229 failed payload/CRC; every
failure was exactly one wrong hard decision at the final body-CRC bit. Rates
were non-monotonic in both C/N and payload length. The conservative result was
trial-for-trial identical to `ic705_to_kg_uv9d` (280/360 each), versus 291/360
for `kg_uv9d_to_ic705`. Replaying a known failure from each preset with 10 ms
of post-frame audio processed through the stateful FM channel changed all
three to exact decodes. The evidence therefore identifies the direct-frame
runner's missing channel tail, not a conservative or directional preset
problem, payload-length sensitivity, or an RF-noise weakness.

See `MODE_QUALIFICATION.md` for the complete delivery matrix and conclusion,
and `CHANNELS.md` for the finite-buffer mechanism and proposed next
experiment. Mode 2's on-air format and registry disposition were not changed.
