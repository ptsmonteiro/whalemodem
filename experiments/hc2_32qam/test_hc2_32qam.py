import numpy as np
import pytest

from experiments.hc2_32qam import hc2_32qam as hc2


def test_geometry_and_rate_budget_clear_top_hf_target():
    assert hc2.N_CARRIERS == 49
    assert hc2.CARRIER_SPACING_HZ == pytest.approx(46.875)
    assert hc2.SYMBOL_RATE == pytest.approx(41.6666666667)
    assert hc2.CARRIER_HZ[-1] - hc2.CARRIER_HZ[0] == pytest.approx(2250.0)
    assert hc2.RAW_BIT_RATE == pytest.approx(10208.3333333)
    assert hc2.CODED_INFORMATION_RATE == pytest.approx(7656.25)
    assert hc2.SUSTAINED_USER_BIT_RATE > 7050


@pytest.mark.parametrize("seed", [1, 17, 20260830])
def test_full_payload_round_trips_exactly_in_clean_oracle_channel(seed):
    payload = np.random.default_rng(seed).integers(
        0, 256, hc2.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    audio = hc2.modulate(payload)
    assert len(audio) == hc2.FRAME_SAMPLES
    assert hc2.demodulate_oracle(audio) == payload


def test_short_payload_and_crc_path_round_trip():
    payload = b"speed-first HC2"
    assert hc2.demodulate_oracle(hc2.modulate(payload)) == payload


def test_oversize_payload_is_rejected():
    with pytest.raises(ValueError, match="maximum"):
        hc2.modulate(bytes(hc2.MAX_PAYLOAD_BYTES + 1))
