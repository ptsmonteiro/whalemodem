"""Pure-software checks for framing + afsk -- no hardware, no radios.

Run: python tests/test_afsk_loopback.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from whale import afsk, framing


def test_framing_roundtrip():
    for payload in (b"", b"hello", bytes(range(256))[:255]):
        bits = framing.build_frame_bits(payload)
        decoded = framing.parse_frame_bits(bits[len(framing.SYNC_BITS):])
        assert decoded == payload, (payload, decoded)
    print("test_framing_roundtrip OK")


def test_afsk_clean_loopback():
    rng = np.random.default_rng(0)
    payload = bytes(rng.integers(0, 256, size=37, dtype=np.uint8))
    tx = afsk.modulate(payload)
    result = afsk.demodulate(tx)
    assert result["synced"], result
    assert result["payload"] == payload, (result["payload"], payload)
    print("test_afsk_clean_loopback OK")


def test_afsk_noisy_delayed_loopback():
    rng = np.random.default_rng(1)
    payload = bytes(rng.integers(0, 256, size=200, dtype=np.uint8))
    tx = afsk.modulate(payload)
    lead = np.zeros(int(rng.integers(0, 4000)))
    tail = np.zeros(2000)
    gain = 0.3
    noisy = np.concatenate([lead, tx, tail]) * gain
    noisy = noisy + rng.normal(0, 0.02, size=len(noisy))
    result = afsk.demodulate(noisy)
    assert result["synced"], result
    assert result["payload"] == payload, "payload mismatch"
    print("test_afsk_noisy_delayed_loopback OK")


def test_link_packet_roundtrip():
    """Same shape as whale.link's packet encode: type byte + body, through
    modulate/demodulate."""
    from whale.link import PT_DATA, EOF_BIT

    body = bytes([0x00 | EOF_BIT]) + b"x" * 200
    payload = bytes([PT_DATA]) + body
    tx = afsk.modulate(payload)
    result = afsk.demodulate(tx)
    assert result["payload"] == payload
    print("test_link_packet_roundtrip OK")


if __name__ == "__main__":
    test_framing_roundtrip()
    test_afsk_clean_loopback()
    test_afsk_noisy_delayed_loopback()
    test_link_packet_roundtrip()
    print("all tests OK")
