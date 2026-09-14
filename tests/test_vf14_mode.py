"""VF14 mode contract, loopback, FM-channel decode and sync calibration."""

import numpy as np
import pytest

from whale import framing, link_protocol, rx_audio, waveform
from whale.dsp import mfsk
from whale.fm_channel import ComplexFmChannel
from whale.mode_qualification import registry
from whale.modes.vf14 import PROFILES, VF14_4, VF14_8, VF14_16

MODES = tuple(PROFILES.values())
LEAD = 4_000


def _packet(size, seed=1):
    return np.random.default_rng(seed).integers(0, 256, size, np.uint8).tobytes()


def _capture(audio, after=2_000):
    return rx_audio.downsample(np.concatenate((
        np.zeros(LEAD, np.float32), np.asarray(audio, np.float32),
        np.zeros(after, np.float32))))


def _through_fm(mode, packet, cn_db, seed, profile="flat_nbfm"):
    channel = ComplexFmChannel.from_profile(48_000, profile, cn_db, seed)
    head = channel.process(np.concatenate((np.zeros(12_000, np.float32),
                                           mode.encode(packet)))).audio
    return rx_audio.downsample(np.concatenate((
        head, channel.drain().audio,
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, np.float32))))


def test_geometry_of_both_profiles():
    assert VF14_16.spacing_hz == 62.5 and VF14_16.symbol_samples == 768
    assert VF14_16.rx_symbol_samples == 192
    assert VF14_16.tx_bank.tone_hz[0] == 562.5
    assert VF14_16.tx_bank.tone_hz[-1] == 1500.0
    assert VF14_4.spacing_hz == 600 and VF14_4.symbol_samples == 80
    assert VF14_4.rx_symbol_samples == 20
    assert VF14_4.tx_bank.tone_hz[0] == 600.0
    assert VF14_4.tx_bank.tone_hz[-1] == 2400.0
    assert VF14_8.spacing_hz == 125.0 and VF14_8.symbol_samples == 384
    assert VF14_8.rx_symbol_samples == 96
    assert VF14_8.tx_bank.tone_hz[0] == 625.0
    assert VF14_8.tx_bank.tone_hz[-1] == 1500.0
    for mode in MODES:
        if mode is VF14_4:
            assert mode.codec.max_payload_bytes == 274
            assert mode.short_codec.max_payload_bytes == 20
            assert mode.medium_codec.max_payload_bytes == 68
        else:
            assert mode.codec.max_payload_bytes == 64
            assert mode.short_codec.max_payload_bytes == 11
            assert mode.medium_codec.max_payload_bytes == 36


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_mode_contract(mode):
    assert isinstance(mode, waveform.WaveformMode)
    assert mode.chunk_size == mode.max_payload_bytes - framing.AIR_HEADER_BYTES
    assert mode.mode_id in registry("fm", "experimental").supported_ids
    assert (mode.mode_id in registry("fm", "default").supported_ids) == (mode is VF14_4)


def test_vf14_4_is_the_fm_control_mode():
    for level in ("default", "optional", "experimental"):
        assert registry("fm", level).control is VF14_4


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_full_grid_carries_the_largest_connect_ack(mode):
    ids = registry("fm", "experimental").supported_ids
    body = link_protocol.encode_connect_ack("A" * 15, "B" * 15, ids,
                                            ids[-1], ids[-1], 0x5A)
    on_air = framing.AIR_HEADER_BYTES + len(body) - 2  # two bytes ride inline
    assert on_air <= mode.max_payload_bytes


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_short_grid_carries_every_bodyless_or_ack_packet(mode):
    ack = framing.AIR_HEADER_BYTES + 1
    assert mode.grid_symbols(ack) == mode.short_payload_symbols
    assert mode.airtime(ack) < mode.airtime(mode.max_payload_bytes)


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_medium_grid_carries_a_typical_connect_exchange(mode):
    ids = registry("fm", "experimental").supported_ids
    connect = link_protocol.encode_call_and_modes("STA1", "STA2", ids, ids[0], 0x5A)
    connect_ack = link_protocol.encode_connect_ack(
        "STA1", "STA2", ids, ids[0], ids[0], 0x5A)
    for body in (connect, connect_ack):
        on_air = framing.AIR_HEADER_BYTES + len(body) - 2
        assert on_air <= mode.max_payload_bytes
        expected = (mode.medium_payload_symbols
                    if on_air <= mode.medium_max_payload_bytes
                    else mode.payload_symbols)
        assert mode.grid_symbols(on_air) == expected


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_constant_envelope(mode):
    audio = np.asarray(mode.encode(_packet(mode.max_payload_bytes)), np.float64)
    body = audio[mode.head_symbols * mode.symbol_samples:-mode.tail_samples]
    assert np.max(np.abs(body)) == pytest.approx(0.6, abs=1e-3)
    crest = np.max(np.abs(body)) / np.sqrt(np.mean(body ** 2))
    assert crest == pytest.approx(np.sqrt(2.0), abs=0.02)


