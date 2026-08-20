"""Software validation for VF3's 58-carrier frame."""

from pathlib import Path
import sys

import numpy as np
from scipy.signal import lfilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vf3


def _capture(audio, before=8_321, after=3_211):
    return np.pad(np.asarray(audio, dtype=np.float64), (before, after))


def _awgn(audio, snr_db, seed):
    rng = np.random.default_rng(seed)
    audio = np.asarray(audio, dtype=np.float64)
    active = audio[np.abs(audio) > 1e-8]
    rms = np.sqrt(np.mean(active ** 2))
    return audio + rng.normal(0.0, rms * 10 ** (-snr_db / 20.0), len(audio))


def test_geometry_and_capacity():
    assert vf3.CORE_SAMPLES == 1024 and vf3.GUARD_SAMPLES == 128
    assert vf3.SYMBOL_SAMPLES == 1152
    assert vf3.CARRIER_SPACING_HZ == 46.875
    assert len(vf3.CARRIER_BINS) == 58
    assert (vf3.CARRIER_HZ[0], vf3.CARRIER_HZ[-1]) == (468.75, 3140.625)
    assert vf3.PAYLOAD_BITS == 23_084
    assert vf3.FEC_INPUT_BITS == 11_542
    assert vf3.MAX_PAYLOAD_BYTES == 1_436
    assert vf3.FRAME_SAMPLES == 249_600 and vf3.FRAME_SECONDS == 5.2


def test_symbol_prefix_and_carrier_recovery():
    values = vf3.qpsk_from_bits(
        vf3._base._lfsr_bits(vf3.BITS_PER_SYMBOL, 201))
    symbol = vf3.build_symbol(values)
    assert len(symbol) == 1152
    assert np.allclose(symbol[:128], symbol[-128:], atol=1e-12)
    assert np.allclose(vf3.symbol_carriers(symbol, 0), values, atol=1e-12)
    assert np.allclose(vf3.symbol_carriers(symbol, 128), values, atol=1e-12)


def test_constellation_is_qpsk_on_every_carrier():
    values = vf3.frame_constellation(bytes(range(251)))
    assert values.shape == (214, 58)
    assert np.allclose(np.abs(values), 1.0)
    assert np.array_equal(values[:vf3.SYNC_SYMBOLS],
                          np.tile(values[0], (vf3.SYNC_SYMBOLS, 1)))
    assert not np.array_equal(values[5:], np.tile(values[0], (10, 1)))


def test_frame_shape_drive_and_tail():
    rng = np.random.default_rng(211)
    payload = rng.integers(
        0, 256, vf3.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    audio = vf3.modulate(payload)
    assert audio.dtype == np.float32 and len(audio) == 249_600
    assert np.max(np.abs(audio)) <= vf3.MAX_SAMPLE + 1e-7
    assert np.count_nonzero(audio[-vf3.TAIL_SAMPLES:]) == 0


def test_clean_round_trip_short_and_full():
    rng = np.random.default_rng(221)
    for size in (0, 43, vf3.MAX_PAYLOAD_BYTES):
        payload = rng.integers(0, 256, size, dtype=np.uint8).tobytes()
        result = vf3.demodulate(_capture(vf3.modulate(payload)))
        assert result["synced"] and result["crc_ok"]
        assert result["payload"] == payload


def test_awgn_and_dispersive_channel():
    rng = np.random.default_rng(231)
    payload = rng.integers(
        0, 256, vf3.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    channel = np.zeros(25)
    channel[7], channel[13], channel[24] = 1.0, 0.28, -0.12
    received = lfilter(channel, [1.0], vf3.modulate(payload).astype(float))
    result = vf3.demodulate_debug(
        _capture(_awgn(received, 24.0, 232)), payload)
    assert result["payload"] == payload
    assert result["crc_ok"]


def test_tracks_75ppm_soundcard_offset():
    rng = np.random.default_rng(241)
    payload = rng.integers(0, 256, 1_000, dtype=np.uint8).tobytes()
    audio = vf3.modulate(payload).astype(float)
    ppm = 75.0
    output_length = int(round(len(audio) * (1.0 + ppm * 1e-6)))
    positions = np.arange(output_length) / (1.0 + ppm * 1e-6)
    received = np.interp(positions, np.arange(len(audio)), audio,
                         left=0.0, right=0.0)
    result = vf3.demodulate_debug(
        _capture(_awgn(received, 27.0, 242)), payload)
    assert result["payload"] == payload
    assert abs(abs(result["clock_offset_ppm"]) - ppm) < 20.0


def test_noise_and_tone_do_not_decode():
    rng = np.random.default_rng(251)
    count = vf3.FRAME_SAMPLES + 10_000
    noise = rng.normal(0.0, 0.02, count)
    t = np.arange(count) / vf3.SAMPLE_RATE
    tone = 0.2 * np.sin(2 * np.pi * 1_000.0 * t)
    assert vf3.demodulate(noise)["payload"] is None
    assert vf3.demodulate(tone)["payload"] is None


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        print(f"{test.__name__} ...", end=" ", flush=True)
        test()
        print("ok")
    print(f"\n{len(tests)} VF3 tests passed")
