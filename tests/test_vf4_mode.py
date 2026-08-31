"""Focused conformance tests for experimental Level-3 VHF FM mode VF4."""

import numpy as np
import pytest

from whale import afsk, framing, modes, rx_audio, waveform
from whale.modes import vf4
from whale.modes.vf4_mode import VF4


def test_vf4_capacity_and_mode_contract():
    assert isinstance(VF4, waveform.WaveformMode)
    assert VF4.mode_id == 8
    assert vf4.PAYLOAD_BITS == 43_848
    assert vf4.PACKET_BYTES == 5_481
    assert (vf4.RS_BLOCKS, vf4.RS_CODEWORD_BYTES, vf4.RS_DATA_BYTES) == (21, 254, 238)
    assert vf4.RS_ENCODED_BYTES == 5_334
    assert vf4.UNUSED_GRID_BYTES == 147
    assert vf4.RS_PACKET_BYTES == 4_998
    assert vf4.MAX_PAYLOAD_BYTES == 4_992
    assert VF4.chunk_size == 4_982


def test_gray_16qam_round_trips_all_labels_with_separable_slicer():
    labels = np.arange(16, dtype=np.uint8)
    bits = np.unpackbits(labels[:, None], axis=1)[:, 4:].reshape(-1)
    assert np.array_equal(vf4.bits_from_qam16(vf4.qam16_from_bits(bits)), bits)


def test_rs_only_grid_round_trips_full_capacity_and_corrects_byte_errors():
    rng = np.random.default_rng(20260831)
    payload = rng.integers(0, 256, vf4.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    grid = vf4.encode_payload_bits(payload)
    # One damaged byte in every byte-interleaved codeword is comfortably
    # inside RS(254,238)'s eight-byte correction radius.
    damaged = grid.copy()
    damaged[np.arange(vf4.RS_BLOCKS) * 8] ^= 1
    decoded, meta = vf4.decode_payload_bits(damaged)
    assert decoded == payload
    assert meta["rs_ok"] and meta["crc_ok"]


def test_rs_packet_rejects_an_impossible_declared_length():
    packet = ((vf4.MAX_PAYLOAD_BYTES + 1).to_bytes(2, "big")
              + bytes(vf4.RS_PACKET_BYTES - 2))
    codewords = b"".join(
        vf4._rs_encode_block(packet[at:at + vf4.RS_DATA_BYTES])
        for at in range(0, len(packet), vf4.RS_DATA_BYTES))
    interleaved = np.frombuffer(codewords, dtype=np.uint8).reshape(
        vf4.RS_BLOCKS, vf4.RS_CODEWORD_BYTES).T.reshape(-1)
    grid = np.concatenate((
        interleaved, np.zeros(vf4.UNUSED_GRID_BYTES, dtype=np.uint8)))
    encoded_bits = np.unpackbits(grid) ^ vf4._WHITENER
    decoded, meta = vf4.decode_payload_bits(encoded_bits)
    assert decoded is None
    assert meta["rs_ok"] and meta["failure"] == "invalid length"


def test_experimental_registry_appends_vf4_but_default_does_not():
    assert modes.default_registry().supported_ids == (0, 1, 2, 3)
    registry = modes.experimental_registry()
    assert 8 in registry.supported_ids
    assert registry.control is afsk.CONTROL_PROFILE


def test_clean_audio_frame_round_trips_through_production_rx_boundary():
    packet = bytes(range(251))
    capture = rx_audio.downsample(np.concatenate((
        np.zeros(3_000), VF4.encode(packet), np.zeros(3_000))))
    result = VF4.decode(capture)
    assert result["payload"] == packet
    assert result["end_index"] > result["sync_end_index"]


def test_oversize_packet_is_rejected():
    with pytest.raises(ValueError, match="carries at most"):
        VF4.encode(bytes(vf4.MAX_PAYLOAD_BYTES + 1))


def test_vf4_fixed_airtime_and_useful_rate_beats_vf3():
    from whale.modes.vf3_mode import VF3

    assert VF4.airtime(framing.AIR_HEADER_BYTES) == pytest.approx(5.2)
    vf4_rate = VF4.chunk_size * 8 / VF4.airtime(VF4.chunk_size)
    vf3_rate = VF3.chunk_size * 8 / VF3.airtime(VF3.chunk_size)
    assert vf4_rate > 6_000
    # Nominal frame-payload rate ratio at matched airtime; the qualifying
    # median-useful-throughput comparison is done by the Monte Carlo
    # campaign, not this nominal ratio, but the frame math alone already
    # clears the required >=25% margin many times over.
    assert vf4_rate > 1.25 * vf3_rate
