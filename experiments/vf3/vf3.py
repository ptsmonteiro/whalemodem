"""VF3, now shipped: this is a shim over `whale.modes.vf3`.

The DSP that produced RESULTS.md moved into the package when VF3 gained a
`WaveformMode` adapter (`whale/modes/vf3_mode.py`), so the modem and the
experiment cannot drift apart.  It moved unchanged: at the default head
duration `modulate()` still emits the identical waveform, which
`tests/test_vf3_mode.py` asserts, so the captures under `results/` replay as
they always did.

What the package version adds is the adaptive head the link negotiates -- a
longer lead-in, built the same way -- and the decode-result keys the link's
receive loop reads.  Everything the experiment uses is re-exported so `test_vf3.py`
and `run_air.py` keep working as written.

The DSP itself now lives in `whale/dsp/`, and the shipped module is a
configuration of those kernels; this shim tracks its public surface
rather than the private helpers that surface used to be built from.
"""

from whale.modes.vf3 import *  # noqa: F401,F403
