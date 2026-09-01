"""Deterministic round-trip checks for the HF3 waveform (experiments/hf3/hf3.py).

Mirrors experiments/hf2/test_hf2.py's coverage: clean round trips at
zero/representative/max payload, oversize-payload rejection, and hostile
input (noise/silence) not crashing or falsely syncing. Channel robustness
(AWGN, Watterson fading) is the benchmark harness's job, not this file's.
"""

import numpy as np
import pytest

from experiments.hf3 import hf3
from experiments.hf3 import measure_bandwidth
from whale import rx_audio


def _capture(audio: np.ndarray) -> np.ndarray:
    """TX-rate audio -> RX-rate audio, the same path production decoding uses."""
    return rx_audio.downsample(np.concatenate((
        np.asarray(audio, dtype=np.float32),
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))


PAYLOADS = (
    b"",
    b"\x00" * hf3.MAX_PAYLOAD_BYTES,
    b"\xff" * hf3.MAX_PAYLOAD_BYTES,
    bytes(np.random.default_rng(20260831).integers(0, 256, size=64, dtype=np.uint8)),
    bytes(np.random.default_rng(1).integers(
        0, 256, size=hf3.MAX_PAYLOAD_BYTES, dtype=np.uint8)),
    b"hf3 round trip",
)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_clean_round_trip_recovers_the_exact_payload(payload):
    audio = hf3.modulate(payload)
    result = hf3.demodulate(_capture(audio))
    assert result["synced"] is True, result.get("failure")
    assert result["payload"] == payload
    assert result["crc_ok"] is True
    assert result["fec_tail_ok"] is True
    assert result["confidence"] >= hf3.ACQUISITION_THRESHOLD


def test_payload_over_the_limit_raises_value_error():
    with pytest.raises(ValueError):
        hf3.modulate(b"\x00" * (hf3.MAX_PAYLOAD_BYTES + 1))


def test_noise_and_silence_do_not_crash_and_do_not_falsely_sync():
    rng = np.random.default_rng(7)
    frame_len = len(hf3.modulate(b"noise probe"))

    noise = (rng.standard_normal(frame_len) * 0.05).astype(np.float32)
    result = hf3.demodulate(_capture(noise))
    assert result["synced"] is False
    assert result["payload"] is None

    silence = np.zeros(frame_len, dtype=np.float32)
    result = hf3.demodulate(_capture(silence))
    assert result["synced"] is False
    assert result["payload"] is None


@pytest.mark.parametrize("case", [
    "empty", "wrong_shape", "non_finite", "truncated", "bare_carrier"])
def test_hostile_input_never_raises_and_never_falsely_syncs(case):
    if case == "empty":
        audio = np.zeros(0, dtype=np.float64)
    elif case == "wrong_shape":
        audio = np.random.default_rng(3).normal(0, 0.2, (1_000, 2))
    elif case == "non_finite":
        audio = np.random.default_rng(4).normal(0, 0.2, 20_000)
        audio[100] = np.nan
        audio[200] = np.inf
    elif case == "truncated":
        audio = _capture(hf3.modulate(b"short"))[:500]
    else:
        t = np.arange(20_000) / rx_audio.CAPTURE_SAMPLE_RATE
        audio = _capture(0.3 * np.sin(2 * np.pi * 1_200.0 * t))
    result = hf3.demodulate(audio)
    assert result["payload"] is None


def _occupied_bandwidth_99(audio, sample_rate):
    """99%-power occupied bandwidth, per MODE_QUALIFICATION.md section 6."""
    spectrum = np.abs(np.fft.rfft(audio)) ** 2
    freqs = np.fft.rfftfreq(len(audio), 1.0 / sample_rate)
    cumulative = np.cumsum(spectrum)
    total = cumulative[-1]
    low = freqs[np.searchsorted(cumulative, total * 0.005)]
    high = freqs[np.searchsorted(cumulative, total * 0.995)]
    return low, high


def test_bandwidth_campaign_confidence_is_distribution_free_and_promotion_sized():
    confidence = measure_bandwidth.quantile_upper_bound_confidence(300, 0.99)
    assert confidence >= 0.95
    assert measure_bandwidth.quantile_upper_bound_confidence(298, 0.99) < 0.95


def test_bandwidth_campaign_smoke_covers_both_payload_classes():
    result = measure_bandwidth.run_campaign(
        trials=2, seed=17,
        payload_lengths=(hf3.MAX_PAYLOAD_BYTES // 2, hf3.MAX_PAYLOAD_BYTES))
    assert result["passes"] is True
    assert [row["payload_bytes"] for row in result["payload_classes"]] == [
        hf3.MAX_PAYLOAD_BYTES // 2, hf3.MAX_PAYLOAD_BYTES]
    assert all(len(row["measurements"]) == 2
               for row in result["payload_classes"])


@pytest.mark.parametrize("payload_len", [
    hf3.MAX_PAYLOAD_BYTES // 2, hf3.MAX_PAYLOAD_BYTES])
def test_occupied_bandwidth_is_under_the_2300hz_ceiling(payload_len):
    payload = bytes(np.random.default_rng(9).integers(
        0, 256, size=payload_len, dtype=np.uint8))
    audio = hf3.modulate(payload)
    low, high = _occupied_bandwidth_99(audio, hf3.SAMPLE_RATE)
    assert high - low < 2_300  # SPEED_LADDERS.md's occupied-bandwidth cap


def test_frame_geometry_matches_the_documented_numbers():
    assert hf3.MAX_PAYLOAD_BYTES == 803
    assert hf3.PAYLOAD_SYMBOLS == 120
    assert hf3.TOTAL_SYMBOLS == 126
    assert hf3.N_CARRIERS == 36 and hf3.N_DATA_CARRIERS == 27
    nominal_bit_rate = (hf3.MAX_PAYLOAD_BYTES * 8) / hf3.frame_seconds()
    assert nominal_bit_rate > 2_000  # the Level 3 floor, SPEED_LADDERS.md
    band_hz = hf3.CARRIER_HZ[-1] - hf3.CARRIER_HZ[0]
    assert hf3.CARRIER_HZ[-1] < 2_300  # nominal span stays under the cap


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
