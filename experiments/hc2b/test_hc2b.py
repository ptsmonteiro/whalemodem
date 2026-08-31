import numpy as np
import pytest

from whale import rx_audio
from whale.modes import hc1

from .hc2b import MODES, PAYLOAD_SYMBOL_MATRIX, VARIANTS


@pytest.mark.parametrize("mode", MODES, ids=lambda mode: mode.name)
def test_clean_full_capacity_round_trip(mode):
    payload = bytes(np.random.default_rng(mode.mode_id).integers(
        0, 256, mode.variant.max_payload_bytes, dtype=np.uint8))
    audio = mode.encode(payload)
    capture = rx_audio.downsample(np.concatenate((
        audio, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES))))
    assert mode.decode(capture)["payload"] == payload


def test_matrix_has_exact_whole_byte_grids_and_increasing_throughput():
    assert tuple(v.payload_symbols for v in VARIANTS) == PAYLOAD_SYMBOL_MATRIX
    assert all(v.codec.unused_information_bits == 0 for v in VARIANTS)
    rates = [v.max_payload_bytes * 8 / v.frame_seconds for v in VARIANTS]
    assert rates == sorted(rates)
    assert rates[-1] > 1.25 * rates[0]


def test_experimental_ids_are_unique_and_outside_registry_range():
    assert len({mode.mode_id for mode in MODES}) == len(MODES)
    assert min(mode.mode_id for mode in MODES) >= 20_000


def test_trial_leaves_production_hc1_configuration_untouched():
    baseline = (hc1.PAYLOAD_SYMBOLS, hc1.TOTAL_SYMBOLS, hc1.CODEC)
    mode = MODES[-1]
    payload = bytes(mode.variant.max_payload_bytes)
    audio = mode.encode(payload)
    capture = rx_audio.downsample(np.concatenate((
        audio, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES))))
    assert mode.decode(capture)["payload"] == payload
    assert (hc1.PAYLOAD_SYMBOLS, hc1.TOTAL_SYMBOLS, hc1.CODEC) == baseline
