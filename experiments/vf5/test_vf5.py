"""Software validation for VF5's 58-carrier square-16-QAM frame."""

from pathlib import Path
import sys

import numpy as np
from scipy.signal import lfilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vf5


def _capture(audio, before=8_321, after=3_211):
    return np.pad(np.asarray(audio, dtype=np.float64), (before, after))


def _awgn(audio, snr_db, seed):
    rng = np.random.default_rng(seed)
    audio = np.asarray(audio, dtype=np.float64)
    active = audio[np.abs(audio) > 1e-8]
    rms = np.sqrt(np.mean(active ** 2))
    return audio + rng.normal(0.0, rms * 10 ** (-snr_db / 20.0), len(audio))


def test_geometry_and_capacity():
    assert vf5.CORE_SAMPLES == 1024 and vf5.GUARD_SAMPLES == 128
    assert vf5.SYMBOL_SAMPLES == 1152
    assert vf5.CARRIER_SPACING_HZ == 46.875
    assert len(vf5.CARRIER_BINS) == 58
    assert (vf5.CARRIER_HZ[0], vf5.CARRIER_HZ[-1]) == (468.75, 3140.625)
    assert vf5.BITS_PER_SYMBOL == 232
    assert vf5.PILOT_SYMBOLS == 10 and vf5.DATA_SYMBOLS == 189
    assert vf5.PAYLOAD_BITS == 43_848
    assert vf5.FEC_INPUT_BITS == 21_924
    assert vf5.PACKET_BYTES == 2_739 and vf5.UNUSED_INFO_BITS == 6
    assert vf5.RS_CODEWORD_BYTES == 249 and vf5.RS_DATA_BYTES == 217
    assert vf5.RS_PACKET_BYTES == 2_387
    assert vf5.MAX_PAYLOAD_BYTES == 2_381
    assert vf5.FRAME_SAMPLES == 249_600 and vf5.FRAME_SECONDS == 5.2


def test_square_16qam_mapping_and_geometry():
    labels = np.unpackbits(
        np.arange(16, dtype=np.uint8)[:, None], axis=1)[:, -4:]
    points = vf5.qam16_from_bits(labels)
    assert len(np.unique(points)) == 16
    assert np.array_equal(vf5.bits_from_qam16(points), labels.reshape(-1))
    levels = np.array([-3.0, -1.0, 1.0, 3.0]) / np.sqrt(10.0)
    assert np.allclose(np.unique(points.real), levels)
    assert np.allclose(np.unique(points.imag), levels)
    assert np.isclose(np.mean(np.abs(points) ** 2), 1.0)


