"""Software invariants and end-to-end checks for qpsk29.

Run directly; the repository's pytest configuration intentionally collects
only tests/.
"""

from pathlib import Path
import sys

import numpy as np
from scipy.signal import hilbert

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ldpc  # noqa: E402
import qpsk29 as q  # noqa: E402


def test_symbol_is_periodic_and_invertible():
    values = q.header_values()[3]
    symbol = q.build_symbol(values)
    core = symbol[q.GUARD_SAMPLES:q.GUARD_SAMPLES + q.CORE_SAMPLES]
    assert np.allclose(symbol, np.resize(np.roll(core, q.GUARD_SAMPLES),
                                         q.SYMBOL_SAMPLES), atol=1e-12)
    assert np.allclose(q.symbol_carriers(symbol), values, atol=1e-12)
    assert np.allclose(q.symbol_carriers(symbol, 300, combine=False), values,
                       atol=1e-12)
    try:
        q.symbol_carriers(symbol, q.COMBINE_MAX_OFFSET + 1, combine=True)
    except ValueError:
        pass
    else:
        raise AssertionError("combined FFT past the second core did not fail")


def test_interleaver_scatter_and_pilot_pairs():
    mapping = q.interleave_map()
    assert np.array_equal(np.sort(mapping), np.arange(q.PAYLOAD_BITS))
    assert np.all(mapping[1::2] == mapping[0::2] + 1)
    pilot = q.pilot_positions()
    assert len(pilot) == q.PAD_BITS
    assert np.all(pilot[1::2] == pilot[0::2] + 1)
    pilot_qpsk = pilot[0::2] // 2
    pilot_rows = pilot_qpsk // q.N_CARRIERS
    pilot_carriers = pilot_qpsk % q.N_CARRIERS
    for carrier in range(q.N_CARRIERS):
        rows = pilot_rows[pilot_carriers == carrier]
        assert len(rows) >= 8
        assert rows[0] < q.PAYLOAD_SYMBOLS // 4
        assert rows[-1] >= 3 * q.PAYLOAD_SYMBOLS // 4
    owners = q.codeword_of_grid_bit().reshape(q.PAYLOAD_SYMBOLS,
                                               q.BITS_PER_SYMBOL)
    for codeword in range(q.CODEWORDS):
        rows = np.flatnonzero(np.any(owners == codeword, axis=1))
        assert len(rows) > q.PAYLOAD_SYMBOLS * 0.7


def test_noise_does_not_sync():
    noise = np.random.default_rng(3).normal(
        0.0, 0.1, q.FRAME_SAMPLES + q.SAMPLE_RATE)
    decoded = q.demodulate(noise)
    assert not decoded["synced"]
    assert decoded["payload"] is None


def test_batch_ldpc_matches_scalar():
    rng = np.random.default_rng(7)
    rate = "2/3"
    information = rng.integers(0, 2, (5, ldpc.INFORMATION_BITS[rate]),
                               dtype=np.uint8)
    coded = np.vstack([ldpc.encode(row, rate) for row in information])
    llr = (1.0 - 2.0 * coded) * 2.0 + rng.normal(0.0, 1.0, coded.shape)
    batch = ldpc.decode_batch(llr, rate=rate)
    scalar = [ldpc.decode(row, rate=rate) for row in llr]
    assert np.array_equal(batch[0], np.vstack([item[0] for item in scalar]))
    assert np.array_equal(batch[1], [item[1] for item in scalar])
    assert np.array_equal(batch[2], [item[2] for item in scalar])


def test_clean_round_trip_all_rates():
    rng = np.random.default_rng(11)
    for fec in ("1/2", "2/3", "3/4", None):
        profile = q.Qpsk29Profile(fec=fec)
        payload = rng.integers(0, 256, profile.max_payload,
                               dtype=np.uint8).tobytes()
        audio = q.modulate(payload, profile)
        assert len(audio) == q.FRAME_SAMPLES
        decoded = q.demodulate_debug(audio, profile, payload)
        assert decoded["payload"] == payload, decoded.get("failure")
        assert decoded["total_bit_errors"] == 0


def test_hf_offset_noise_and_capture_lead():
    rng = np.random.default_rng(29)
    profile = q.Qpsk29Profile(fec="2/3")
    payload = rng.integers(0, 256, profile.max_payload,
                           dtype=np.uint8).tobytes()
    audio = q.modulate(payload, profile)
    offset_hz = -8.25
    n = np.arange(len(audio))
    shifted = np.real(hilbert(audio) * np.exp(
        2j * np.pi * offset_hz * n / q.SAMPLE_RATE))
    rms = np.sqrt(np.mean(shifted ** 2))
    capture = np.concatenate((np.zeros(4_800), shifted, np.zeros(2_400)))
    capture += rng.normal(0.0, rms / 10 ** (16.0 / 20.0), len(capture))
    decoded = q.demodulate(capture, profile)
    assert decoded["payload"] == payload, decoded.get("failure")
    assert abs(decoded["cfo_hz"] - offset_hz) < 0.2


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} qpsk29 tests passed")


if __name__ == "__main__":
    main()
