"""Software invariants and channel tests for the standalone VF2 modem."""

from pathlib import Path
import sys

import numpy as np
from scipy.signal import lfilter

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vf2


def _capture(audio, before=8_321, after=3_211):
    return np.pad(np.asarray(audio, dtype=np.float64), (before, after))


def _awgn(audio, snr_db, seed=1):
    rng = np.random.default_rng(seed)
    audio = np.asarray(audio, dtype=np.float64)
    active = audio[np.abs(audio) > 1e-8]
    rms = np.sqrt(np.mean(active ** 2))
    return audio + rng.normal(0.0, rms * 10 ** (-snr_db / 20.0), len(audio))


def test_published_geometry():
    assert vf2.SAMPLE_RATE == 48_000
    assert vf2.SYMBOL_SAMPLES == 1_152
    assert vf2.GUARD_SAMPLES == 128
    assert vf2.CORE_SAMPLES == 512
    assert vf2.CARRIER_SPACING_HZ == 93.75
    assert len(vf2.CARRIER_BINS) == 29
    assert (vf2.CARRIER_HZ[0], vf2.CARRIER_HZ[-1]) == (468.75, 3093.75)
    assert (vf2.HEADER_SYMBOLS, vf2.PAYLOAD_SYMBOLS, vf2.TOTAL_SYMBOLS) == (15, 199, 214)
    assert vf2.PAYLOAD_BITS == 11_542
    assert vf2.FRAME_SAMPLES == 249_600
    assert vf2.FRAME_SECONDS == 5.2


def test_symbol_is_one_512_periodic_waveform_and_recovers_every_carrier():
    bits = vf2._lfsr_bits(vf2.BITS_PER_SYMBOL, 123)
    values = vf2.qpsk_from_bits(bits)
    symbol = vf2.build_symbol(values)
    assert len(symbol) == 1_152
    assert np.allclose(symbol[:640], symbol[512:1_152], atol=1e-12)
    assert np.allclose(vf2.symbol_carriers(symbol, offset=0), values, atol=1e-12)
    assert np.allclose(vf2.symbol_carriers(symbol, offset=128), values, atol=1e-12)
    assert np.allclose(vf2.symbol_carriers(symbol, offset=640, combine=False),
                       values, atol=1e-12)
    try:
        vf2.symbol_carriers(symbol, offset=129, combine=True)
    except ValueError:
        pass
    else:
        raise AssertionError("combining beyond the two-core window must fail")


def test_all_header_and_payload_carriers_are_constant_modulus_qpsk():
    values = vf2.frame_constellation(bytes(range(251)))
    assert values.shape == (214, 29)
    assert np.allclose(np.abs(values), 1.0)
    assert np.array_equal(values[:vf2.SYNC_SYMBOLS],
                          np.tile(values[0], (vf2.SYNC_SYMBOLS, 1)))
    assert not np.array_equal(values[vf2.SYNC_SYMBOLS:],
                              np.tile(values[0],
                                      (vf2.HEADER_SYMBOLS - vf2.SYNC_SYMBOLS, 1)))
    assert np.all(np.isin(np.sign(values.real), (-1, 1)))
    assert np.all(np.isin(np.sign(values.imag), (-1, 1)))


def test_frame_duration_rms_and_payload_capacity():
    payload = bytes((i * 73) & 0xFF for i in range(vf2.MAX_PAYLOAD_BYTES))
    audio = vf2.modulate(payload)
    assert audio.dtype == np.float32
    assert len(audio) == 249_600
    assert np.max(np.abs(audio)) < 1.0
    body_at = vf2.LEAD_IN_SAMPLES
    first = audio[body_at:body_at + vf2.SYMBOL_SAMPLES]
    first_core = first[vf2.GUARD_SAMPLES:vf2.GUARD_SAMPLES + vf2.CORE_SAMPLES]
    assert abs(np.sqrt(np.mean(first_core ** 2)) - vf2.TX_RMS) < 1e-6
    assert np.count_nonzero(audio[-vf2.TAIL_SAMPLES:]) == 0


