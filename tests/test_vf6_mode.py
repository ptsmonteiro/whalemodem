"""Focused conformance tests for experimental top-rung VF6."""

import numpy as np
import pytest

from whale import afsk, framing, modes, rx_audio, waveform
from whale.modes import vf6
from whale.modes.vf6_mode import VF6


def test_vf6_capacity_and_mode_contract():
    assert isinstance(VF6, waveform.WaveformMode)
    assert VF6.mode_id == 6
    assert vf6.PAYLOAD_BITS == 87_696
    assert vf6.PACKET_BYTES == 10_962
    assert (vf6.RS_BLOCKS, vf6.RS_CODEWORD_BYTES, vf6.RS_DATA_BYTES) == (43, 254, 238)
    assert vf6.RS_ENCODED_BYTES == 10_922
    assert vf6.UNUSED_GRID_BYTES == 40
    assert vf6.RS_PACKET_BYTES == 10_234
    assert vf6.MAX_PAYLOAD_BYTES == 10_228
    assert VF6.chunk_size == 10_218


def test_gray_256qam_round_trips_all_labels_with_separable_slicer():
    labels = np.arange(256, dtype=np.uint8)
    bits = np.unpackbits(labels[:, None], axis=1).reshape(-1)
    assert np.array_equal(vf6.bits_from_qam256(vf6.qam256_from_bits(bits)), bits)


def test_rs_only_grid_round_trips_full_capacity_and_corrects_byte_errors():
    rng = np.random.default_rng(20260831)
    payload = rng.integers(0, 256, vf6.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    grid = vf6.encode_payload_bits(payload)
    # One damaged byte in every byte-interleaved codeword is comfortably
    # inside RS(254,238)'s eight-byte correction radius.
    damaged = grid.copy()
    damaged[np.arange(vf6.RS_BLOCKS) * 8] ^= 1
    decoded, meta = vf6.decode_payload_bits(damaged)
    assert decoded == payload
    assert meta["rs_ok"] and meta["crc_ok"]


def test_rs_packet_rejects_an_impossible_declared_length():
    packet = ((vf6.MAX_PAYLOAD_BYTES + 1).to_bytes(2, "big")
              + bytes(vf6.RS_PACKET_BYTES - 2))
    codewords = b"".join(
        vf6._rs_encode_block(packet[at:at + vf6.RS_DATA_BYTES])
        for at in range(0, len(packet), vf6.RS_DATA_BYTES))
    interleaved = np.frombuffer(codewords, dtype=np.uint8).reshape(
        vf6.RS_BLOCKS, vf6.RS_CODEWORD_BYTES).T.reshape(-1)
    grid = np.concatenate((
        interleaved, np.zeros(vf6.UNUSED_GRID_BYTES, dtype=np.uint8)))
    encoded_bits = np.unpackbits(grid) ^ vf6._WHITENER
    decoded, meta = vf6.decode_payload_bits(encoded_bits)
    assert decoded is None
    assert meta["rs_ok"] and meta["failure"] == "invalid length"


def test_experimental_registry_appends_vf6_but_default_does_not():
    assert modes.default_registry().supported_ids == (0, 1, 2, 3)
    registry = modes.experimental_registry()
    assert registry.supported_ids == (0, 1, 2, 3, 6)
    assert registry.control is afsk.CONTROL_PROFILE


def test_clean_audio_frame_round_trips_through_production_rx_boundary():
    packet = bytes(range(251))
    capture = rx_audio.downsample(np.concatenate((
        np.zeros(3_000), VF6.encode(packet), np.zeros(3_000))))
    result = VF6.decode(capture)
    assert result["payload"] == packet
    assert result["end_index"] > result["sync_end_index"]


def test_oversize_packet_is_rejected():
    with pytest.raises(ValueError, match="carries at most"):
        VF6.encode(bytes(vf6.MAX_PAYLOAD_BYTES + 1))


def test_vf6_fixed_airtime_and_top_rate():
    assert VF6.airtime(framing.AIR_HEADER_BYTES) == pytest.approx(5.2)
    assert VF6.chunk_size * 8 / VF6.airtime(VF6.chunk_size) > 15_000
