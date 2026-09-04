"""Deterministic point-3 checks for the standalone HR1-A experiment."""

import hashlib

import numpy as np
import pytest
from scipy.signal import hilbert, resample_poly

from experiments.hr1 import benchmark, hr1
from whale import rx_audio, waveform


def _capture(tx, prefix=0):
    return rx_audio.downsample(np.concatenate((
        np.zeros(prefix, np.float32), np.asarray(tx, np.float32),
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, np.float32),
    )))


@pytest.mark.parametrize("length", [0, 1, 12, 26, 63, 64])
def test_payload_sizes_round_trip_through_the_aligned_checked_path(length):
    payload = bytes((37 * at + length) & 0xFF for at in range(length))
    captured = _capture(hr1.HR1.encode(payload))
    result = hr1.decode_aligned(
        captured,
        preamble_start=(hr1.LEAD_RX_SAMPLES
                        + rx_audio.FILTER_DELAY_DECODE_SAMPLES),
    )
    assert result["payload"] == payload
    assert result["decoded_length"] == length
    assert result["crc_ok"] and result["fec_tail_ok"]
    assert result["zero_fill_ok"]


def test_mode_surface_geometry_airtime_and_oversize_rejection():
    assert isinstance(hr1.HR1, waveform.WaveformMode)
    assert hr1.HR1.chunk_size == 54
    assert hr1.HR1.tx_sample_rate == 48_000
    assert hr1.HR1.rx_sample_rate == 12_000
    assert hr1.HR1.airtime(1) == pytest.approx(12.604)
    with pytest.raises(ValueError, match="maximum"):
        hr1.HR1.encode(bytes(65))


def test_rate_third_k7_vector_and_terminated_round_trip():
    inputs = np.asarray([1, 0, 1, 1, 0, 0, 1, 0], dtype=np.uint8)
    expected = np.asarray([int(bit) for bit in
                           "111010110011100010001000"], dtype=np.uint8)
    assert np.array_equal(hr1.CODE.encode(inputs), expected)

    terminated = np.concatenate((inputs, np.zeros(8, dtype=np.uint8)))
    coded = hr1.CODE.encode(terminated)
    decoded, work = hr1.CODE.decode_hard(coded)
    assert np.array_equal(decoded, terminated)
    assert work["viterbi_branch_metrics"] == len(terminated) * 64 * 2


def test_coded_wire_vector_is_frozen_and_deterministic():
    payload = bytes(range(64))
    first = hr1.encode_coded_bits(payload)
    second = hr1.encode_coded_bits(payload)
    assert np.array_equal(first, second)
    assert hashlib.sha256(first.tobytes()).hexdigest() == (
        "35e19188ca5644c30428294d054ac482e7a057deac66c0fdceff7674fa431202")


def test_interleaver_class_words_hopping_and_pilot_lattice_invariants():
    assert hr1.INTERLEAVER.is_valid()
    assert np.array_equal(hr1.INTERLEAVER.gather(
        hr1.INTERLEAVER.spread(np.arange(hr1.CODED_BITS))),
        np.arange(hr1.CODED_BITS))
    for word in hr1.CLASS_WORDS:
        blocks = word.reshape(5, 16)
        assert all(np.array_equal(np.sort(block), np.arange(16))
                   for block in blocks)
    assert len(np.unique(hr1.CLASS_WORDS, axis=0)) == 3
    assert hr1.HOP_MASKS[:16].tolist() == [
        12, 11, 6, 5, 4, 8, 2, 7, 8, 4, 8, 13, 13, 0, 7, 1]

    body = hr1.body_tones(bytes(range(64)))
    assert len(body) == 439
    pilot_positions = []
    body_at = 0
    for data_at in range(1, hr1.DATA_SYMBOLS + 1):
        body_at += 1
        if data_at % 31 == 0 and data_at != hr1.DATA_SYMBOLS:
            pilot_positions.append(body_at)
            body_at += 1
    assert body[pilot_positions].tolist() == list(range(13))


def test_real_acquisition_handles_leading_timing_cfo_and_clock_error():
    payload = bytes(range(64))
    tx = hr1.HR1.encode(payload).astype(np.float64)
    index = np.arange(len(tx))
    shifted = np.real(hilbert(tx) * np.exp(2j * np.pi * 87.0 * index / 48_000))
    # +100 ppm also demonstrates that the fixed timing remains within one
    # observation over the first-pass 12.6-second frame.
    shifted = resample_poly(shifted, 10_001, 10_000)
    captured = _capture(shifted, prefix=731)
    result = hr1.HR1.decode(captured)
    assert result["payload"] == payload
    assert result["candidate_rank"] <= hr1.MAX_CANDIDATES
    assert result["candidates_tried"] <= hr1.MAX_CANDIDATES
    assert result["cfo_hz"] == pytest.approx(87.0, abs=8.0)
    assert abs(result["start_index"]
               - (hr1.LEAD_RX_SAMPLES + 731 / 4
                  + rx_audio.FILTER_DELAY_DECODE_SAMPLES)) <= 6


