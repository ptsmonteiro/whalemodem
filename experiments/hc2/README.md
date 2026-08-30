# HC2 — candidate fast HF-SSB rung (differential 8-PSK, K=9)

Experimental, unqualified, uncommitted-to-registry mode explored as a
possible fast rung above HC1 (`whale/modes/hc1.py`). See `hc2.py`'s module
docstring for the design and `RESULTS.md` for benchmark numbers and why
this particular design does not clear the bar to replace or complement HC1.

Files:

- `hc2.py` — the waveform: modulate/demodulate, reusing `whale/dsp/`.
- `hc2_mode.py` — a `WaveformMode`-shaped wrapper so `whale.qualification`'s
  trial runner can drive it, the way `whale/modes/hc1_mode.py` wraps
  `hc1.py`. Not registered anywhere; HC2 has no on-air mode ID.
- `benchmark_hc2.py` — Monte Carlo frame sweep across AWGN and the three
  mid-latitude Watterson presets, same trial machinery and Wilson-interval
  summary as `scripts/benchmark_simulated_channels.py`.
- `RESULTS.md` — the numbers, decode CPU cost, and the failure analysis.

Run:

    python experiments/hc2/benchmark_hc2.py --out-dir logs/scratch/hc2

(the retained evidence run instead used
`logs/mode_qualification/hf-ssb/hc2-experimental/2026-08-30/`, since it is
worth keeping as a negative result -- see `LOGS.md` for that convention.)
