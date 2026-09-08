"""HF6 OFDM codec contracts; no hardware required."""

import numpy as np

from whale import framing, rx_audio
from whale.phy.sc import bits_to_symbols, symbols_to_bits
from whale.modes.hf6_mode import HF6


def test_hf6_clean_loopback_and_throughput():
    payload = bytes((i * 47 + 11) & 0xFF
                    for i in range(HF6.chunk_size + framing.AIR_HEADER_BYTES))
    tx = HF6.encode(payload)
    captured = rx_audio.downsample(np.concatenate((
        tx, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))
    result = HF6.decode(captured)
    assert result["payload"] == payload
    assert result["crc_ok"]
    assert HF6.airtime(len(payload)) < 0.4
    assert 8 * HF6.chunk_size / HF6.airtime(len(payload)) > 4_000


def test_hf6_rejects_oversize_payload():
    try:
        HF6.encode(bytes(HF6.chunk_size + framing.AIR_HEADER_BYTES + 1))
    except ValueError:
        pass
    else:
        raise AssertionError("oversize HF6 payload was accepted")


def test_64qam_mapping_is_gray_and_invertible():
    labels = np.unpackbits(np.arange(8, dtype=np.uint8)[:, None], axis=1)[:, -3:]
    points = bits_to_symbols(np.tile(labels, (1, 2)).reshape(-1), 6)
    assert np.array_equal(symbols_to_bits(points, 6), np.tile(labels, (1, 2)).reshape(-1))
    axis = np.unique(np.round(points.real * np.sqrt(42.0), 6))
    assert np.array_equal(axis, np.array([-7., -5., -3., -1., 1., 3., 5., 7.]))