def test_aligned_and_real_receivers_survive_a_deterministic_awgn_smoke_point():
    payload = bytes(range(64))
    tx = hr1.HR1.encode(payload).astype(np.float64)
    power = float(np.mean(tx ** 2))
    noise = np.random.default_rng(20260830).normal(
        0.0, np.sqrt(power / 10 ** (-14.0 / 10.0)), len(tx))
    captured = _capture(tx + noise)
    aligned = hr1.decode_aligned(
        captured,
        preamble_start=(hr1.LEAD_RX_SAMPLES
                        + rx_audio.FILTER_DELAY_DECODE_SAMPLES),
    )
    real = hr1.HR1.decode(captured)
    assert aligned["payload"] == real["payload"] == payload


def test_truncation_wrong_class_and_corrupt_soft_bits_never_return_payload():
    payload = bytes(range(64))
    tx = hr1.HR1.encode(payload)
    truncated = _capture(tx[:hr1.LEAD_TX_SAMPLES
                            + (hr1.CLASS_SYMBOLS + 20)
                            * hr1.SYMBOL_TX_SAMPLES])
    assert hr1.HR1.decode(truncated)["payload"] is None

    wrong_class = _capture(hr1._modulate_frame(payload,
                                                class_id=hr1.TINY_CLASS))
    wrong = hr1.HR1.decode(wrong_class)
    assert wrong["payload"] is None
    assert wrong["candidates_tried"] == 0

    coded = hr1.encode_coded_bits(payload)
    corrupted = coded.copy()
    corrupted[::3] ^= 1
    checked, meta = hr1.decode_coded_soft(1.0 - 2.0 * corrupted)
    assert checked is None
    assert not (meta["crc_ok"] and meta["fec_tail_ok"])


@pytest.mark.parametrize("kind", ["silence", "noise", "carrier",
                                   "nonfinite", "wrongshape", "nonnumeric",
                                   "empty"])
def test_hostile_inputs_are_safe_integrity_checked_and_bounded(kind):
    count = hr1.FRAME_TX_SAMPLES // 4
    if kind == "silence":
        audio = np.zeros(count)
    elif kind == "noise":
        audio = np.random.default_rng(919).normal(0.0, 0.2, count)
    elif kind == "carrier":
        audio = 0.2 * np.sin(2 * np.pi * 1_500 * np.arange(count) / 12_000)
    elif kind == "nonfinite":
        audio = np.zeros(count)
        audio[count // 2] = np.nan
    elif kind == "wrongshape":
        audio = np.zeros((100, 2))
    elif kind == "nonnumeric":
        audio = np.asarray(["not audio"], dtype=object)
    else:
        audio = np.zeros(0)
    result = hr1.HR1.decode(audio)
    assert result["payload"] is None
    assert result["candidate_count"] <= hr1.MAX_CANDIDATES
    assert result["candidates_tried"] <= hr1.MAX_CANDIDATES
    assert result["search_cells_evaluated"] <= (
        1_001 * len(hr1.CFO_HYPOTHESES) * len(hr1.CLASS_WORDS))


def test_input_length_cap_and_repeat_decode_are_deterministic():
    hostile = np.random.default_rng(72).normal(
        0.0, 0.1, hr1.MAX_CAPTURE_SAMPLES + 5_000)
    first = hr1.HR1.decode(hostile)
    second = hr1.HR1.decode(hostile)
    for key in ("payload", "confidence", "candidate_count",
                "candidates_tried", "search_cells_evaluated", "failure"):
        assert first.get(key) == second.get(key)
    assert first["capture_truncated_to_limit"] is True


def test_complete_keying_occupied_bandwidth_is_inside_the_design_gate():
    audio = hr1.HR1.encode(bytes(range(64)))
    occupied = benchmark._occupied_bandwidth_99(audio, hr1.TX_SAMPLE_RATE)
    assert occupied["width_hz"] < 2_300
    assert occupied["low_hz"] > 300
    assert occupied["high_hz"] < 2_400


def test_benchmark_loads_the_mode_and_records_bounded_decoder_metrics():
    selector = "experiments.hr1.hr1:HR1"
    mode = benchmark.load_mode(selector)
    assert mode is hr1.HR1
    key = benchmark.canonical_point_key("awgn", None, 20)
    task = {
        "mode_selector": selector, "model": "awgn", "preset": None,
        "snr_db": 20.0, "point_key": key, "trial": 1,
        "derived_seeds": {
            namespace: benchmark.derive_seed(20260830, namespace, key, 1)
            for namespace in ("workload", "watterson", "awgn")
        },
    }
    record, _ = benchmark.execute_trial(task)
    assert record["outcome"] == "decoded"
    metrics = record["decoder_metrics"]
    assert metrics["candidate_limit"] == hr1.MAX_CANDIDATES
    assert metrics["candidate_count"] <= metrics["candidate_limit"]
    assert metrics["search_cells_evaluated"] == 81_081