def test_payload_pilots_remove_linear_phase_drift():
    rng = np.random.default_rng(202)
    payload = rng.integers(
        0, 256, vf5.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    bits = vf5.encode_payload_bits(payload)
    values = vf5.frame_constellation(payload)[vf5.HEADER_SYMBOLS:]
    offset = np.linspace(-0.7, 0.8, vf5.N_CARRIERS)
    slope = np.linspace(-0.009, 0.011, vf5.N_CARRIERS)
    positions = np.arange(vf5.PAYLOAD_SYMBOLS)[:, None]
    phase = offset[None, :] + positions * slope[None, :]
    initial = vf5.HEADER_VALUES[-1] * np.exp(1j * (offset - slope))
    corrected, estimated = vf5.pilot_phase_correct(
        values * np.exp(1j * phase), initial)
    decoded = vf5.qam16_hard_bits(corrected[vf5.DATA_SYMBOL_INDICES])
    assert np.allclose(estimated, phase, atol=1e-12)
    assert np.array_equal(decoded, bits)


def test_shortened_reed_solomon_repairs_sixteen_byte_errors():
    rng = np.random.default_rng(203)
    data = rng.integers(0, 256, vf5.RS_DATA_BYTES, dtype=np.uint8).tobytes()
    encoded = vf5._rs_encode_block(data)
    assert len(encoded) == vf5.RS_CODEWORD_BYTES
    for error_count in (0, 1, 12, 16):
        damaged = bytearray(encoded)
        positions = rng.choice(len(damaged), error_count, replace=False)
        for position in positions:
            damaged[position] ^= int(rng.integers(1, 256))
        decoded, corrected = vf5._rs_decode_block(bytes(damaged))
        assert decoded == data and corrected == error_count


def test_outer_interleaver_spreads_eighty_consecutive_error_bytes():
    rng = np.random.default_rng(204)
    payload = rng.integers(
        0, 256, vf5.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    transmitted = vf5.encode_payload_bits(payload)
    coded = np.empty(vf5.PAYLOAD_BITS, dtype=np.uint8)
    coded[vf5._INTERLEAVER] = transmitted
    information = vf5.convolutional_decode(coded)
    packet_bits = information[:vf5.PACKET_BYTES * 8].reshape(-1, 8)
    packet_bits[1_000:1_080, 0] ^= 1
    decoded, meta = vf5._decode_information(information)
    assert decoded == payload
    assert meta["rs_ok"] and meta["rs_corrected_bytes"] == 80


def test_symbol_prefix_and_carrier_recovery():
    values = vf5.qam16_from_bits(
        vf5._base._lfsr_bits(vf5.BITS_PER_SYMBOL, 201))
    symbol = vf5.build_symbol(values)
    assert len(symbol) == 1152
    assert np.allclose(symbol[:128], symbol[-128:], atol=1e-12)
    assert np.allclose(vf5.symbol_carriers(symbol, 0), values, atol=1e-12)
    assert np.allclose(vf5.symbol_carriers(symbol, 128), values, atol=1e-12)


def test_constellation_is_square_16qam_on_every_carrier():
    values = vf5.frame_constellation(bytes(range(251)))
    assert values.shape == (214, 58)
    assert np.all(np.min(
        np.abs(values[..., None] - vf5._QAM16_POINTS), axis=-1) < 1e-12)
    assert np.array_equal(values[:vf5.SYNC_SYMBOLS],
                          np.tile(values[0], (vf5.SYNC_SYMBOLS, 1)))
    assert not np.array_equal(values[5:], np.tile(values[0], (10, 1)))


def test_frame_shape_drive_and_tail():
    rng = np.random.default_rng(211)
    payload = rng.integers(
        0, 256, vf5.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    audio = vf5.modulate(payload)
    assert audio.dtype == np.float32 and len(audio) == 249_600
    assert np.max(np.abs(audio)) <= vf5.MAX_SAMPLE + 1e-7
    assert np.count_nonzero(audio[-vf5.TAIL_SAMPLES:]) == 0


def test_clean_round_trip_short_and_full():
    rng = np.random.default_rng(221)
    for size in (0, 43, vf5.MAX_PAYLOAD_BYTES):
        payload = rng.integers(0, 256, size, dtype=np.uint8).tobytes()
        result = vf5.demodulate(_capture(vf5.modulate(payload)))
        assert result["synced"] and result["crc_ok"]
        assert result["payload"] == payload


def test_awgn_and_dispersive_channel():
    rng = np.random.default_rng(231)
    payload = rng.integers(
        0, 256, vf5.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
    channel = np.zeros(25)
    channel[7], channel[13], channel[24] = 1.0, 0.28, -0.12
    received = lfilter(channel, [1.0], vf5.modulate(payload).astype(float))
    result = vf5.demodulate_debug(
        _capture(_awgn(received, 31.0, 232)), payload)
    assert result["payload"] == payload
    assert result["crc_ok"]


def test_tracks_75ppm_soundcard_offset():
    rng = np.random.default_rng(241)
    payload = rng.integers(0, 256, 1_000, dtype=np.uint8).tobytes()
    audio = vf5.modulate(payload).astype(float)
    ppm = 75.0
    output_length = int(round(len(audio) * (1.0 + ppm * 1e-6)))
    positions = np.arange(output_length) / (1.0 + ppm * 1e-6)
    received = np.interp(positions, np.arange(len(audio)), audio,
                         left=0.0, right=0.0)
    result = vf5.demodulate_debug(
        _capture(_awgn(received, 33.0, 242)), payload)
    assert result["payload"] == payload
    assert abs(abs(result["clock_offset_ppm"]) - ppm) < 20.0


def test_noise_and_tone_do_not_decode():
    rng = np.random.default_rng(251)
    count = vf5.FRAME_SAMPLES + 10_000
    noise = rng.normal(0.0, 0.02, count)
    t = np.arange(count) / vf5.SAMPLE_RATE
    tone = 0.2 * np.sin(2 * np.pi * 1_000.0 * t)
    assert vf5.demodulate(noise)["payload"] is None
    assert vf5.demodulate(tone)["payload"] is None


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        print(f"{test.__name__} ...", end=" ", flush=True)
        test()
        print("ok")
    print(f"\n{len(tests)} VF5 tests passed")
