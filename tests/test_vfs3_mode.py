"""Software conformance and clean loopback tests for VFS3."""

import numpy as np
import pytest

from whale import framing, rx_audio, waveform
from whale.fm_channel import ComplexFmChannel
from whale.mode_qualification import QualificationLevel, qualification_level, registry
from whale.modes.vfs2 import VFS2
from whale.modes.vfs3 import VFS3


def _through_fm(mode, packet, cn_db, seed, profile="flat_nbfm"):
    channel = ComplexFmChannel.from_profile(48_000, profile, cn_db, seed)
    head = channel.process(np.concatenate((np.zeros(12_000, np.float32),
                                           mode.encode(packet)))).audio
    return rx_audio.downsample(np.concatenate((
        head, channel.drain().audio,
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, np.float32))))


def test_mode_contract_and_capacity():
    assert isinstance(VFS3, waveform.WaveformMode)
    assert VFS3.name == "vfs3"
    assert VFS3.mode_id == 26
    assert VFS3.fec_rate == "3/4"
    assert VFS3.bits_per_symbol_order == 2  # QPSK
    assert VFS3.n_codewords == 24
    assert VFS3.max_payload_bytes == 1452
    assert VFS3.chunk_size == VFS3.max_payload_bytes - framing.AIR_HEADER_BYTES


def test_differs_from_vfs2_only_in_code_rate():
    """The whole point of the rung: same waveform, same airtime, more payload.

    An LDPC codeword is 648 coded bits at every rate, so the same codeword
    count occupies the same blocks; only the information each carries moves.
    """
    for attribute in ("bits_per_symbol_order", "n_codewords", "pilot_block_stride",
                      "symbol_samples", "section_blocks", "total_blocks",
                      "lead_in_seconds", "tail_seconds", "tilt_db"):
        assert getattr(VFS3, attribute) == getattr(VFS2, attribute)
    assert VFS3.airtime(VFS3.max_payload_bytes) == pytest.approx(
        VFS2.airtime(VFS2.max_payload_bytes))
    assert VFS3.airtime(VFS3.max_payload_bytes) == pytest.approx(5.010, abs=0.002)
    assert VFS3.fec_rate == "3/4" and VFS2.fec_rate == "1/2"
    assert VFS3.max_payload_bytes == pytest.approx(1.5 * VFS2.max_payload_bytes, rel=0.01)


def test_registered_experimental_above_vfs2():
    names = [mode.name for mode in registry("fm", "experimental").modes]
    assert names.index("vfs3") > names.index("vfs2")
    assert qualification_level("fm", VFS3.mode_id) == QualificationLevel.EXPERIMENTAL


def test_full_capacity_clean_loopback():
    payload = np.arange(VFS3.max_payload_bytes, dtype=np.uint8).tobytes()
    result = VFS3.decode(rx_audio.downsample(VFS3.encode(payload)))

    assert result["payload"] == payload
    assert result["crc_ok"] is True
    assert result["codewords_ok"] == VFS3.n_codewords


def test_acquisition_is_not_fooled_by_the_repeated_sync_pair():
    """The sync and training pairs are each one CAZAC block repeated.

    `_fit` solves a free complex gain per bin, so a repeated constant-modulus
    pair fits *any* CAZAC root exactly. Acquiring on the training pair alone
    therefore locks two blocks early and the payload decodes as noise -- which
    is what happened on the first radio captures. Acquisition must land on the
    true first sync block.
    """
    payload = np.arange(VFS3.max_payload_bytes, dtype=np.uint8).tobytes()
    audio = rx_audio.downsample(VFS3.encode(payload))
    start, evm = VFS3._acquire(audio)

    assert evm < 0.05
    expected = VFS3.lead_in_samples
    assert abs(start - expected) <= VFS3.symbol_samples // 2


def test_decodes_through_the_fm_channel_at_its_pass_point():
    payload = np.arange(VFS3.max_payload_bytes, dtype=np.uint8).tobytes()
    assert VFS3.decode(_through_fm(VFS3, payload, 2.0, 501))["payload"] == payload


def test_nonfinite_or_wrong_shaped_audio_is_rejected():
    for audio in (np.array([np.nan]), np.array([np.inf]), np.zeros((4, 2))):
        result = VFS3.decode(audio)
        assert result["payload"] is None
        assert result["crc_ok"] is False