@pytest.mark.parametrize("size", (0, 10, 11, 12, 29, 36, 37, 64))
@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_clean_loopback_on_both_grids(mode, size):
    packet = _packet(size, seed=size)
    audio = mode.encode(packet)
    assert len(audio) / mode.tx_sample_rate == pytest.approx(mode.airtime(size))
    result = mode.decode(_capture(audio))
    assert result["payload"] == packet
    assert result["confidence"] >= mode.confidence_threshold
    expected_end = ((LEAD + len(audio)) // rx_audio.DECIMATION
                    + rx_audio.FILTER_DELAY_DECODE_SAMPLES)
    assert abs(result["end_index"] - expected_end) <= 4


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_a_partial_full_frame_waits_for_more_audio(mode):
    audio = mode.encode(_packet(mode.max_payload_bytes))
    arrived = len(audio) * 2 // 3
    result = mode.decode(_capture(audio[:arrived], after=0))
    assert result["confidence"] >= mode.confidence_threshold
    assert result["payload"] is None
    assert "end_index" not in result


#: VF14_4's 600 Bd rate-3/4 grid sits at a much higher C/N cliff than the
#: other two profiles (see the pass points in vf14.py), so -3 dB is well
#: below its own working point.
_MODERATE_CN_DB = {VF14_4: 0.0}


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_decodes_through_fm_channel_at_moderate_cn(mode):
    cn_db = _MODERATE_CN_DB.get(mode, -3.0)
    for seed in range(3):
        packet = _packet(mode.max_payload_bytes, seed=seed)
        result = mode.decode(_through_fm(mode, packet, cn_db, 100 + seed))
        assert result["payload"] == packet


@pytest.mark.parametrize("mode,cn_db,min_delivered", (
    (VF14_16, -7.0, 18),
    (VF14_4, 0.0, 20),
    (VF14_8, -6.0, 17),
), ids=("vf14-16", "vf14-4", "vf14-8"))
def test_full_grid_near_discriminator_threshold(mode, cn_db, min_delivered):
    packet = _packet(mode.max_payload_bytes, seed=42)
    delivered = sum(
        mode.decode(_through_fm(mode, packet, cn_db, 10_000 + seed))["payload"] == packet
        for seed in range(20))
    assert delivered >= min_delivered


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_sync_threshold_sits_above_noise(mode):
    """FM discriminator noise (clicks included) and white noise must not
    reach the acquisition threshold; see the calibration note in vf14.py."""
    peaks = []
    for seed, cn_db in enumerate((-10.0, 0.0, 20.0)):
        channel = ComplexFmChannel.from_profile(48_000, "flat_nbfm", cn_db, seed)
        audio = rx_audio.downsample(channel.process(
            np.zeros(240_000, np.float32)).audio)
        scores, _ = mfsk.correlate(mode.rx_bank, audio, mode.sync_pattern)
        peaks.append(float(scores.max()))
        assert mode.decode(audio)["payload"] is None
    noise = np.random.default_rng(9).normal(0.0, 0.1, 60_000)
    scores, _ = mfsk.correlate(mode.rx_bank, noise, mode.sync_pattern)
    peaks.append(float(scores.max()))
    assert max(peaks) < 0.85 * mode.confidence_threshold
