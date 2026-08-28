"""VF3, now shipped: this is a shim over `whale.modes.vf3`.

The DSP that produced RESULTS.md moved into the package when VF3 gained a
`WaveformMode` adapter (`whale/modes/vf3_mode.py`), so the modem and the
experiment cannot drift apart.  It moved unchanged: at the default head
duration `modulate()` still emits the identical waveform, which
`tests/test_vf3_mode.py` asserts, so the captures under `results/` replay as
they always did.

What the package version adds is the adaptive head the link negotiates -- a
longer lead-in, built the same way -- and the decode-result keys the link's
receive loop reads.  Everything below is re-exported so `test_vf3.py` and
`run_air.py` keep working as written.
"""

from whale.modes import _primitives as _base  # noqa: F401 -- test_vf3 uses it
from whale.modes.vf3 import *  # noqa: F401,F403
from whale.modes.vf3 import (  # noqa: F401 -- names `import *` will not take
    _DIFFERENTIAL_LABELS,
    _DIFFERENTIAL_POINTS,
    _INTERLEAVER,
    _SYNC_BITS,
    _TIME_SCALE,
    _TRAINING_BITS,
    _WHITENER,
    _acquire,
    _base_result,
    _check_constants,
    _decode_information,
    _estimate_timing,
    _fft_bank,
    _header_candidate_snr,
    _measure_head,
    _rolling_sum,
)
