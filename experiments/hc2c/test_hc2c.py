import numpy as np

from whale import rx_audio
from whale.modes import hc1

from .hc2c import (CODEC, DATA_SYMBOLS, MODE, PAYLOAD_SYMBOLS,
                   PILOT_POSITIONS, VARIANT)


def test_geometry_and_pilot_cost():
    assert PAYLOAD_SYMBOLS == 90
    assert len(PILOT_POSITIONS) == 8
    assert DATA_SYMBOLS == 82
    assert CODEC.unused_information_bits == 0
    assert VARIANT.max_payload_bytes == 188
    assert VARIANT.frame_seconds == 1.5213333333333334


def test_clean_full_capacity_round_trip_and_restoration():
    baseline = (hc1.PAYLOAD_SYMBOLS, hc1.TOTAL_SYMBOLS, hc1.CODEC, hc1._diff)
    payload = bytes(np.random.default_rng(21000).integers(
        0, 256, VARIANT.max_payload_bytes, dtype=np.uint8))
    audio = MODE.encode(payload)
    capture = rx_audio.downsample(np.concatenate((
        audio, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES))))
    result = MODE.decode(capture)
    assert result["payload"] == payload
    assert (hc1.PAYLOAD_SYMBOLS, hc1.TOTAL_SYMBOLS, hc1.CODEC, hc1._diff) == baseline
