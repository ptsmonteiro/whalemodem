import binascii

import numpy as np

import hr0


def test_geometry_meets_the_bounded_screen_requirements():
    geometry = hr0.describe()
    assert geometry["occupied_edges_hz"][1] <= 2300
    assert geometry["cp_ms"] >= 2.0
    assert 7.9 <= geometry["frame_seconds"] <= 8.1
    assert geometry["max_payload_bytes"] == 53
    assert geometry["packet_bytes"] == 58
    assert geometry["ldpc_blocks"] == 2
    assert geometry["omitted_filler_bits_per_block"] == 92
    assert geometry["repetition_min"] >= 8


def test_clean_round_trip_at_full_capacity():
    payload = bytes(range(hr0.MAX_PAYLOAD_BYTES))
    decoded = hr0.demodulate(hr0.modulate(payload))
    assert decoded["payload"] == payload
    assert decoded["crc_ok"]
    assert decoded["ldpc_ok"] == [True, True]


def test_packet_crc_rejects_corruption_and_length_is_bounded():
    packet = bytearray(hr0.build_packet(b"short"))
    packet[7] ^= 1
    assert hr0.parse_packet(packet) is None
    body = bytes((54,)) + bytes(hr0.MAX_PAYLOAD_BYTES)
    invalid = body + binascii.crc32(body).to_bytes(4, "big")
    assert hr0.parse_packet(invalid) is None


def test_shortening_omits_known_systematic_filler():
    bits = hr0.encode_code_bits(np.arange(53, dtype=np.uint8).tobytes())
    assert bits.shape == (hr0.TX_CODE_BITS,)
    assert hr0.TX_CODE_BITS == 2 * (232 + 324)
