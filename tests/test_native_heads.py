"""Native adaptive-head contracts for the production HF modes."""

import numpy as np
import pytest

from whale import framing, rx_audio
from whale.modes.hc0_mode import HC0
from whale.modes.hc1w_mode import HC1W
from whale.modes.hf2_mode import HF2
from whale.modes.hf7_mode import HF7, HF7_PHY
from whale.modes.hf8_mode import HF8, HF8_PHY
from whale.modes.hr0_mode import HR0


MODES = (HR0, HC0, HF2, HC1W, HF8, HF7)


def _capture(audio):
    padded = np.concatenate((
        np.asarray(audio, np.float32),
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, np.float32)))
    return rx_audio.downsample(padded)


@pytest.mark.parametrize("mode", MODES, ids=lambda mode: mode.name)
def test_native_head_round_trip_and_measurement(mode):
    payload = bytes(i & 0xFF for i in range(mode.chunk_size + framing.AIR_HEADER_BYTES))
    result = mode.decode(_capture(mode.encode(payload)))
    assert result["payload"] == payload
    assert result["head_seconds_received"] > 0.0
    assert result["head_match"] >= 0.5


@pytest.mark.parametrize(
    "mode,phy", ((HF8, HF8_PHY), (HF7, HF7_PHY)),
    ids=("hf8", "hf7"),
)
def test_ofdm_native_head_stays_near_body_power(mode, phy):
    payload = bytes(i & 0xFF for i in range(mode.chunk_size + framing.AIR_HEADER_BYTES))
    encoded = mode.encode(payload)
    head_samples = phy.head_samples() * 4
    active_body_samples = round(phy.frame_seconds() * mode.tx_sample_rate)
    # Ignore the intentional 5 ms start-of-keying fade.  Head and body are
    # jointly rendered, so this catches either an AGC-provoking level step or
    # a head peak that steals drive from the qualified body waveform.
    head = encoded[240:head_samples]
    body = encoded[head_samples:head_samples + active_body_samples]
    head_rms = float(np.sqrt(np.mean(head ** 2)))
    body_rms = float(np.sqrt(np.mean(body ** 2)))
    assert abs(20.0 * np.log10(head_rms / body_rms)) < 0.5
    assert np.max(np.abs(head)) <= np.max(np.abs(body))


@pytest.mark.parametrize("phy", (HF8_PHY, HF7_PHY), ids=("hf8", "hf7"))
def test_ofdm_head_is_a_two_symbol_repeat_not_a_body_preamble(phy):
    block = phy._native_head_block
    assert len(block) == 2 * phy.symbol_len
    assert phy.head_samples(0.5) % len(block) == 0
    adjacent = np.vdot(block[:phy.symbol_len], block[phy.symbol_len:])
    adjacent /= (np.linalg.norm(block[:phy.symbol_len])
                 * np.linalg.norm(block[phy.symbol_len:]))
    assert abs(adjacent) < 0.2


@pytest.mark.parametrize("phy", (HF8_PHY, HF7_PHY), ids=("hf8", "hf7"))
@pytest.mark.parametrize("head_seconds", (None, 0.5, 1.0))
def test_ofdm_head_alone_cannot_publish_a_body_start(phy, head_seconds):
    captured = _capture(phy.native_head(head_seconds))
    result = phy.demodulate(captured)
    assert result.get("start_sample") is None
    assert result["preamble_repeat_coherence"] is None


@pytest.mark.parametrize("mode", MODES, ids=lambda mode: mode.name)
def test_clipped_native_head_still_decodes_and_reports_shorter_head(mode):
    payload = bytes(i & 0xFF for i in range(mode.chunk_size + framing.AIR_HEADER_BYTES))
    audio = mode.encode(payload, head_seconds=0.5)
    full = mode.decode(_capture(audio))["head_seconds_received"]
    blackout = min(len(audio) // 3, round(0.2 * mode.tx_sample_rate))
    clipped = np.concatenate((np.zeros(blackout, np.float32),
                              audio[blackout:]))
    result = mode.decode(_capture(clipped))
    assert result["payload"] == payload
    assert 0.0 < result["head_seconds_received"] < full