def test_clean_round_trip_short_and_full_payload():
    rng = np.random.default_rng(20260820)
    for size in (0, 37, vf2.MAX_PAYLOAD_BYTES):
        payload = rng.integers(0, 256, size, dtype=np.uint8).tobytes()
        result = vf2.demodulate(_capture(vf2.modulate(payload)))
        assert result["synced"]
        assert result["crc_ok"]
        assert result["payload"] == payload
        assert result["confidence"] > 0.99


def test_awgn_and_short_audio_filter_round_trip():
    rng = np.random.default_rng(51)
    payload = rng.integers(0, 256, vf2.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    audio = vf2.modulate(payload).astype(np.float64)
    # A short, non-flat LTI audio path plus delay; it changes every carrier's
    # complex gain while remaining comfortably inside the repeated core.
    channel = np.zeros(25)
    channel[7], channel[13], channel[24] = 1.0, 0.28, -0.12
    received = lfilter(channel, [1.0], audio)
    received = _awgn(received, 22.0, seed=52)
    result = vf2.demodulate_debug(_capture(received), payload)
    assert result["payload"] == payload
    assert result["total_bit_errors"] == 0


def test_tracks_independent_soundcard_clock_offset():
    rng = np.random.default_rng(61)
    payload = rng.integers(0, 256, 600, dtype=np.uint8).tobytes()
    audio = vf2.modulate(payload).astype(np.float64)
    ppm = 75.0
    output_length = int(round(len(audio) * (1.0 + ppm * 1e-6)))
    source_positions = np.arange(output_length) / (1.0 + ppm * 1e-6)
    received = np.interp(source_positions, np.arange(len(audio)), audio,
                         left=0.0, right=0.0)
    result = vf2.demodulate_debug(_capture(_awgn(received, 26.0, seed=62)), payload)
    assert result["payload"] == payload
    assert result["total_bit_errors"] == 0
    assert abs(abs(result["clock_offset_ppm"]) - ppm) < 15.0


def test_noise_and_tone_do_not_produce_a_frame():
    rng = np.random.default_rng(71)
    noise = rng.normal(0.0, 0.02, vf2.FRAME_SAMPLES + 10_000)
    tone_t = np.arange(vf2.FRAME_SAMPLES + 10_000) / vf2.SAMPLE_RATE
    tone = 0.2 * np.sin(2.0 * np.pi * 1_000.0 * tone_t)
    assert vf2.demodulate(noise)["payload"] is None
    tone_result = vf2.demodulate(tone)
    assert tone_result["payload"] is None
    assert tone_result.get("present_carriers", 0) < vf2.MIN_PRESENT_CARRIERS


def test_crc_rejects_payload_bit_error():
    payload = b"CRC must cover the delivered user payload"
    bits = vf2.encode_payload_bits(payload)
    decoded, meta = vf2.decode_payload_bits(bits)
    assert decoded == payload and meta["crc_ok"]
    coded = np.empty(vf2.PAYLOAD_BITS, dtype=np.uint8)
    coded[vf2._INTERLEAVER] = bits
    information = vf2.convolutional_decode(coded)
    # Re-encode a wrong but perfectly valid codeword, proving that CRC32 is
    # still the final integrity check after a successful trellis decode.
    information[137] ^= 1
    damaged = vf2.convolutional_encode(information)[vf2._INTERLEAVER]
    decoded, meta = vf2.decode_payload_bits(damaged)
    assert decoded is None and not meta["crc_ok"]


def test_convolutional_code_corrects_scattered_errors():
    rng = np.random.default_rng(141)
    information = rng.integers(0, 2, vf2.FEC_INPUT_BITS, dtype=np.uint8)
    information[-vf2.FEC_TAIL_BITS:] = 0
    coded = vf2.convolutional_encode(information)
    damaged = coded.copy()
    damaged[rng.choice(len(damaged), 80, replace=False)] ^= 1
    assert np.array_equal(vf2.convolutional_decode(damaged), information)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        print(f"{test.__name__} ...", end=" ", flush=True)
        test()
        print("ok")
    print(f"\n{len(tests)} VF2 tests passed")
