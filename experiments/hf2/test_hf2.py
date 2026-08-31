"""Deterministic round-trip checks for the HF2 waveform (experiments/hf2/hf2.py).

Stage 2 of experiments/hf2/PLAN.md: prove the waveform itself -- geometry,
16-QAM mapping, FEC/framing, acquisition and channel tracking -- round-trips
payload bytes over a clean (no-channel) signal.  Channel robustness (AWGN,
Watterson fading) is stage 3/4's benchmark harness, not this test.
"""

import numpy as np
import pytest

from experiments.hf2 import hf2
from whale import rx_audio


def _capture(audio: np.ndarray) -> np.ndarray:
    """TX-rate audio -> RX-rate audio, the same path production decoding uses."""
    return rx_audio.downsample(np.concatenate((
        np.asarray(audio, dtype=np.float32),
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))


PAYLOADS = (
    b"",
    b"\x00" * hf2.MAX_PAYLOAD_BYTES,
    b"\xff" * hf2.MAX_PAYLOAD_BYTES,
    bytes(np.random.default_rng(20260831).integers(0, 256, size=64, dtype=np.uint8)),
    bytes(np.random.default_rng(1).integers(
        0, 256, size=hf2.MAX_PAYLOAD_BYTES, dtype=np.uint8)),
    b"hf2 round trip",
)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_clean_round_trip_recovers_the_exact_payload(payload):
    audio = hf2.modulate(payload)
    result = hf2.demodulate(_capture(audio))
    assert result["synced"] is True, result.get("failure")
    assert result["payload"] == payload
    assert result["crc_ok"] is True
    assert result["fec_tail_ok"] is True
    assert result["confidence"] >= hf2.ACQUISITION_THRESHOLD


def test_payload_over_the_limit_raises_value_error():
    with pytest.raises(ValueError):
        hf2.modulate(b"\x00" * (hf2.MAX_PAYLOAD_BYTES + 1))


def test_noise_and_silence_do_not_crash_and_do_not_falsely_sync():
    rng = np.random.default_rng(7)
    frame_len = len(hf2.modulate(b"noise probe"))

    noise = (rng.standard_normal(frame_len) * 0.05).astype(np.float32)
    result = hf2.demodulate(_capture(noise))
    assert result["synced"] is False
    assert result["payload"] is None

    silence = np.zeros(frame_len, dtype=np.float32)
    result = hf2.demodulate(_capture(silence))
    assert result["synced"] is False
    assert result["payload"] is None


def test_frame_geometry_matches_the_documented_numbers():
    assert hf2.MAX_PAYLOAD_BYTES == 117
    assert hf2.PAYLOAD_SYMBOLS == 99
    assert hf2.TOTAL_SYMBOLS == 109
    assert hf2.N_CARRIERS == 19 and hf2.N_DATA_CARRIERS == 11
    nominal_bit_rate = (hf2.MAX_PAYLOAD_BYTES * 8) / hf2.frame_seconds()
    assert nominal_bit_rate > 500  # the Level 2 floor, PLAN.md
