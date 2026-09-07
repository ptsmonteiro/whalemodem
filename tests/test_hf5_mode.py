"""HF5 resilient single-carrier codec contracts."""

import numpy as np

from whale import framing, rx_audio
from whale.modes.hf5_mode import HF5


def test_hf5_clean_loopback_and_capacity():
    payload = bytes((i * 29 + 7) & 0xFF for i in range(HF5.chunk_size + framing.AIR_HEADER_BYTES))
    tx = HF5.encode(payload)
    captured = rx_audio.downsample(np.concatenate((
        tx, np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))
    result = HF5.decode(captured)
    assert result["payload"] == payload
    assert result["crc_ok"]
    assert HF5.airtime(len(payload)) < 6.0
    assert 8 * HF5.chunk_size / HF5.airtime(len(payload)) > 2_000


def test_hf5_rejects_oversize_payload():
    try:
        HF5.encode(bytes(HF5.chunk_size + framing.AIR_HEADER_BYTES + 1))
    except ValueError:
        pass
    else:
        raise AssertionError("oversize HF5 payload was accepted")
