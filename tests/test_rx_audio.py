"""The one shared 48 kHz -> 12 kHz receive front end."""

import numpy as np

from whale import rx_audio


def test_chunking_does_not_change_the_filtered_stream():
    rng = np.random.default_rng(20260828)
    audio = rng.normal(size=48_037).astype(np.float32)
    expected = rx_audio.downsample(audio)

    converter = rx_audio.ReceiveDecimator()
    chunks = []
    offset = 0
    for size in (1, 17, 480, 3, 1024, 799, 4096, 11_003, 30_000):
        if offset >= len(audio):
            break
        chunks.append(converter.process(audio[offset:offset + size]))
        offset += size
    if offset < len(audio):
        chunks.append(converter.process(audio[offset:]))

    assert np.array_equal(np.concatenate(chunks), expected)


def test_filter_rejects_a_tone_that_would_alias_into_the_modem_band():
    n = np.arange(rx_audio.CAPTURE_SAMPLE_RATE)
    wanted = np.sin(2 * np.pi * 3_000 * n / rx_audio.CAPTURE_SAMPLE_RATE)
    alias = np.sin(2 * np.pi * 9_000 * n / rx_audio.CAPTURE_SAMPLE_RATE)

    wanted_rx = rx_audio.downsample(wanted)
    alias_rx = rx_audio.downsample(alias)
    edge = 2 * rx_audio.FILTER_DELAY_DECODE_SAMPLES
    wanted_rms = np.sqrt(np.mean(wanted_rx[edge:-edge] ** 2))
    alias_rms = np.sqrt(np.mean(alias_rx[edge:-edge] ** 2))

    assert wanted_rms > 0.65
    assert alias_rms < wanted_rms / 100


def test_output_count_tracks_the_exact_four_to_one_clock_ratio():
    for count in (1, 4, 5, 47_999, 48_000, 48_003):
        assert len(rx_audio.downsample(np.zeros(count))) == (count + 3) // 4
