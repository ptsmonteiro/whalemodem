"""VFS1 (FM SC-FDE rung 1) geometry, differential mapping and FM decode."""

import numpy as np
import pytest

from whale import framing, rx_audio
from whale.fm_channel import ComplexFmChannel
from whale.mode_qualification import registry
from whale.modes.vfs1 import VFS1
from whale.phy import scfde


def _packet(seed=1):
    return np.random.default_rng(seed).integers(
        0, 256, VFS1.chunk_size, np.uint8).tobytes()


def _through_fm(packet, cn_db, seed, profile="flat_nbfm"):
    channel = ComplexFmChannel.from_profile(48_000, profile, cn_db, seed)
    head = channel.process(np.concatenate((np.zeros(12_000, np.float32),
                                           VFS1.encode(packet)))).audio
    return rx_audio.downsample(np.concatenate((
        head, channel.drain().audio,
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, np.float32))))


def test_geometry_is_the_shared_scfde_plan():
    assert VFS1.mode_id == 24
    assert VFS1.band_hz == (468.75, 2437.5)
    assert scfde.SPREAD == 64 and scfde.FFT_SIZE == 384 and scfde.CP_LEN == 24
    assert VFS1.symbol_samples == 408  # 34 ms at 12 kHz
    assert VFS1.fec_rate == "1/2" and VFS1.bits_per_symbol_order == 1
    assert VFS1.max_payload_bytes == 405 - 6
    assert VFS1.chunk_size == VFS1.max_payload_bytes - framing.AIR_HEADER_BYTES
    assert VFS1.airtime(VFS1.chunk_size) == pytest.approx(4.61, abs=0.01)
    assert VFS1.bits_per_second == pytest.approx(675, abs=1)


def test_one_reference_symbol_per_block_carries_no_bit():
    assert VFS1.bits_per_block == scfde.SPREAD - 1 == 63
    symbols = VFS1._differential(np.zeros(VFS1.payload_blocks * 63, np.uint8))
    # An all-zero bit stream is "no change", so every symbol stays at +1.
    assert np.allclose(symbols, 1.0)
    stream = np.zeros(VFS1.payload_blocks * 63, np.uint8)
    stream[0] = 1
    flipped = VFS1._differential(stream)
    assert flipped[0, 0] == 1.0 and np.allclose(flipped[0, 1:], -1.0)


def test_loopback_round_trip():
    packet = _packet()
    audio = np.asarray(VFS1.encode(packet), float)
    capture = np.concatenate((np.zeros(3_000), audio.reshape(-1, 4)[:, 0],
                              np.zeros(3_000)))
    result = VFS1.decode(capture)
    assert result["synced"] and result["crc_ok"]
    assert result["payload"] == packet
    assert result["codewords_ok"] == VFS1.n_codewords


def test_peak_to_average_stays_under_the_ofdm_rungs():
    """The reason this family is single-carrier: the mic path clips peaks."""
    from whale.modes.vf16 import VF16
    def crest(mode, size):
        audio = np.asarray(mode.encode(_packet()[:size]), float)
        body = audio[int(0.3 * 48_000):-int(0.3 * 48_000)]
        return 10 * np.log10(np.percentile(body ** 2, 99.9) / np.mean(body ** 2))
    assert crest(VFS1, VFS1.chunk_size) < crest(VF16, VF16.chunk_size) - 0.5


@pytest.mark.parametrize("cn_db, expected", [(0.0, True), (-1.0, False)])
def test_simulated_flat_fm_threshold(cn_db, expected):
    """0 dB passes and -1 dB does not: the floor docs/MODES.md records."""
    delivered = 0
    for seed in range(5):
        packet = _packet(seed)
        result = VFS1.decode(_through_fm(packet, cn_db, seed))
        delivered += result["crc_ok"] and result["payload"] == packet
    assert (delivered == 5) is expected


def test_registered_on_the_fm_ladder_in_rate_order():
    modes = registry("fm", "experimental").modes
    rates = [mode.bits_per_second if hasattr(mode, "bits_per_second")
             else 0 for mode in modes]
    assert VFS1.mode_id in {mode.mode_id for mode in modes}
    index = [mode.mode_id for mode in modes].index(VFS1.mode_id)
    assert rates[index] == pytest.approx(675, abs=1)
