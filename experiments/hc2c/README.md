# HC2c — payload-pilot tracking experiment

HC2c compares HC2b's selected 1.521-second differential-QPSK frame with an
otherwise identical frame containing eight evenly spaced, known full-band
payload pilots. The receiver unwraps and interpolates per-carrier phase
between the header and pilots before differential decoding. Each pilot also
resets its carrier's differential chain.

The pilots reduce maximum physical payload from 207 to 188 bytes while
holding airtime, FEC, cyclic prefix, carrier geometry, and modulation fixed.

Run:

    python -m pytest experiments/hc2c/test_hc2c.py -q
    python -m experiments.hc2c.benchmark_hc2c \
      --out logs/scratch/hc2c_paired_screen.json

The bounded 20-trial screen is candidate-selection evidence, not mode
qualification.

See `RESULTS.md` for the paired results and the proposed reset-versus-tracking
ablation.
