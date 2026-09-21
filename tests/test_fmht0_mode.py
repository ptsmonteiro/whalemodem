"""FMHT0 (FM-handheld rung 0) geometry, repetition and loopback decode."""

import numpy as np
import pytest

from whale import framing, rx_audio
from whale.fm_channel import ComplexFmChannel
from whale.mode_qualification import registry
from whale.modes.fmht0 import FMHT0, _combine, _expand

LEAD = 4_000


def _packet(size, seed=1):
    return np.random.default_rng(seed).integers(0, 256, size, np.uint8).tobytes()


def _capture(audio, after=20_000):
    return rx_audio.downsample(np.concatenate((
        np.zeros(LEAD, np.float32), np.asarray(audio, np.float32),
        np.zeros(after, np.float32))))


def _through_fm(packet, cn_db, seed):
    channel = ComplexFmChannel.from_profile(48_000, "flat_nbfm", cn_db, seed)
    head = channel.process(np.concatenate((np.zeros(12_000, np.float32),
                                           FMHT0.encode(packet)))).audio
    return rx_audio.downsample(np.concatenate((
        head, channel.drain().audio,
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, np.float32))))


def test_geometry():
    assert FMHT0.mode_id == 30
    assert FMHT0.tone_count == 4 and FMHT0.baud == 400.0
    assert FMHT0.spacing_hz == 400.0
    assert FMHT0.band_hz == (800.0, 2000.0)
    # 250 ms of shared preamble: energy burst then timing sequence.
    assert (FMHT0.head_symbols + FMHT0.sync_symbols) * FMHT0.symbol_seconds \
        == pytest.approx(0.25)
    assert FMHT0.short_max_payload_bytes == 11  # exactly a DATA_ACK
    assert FMHT0.max_payload_bytes == 68
    assert FMHT0.chunk_size == 68 - framing.AIR_HEADER_BYTES
    assert FMHT0.net_bit_rate() == pytest.approx(184.1, abs=0.1)


def test_overall_code_rate_is_one_third():
    codec = FMHT0.codec
    transmitted = FMHT0.payload_symbols * FMHT0.bits_per_symbol
    assert codec.coded_bits == transmitted
    assert codec.inner.information_bits * 3 == transmitted


def test_repetition_round_trips_soft_values():
    bits = np.arange(12, dtype=np.float64)
    expanded = _expand(bits)
    assert len(expanded) == 18
    combined = _combine(expanded)
    assert len(combined) == 12
    # Every second bit was sent twice, so its metric is doubled.
    assert combined[::2] == pytest.approx(bits[::2] * 2)
    assert combined[1::2] == pytest.approx(bits[1::2])


@pytest.mark.parametrize("size", [0, 11, 28, 68])
def test_loopback_every_grid(size):
    packet = _packet(size)
    result = FMHT0.decode(_capture(FMHT0.encode(packet)))
    assert result["synced"] and result["payload"] == packet


def test_decodes_at_the_simulated_fm_floor():
    packet = _packet(FMHT0.max_payload_bytes, seed=7)
    assert all(FMHT0.decode(_through_fm(packet, -4.0, 100 + seed))["payload"]
               == packet for seed in range(5))


def test_registered_experimental_only():
    assert FMHT0.mode_id in {m.mode_id for m in registry("fm", "experimental").modes}
    assert FMHT0.mode_id not in {m.mode_id for m in registry("fm", "default").modes}
