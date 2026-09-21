"""Software conformance and loopback tests for the VFS2 SC-FDE rung."""

import numpy as np
import pytest

from whale import framing, rx_audio, waveform
from whale.dsp import ldpc
from whale.fm_channel import ComplexFmChannel
from whale.mode_qualification import QualificationLevel, qualification_level, registry
from whale.modes.vfs2 import VFS2
from whale.phy import scfde


def _payload():
    return np.arange(VFS2.max_payload_bytes, dtype=np.uint8).tobytes()


def _loopback(audio):
    return rx_audio.downsample(np.concatenate((
        np.zeros(4_800, np.float32), np.asarray(audio, np.float32),
        np.zeros(4_800, np.float32))))


def _through_fm(packet, cn_db, seed, profile="flat_nbfm"):
    channel = ComplexFmChannel.from_profile(48_000, profile, cn_db, seed)
    head = channel.process(np.concatenate((np.zeros(12_000, np.float32),
                                           VFS2.encode(packet)))).audio
    return rx_audio.downsample(np.concatenate((
        head, channel.drain().audio,
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, np.float32))))


def test_mode_contract():
    assert isinstance(VFS2, waveform.WaveformMode)
    assert VFS2.name == "vfs2" and VFS2.mode_id == 25
    assert VFS2.fec_rate == "1/2" and VFS2.bits_per_symbol_order == 2
    assert VFS2.max_payload_bytes == VFS2.chunk_size + framing.AIR_HEADER_BYTES
    assert VFS2.packet_bytes == VFS2.n_codewords * ldpc.INFORMATION_BITS["1/2"] // 8


def test_shared_scfde_geometry():
    """The geometry rungs 1-3 hold in common; changing it breaks them all."""
    assert (scfde.FFT_SIZE, scfde.CP_LEN, scfde.SPREAD) == (384, 24, 64)
    assert scfde.BIN_SPACING_HZ == 31.25
    assert VFS2.symbol_samples == 408  # 34 ms at 12 kHz
    assert VFS2.band_hz == (468.75, 2437.5)
    assert VFS2.bits_per_block == 128
    # 64 symbols per 34 ms block, QPSK: 1,882 Bd, 3,765 bit/s raw.
    assert 1880 <= scfde.SAMPLE_RATE / VFS2.symbol_samples * scfde.SPREAD <= 1885


def test_pilot_blocks_bracket_every_data_group():
    pilots, data, total = VFS2.block_plan
    assert len(data) == VFS2.payload_blocks
    assert pilots[0] == 0 and pilots[-1] == total - 1
    assert len(pilots) == -(-VFS2.payload_blocks // VFS2.pilot_block_stride) + 1
    assert sorted(set(pilots) | set(data)) == list(range(total))


def test_pilot_and_cazac_sequences_are_constant_modulus():
    assert np.allclose(np.abs(scfde.cazac(scfde.PILOT_ROOT)), 1.0)
    assert np.allclose(np.abs(VFS2.pilot_values), 1.0)


def test_frame_budget_and_net_rate():
    assert VFS2.airtime(VFS2.max_payload_bytes) == pytest.approx(5.010, abs=0.002)
    assert VFS2.bits_per_second == pytest.approx(1527, abs=5)


def test_papr_is_below_the_ofdm_rungs():
    """The reason this family exists: the mic path clips peaks."""
    from whale.modes.vf12 import VF12
    def papr(audio):
        return 20 * np.log10(np.max(np.abs(audio)) / np.sqrt(np.mean(audio ** 2)))
    assert papr(VFS2.encode(_payload())) < papr(VF12.encode(b"\0" * 100)) - 2.0


def test_full_capacity_clean_loopback():
    payload = _payload()
    result = VFS2.decode(_loopback(VFS2.encode(payload)))
    assert result["payload"] == payload
    assert result["crc_ok"] is True
    assert result["codewords_ok"] == VFS2.n_codewords


def test_simulated_flat_fm_floor():
    """Documented floor: 0 dB flat_nbfm C/N passes, -1 dB does not."""
    payload = _payload()
    assert all(VFS2.decode(_through_fm(payload, 0, 1000 + t))["payload"] == payload
               for t in range(3))


def test_clock_offset_is_tracked():
    """+-50 ppm soundcard error must not cost the frame."""
    payload = _payload()
    audio = VFS2.encode(payload)
    for ppm in (-50, 50):
        count = int(round(len(audio) * (1 + ppm * 1e-6)))
        stretched = np.interp(np.linspace(0, len(audio) - 1, count),
                              np.arange(len(audio)), audio)
        assert VFS2.decode(_loopback(stretched))["payload"] == payload


def test_registered_as_experimental_fm_rung():
    assert qualification_level("fm", VFS2.mode_id) == QualificationLevel.EXPERIMENTAL
    assert VFS2.mode_id not in registry("fm", "default").supported_ids
    assert VFS2.mode_id in registry("fm", "experimental").supported_ids


def test_nonfinite_or_wrong_shaped_audio_is_rejected():
    for audio in (np.array([np.nan]), np.array([np.inf]), np.zeros((4, 2))):
        result = VFS2.decode(audio)
        assert result["payload"] is None and result["crc_ok"] is False
