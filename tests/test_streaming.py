"""Live waveform feeding contracts."""

import numpy as np

from whale import afsk, framing, rx_audio
from whale.streaming import decoder_for


def test_streaming_decoder_feeds_chunks_and_decodes_once_at_frame_end():
    payload = bytes(range(framing.AIR_HEADER_BYTES + 8))
    tx = afsk.PROFILE_300.encode(payload)
    captured = rx_audio.downsample(np.concatenate((
        np.zeros(500, dtype=np.float32), tx,
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))
    decoder = decoder_for(afsk.PROFILE_300)

    results = []
    for start in range(0, len(captured), 1_000):
        result = decoder.feed(captured[start:start + 1_000])
        if result is not None:
            results.append(result)

    assert len(results) == 1
    assert results[0]["payload"] == payload
    assert decoder.decode_count < len(captured) // 1_000
    assert results[0]["end_index"] > results[0]["start_index"]


def test_streaming_decoder_reset_starts_a_new_stream():
    decoder = decoder_for(afsk.PROFILE_300)
    decoder.feed(np.zeros(10_000, dtype=np.float32))
    assert decoder.decode_count > 0
    decoder.reset()
    assert decoder.decode_count == 0
    assert not decoder.locked
