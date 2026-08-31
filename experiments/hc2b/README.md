# HC2b — QPSK frame-length experiment

HC2b asks the cheapest question left by HC2's differential 8-PSK failure:
how much useful rate can HC1 gain simply by amortizing its fixed header,
lead, and tail over a longer QPSK payload, and how quickly does a longer
frame become less reliable on time-varying HF channels?

The experiment uses production HC1 unchanged except for its payload-symbol
count, derived packet capacity, interleaver, and frame duration. Its four
grids are 34, 58, 90, and 130 payload symbols, approximately 0.8, 1.1, 1.5,
and 2.1 seconds including the common HF lead. All retain differential QPSK
and rate-1/2 K=7 coding.

Run the deterministic tests:

    python -m pytest experiments/hc2b/test_hc2b.py -q

Run the bounded candidate screen:

    python -m experiments.hc2b.benchmark_hc2b \
      --out logs/scratch/hc2b_screen.json

This 20-trial screen is not qualification evidence. A survivor must be rerun
through the full matrix and gates in `MODE_QUALIFICATION.md`.

See `RESULTS.md` for the retained bounded-screen analysis and selected
1.521-second follow-up candidate.
