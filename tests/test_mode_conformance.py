"""One malformed-input contract run against every registry mode's decoder.

MODE_QUALIFICATION.md section 1.4 requires each mode to reject silence,
bounded white noise, a bare carrier, and non-finite or wrong-shaped audio
without raising or doing unbounded work. Truncated audio, corrupt
header/length, corrupt payload/CRC, and impossible declared length are
already exercised per mode (most completely for CPFSK; VF6 also has a
protected declared-length field, while VF3/HC0/HC1 are fixed-length) by
test_afsk_loopback.py,
test_vf3_mode.py, test_vf6_mode.py, test_hc0_mode.py, and test_hc1_mode.py,
so they are not repeated here.

Software only -- no radios, no sound cards.
"""

import numpy as np
import pytest

from whale import afsk, rx_audio
from whale.modes.hc0_mode import HC0
from whale.modes.hc1_mode import HC1
from whale.modes.hf2_mode import HF2
from whale.modes.hf3_mode import HF3
from whale.modes.vf3_mode import VF3
from whale.modes.vf4_mode import VF4
from whale.modes.vf6_mode import VF6

MODES = (afsk.PROFILE_300, afsk.PROFILE_600, afsk.PROFILE_1200,
         VF3, VF4, VF6, HC1, HC0, HF2, HF3)

RNG = np.random.default_rng(20260830)
CAPTURE_SECONDS = 3
CAPTURE_SAMPLES = CAPTURE_SECONDS * rx_audio.CAPTURE_SAMPLE_RATE


def _downsampled(audio):
    return rx_audio.downsample(np.asarray(audio, dtype=np.float64))


def _silence():
    return _downsampled(np.zeros(CAPTURE_SAMPLES))


def _white_noise():
    return _downsampled(RNG.normal(0.0, 0.2, CAPTURE_SAMPLES))


def _bare_carrier():
    t = np.arange(CAPTURE_SAMPLES) / rx_audio.CAPTURE_SAMPLE_RATE
    return _downsampled(0.3 * np.sin(2 * np.pi * 1_500.0 * t))


def _non_finite():
    audio = RNG.normal(0.0, 0.2, CAPTURE_SAMPLES)
    audio[CAPTURE_SAMPLES // 2] = np.nan
    audio[CAPTURE_SAMPLES // 3] = np.inf
    return _downsampled(audio)


def _wrong_shape():
    return RNG.normal(0.0, 0.2, (1_000, 2))


def _empty():
    return np.zeros(0, dtype=np.float64)


CASES = {
    "silence": _silence,
    "bounded white noise": _white_noise,
    "bare carrier": _bare_carrier,
    "non-finite audio": _non_finite,
    "wrong-shaped audio": _wrong_shape,
    "empty audio": _empty,
}


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_hostile_input_is_rejected_without_raising(mode, case):
    audio = CASES[case]()
    result = mode.decode(audio)
    assert result.get("payload") is None, (mode.name, case)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
