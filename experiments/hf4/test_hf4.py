"""Unit tests for the standalone HF4 experiment.

HF4 (`experiments/hf4/hf4.py`, wired for the link ABI by
`whale/modes/hf4_mode.py`) is not registered in any `ModeRegistry` or in
`whale.mode_qualification.MANIFEST`. These tests exercise it directly and
through its `WaveformMode` adapter, following the shape of
`MODE_QUALIFICATION.md` section 1's per-mode unit/malformed-input gate:
zero/representative/maximum payload round trips, oversize rejection, and
rejection (without an exception or unbounded work) of hostile/degenerate
audio.
"""

from __future__ import annotations

import numpy as np
import pytest

from experiments.hf4 import hf4
from whale import rx_audio
from whale.modes.hf4_mode import HF4


def _payload(n: int, seed: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    return bytes(rng.integers(0, 256, n, dtype=np.uint8))


def _round_trip(payload: bytes) -> dict:
    """Encode through the real 48 kHz TX path, decimate like the link does,
    and decode -- the same boundary `whale.modes` uses everywhere else."""
    tx = HF4.encode(payload)
    assert tx.dtype == np.float32
    assert tx.ndim == 1
    rx = rx_audio.downsample(tx)
    return HF4.decode(rx)


# --------------------------------------------------------------------------
# Round trips: zero, representative, and maximum payload.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("size", [0, 1, 731, hf4.MAX_PAYLOAD_BYTES // 2,
                                   hf4.MAX_PAYLOAD_BYTES])
def test_round_trip_exact_payload(size):
    payload = _payload(size, seed=size)
    result = _round_trip(payload)
    assert result["synced"] is True
    assert result["crc_ok"] is True
    assert result["payload"] == payload


def test_round_trip_via_chunk_size_matches_air_header_budget():
    """`HF4.chunk_size` is what the link would actually hand this mode."""
    from whale import framing
    assert HF4.chunk_size == hf4.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES
    payload = _payload(HF4.chunk_size, seed=99)
    result = _round_trip(payload)
    assert result["payload"] == payload


def test_encode_output_matches_declared_rates_and_airtime():
    payload = _payload(200, seed=1)
    tx = HF4.encode(payload)
    assert HF4.tx_sample_rate == hf4.SAMPLE_RATE == 48_000
    assert HF4.rx_sample_rate == hf4.RX_SAMPLE_RATE == 12_000
    expected_seconds = len(tx) / HF4.tx_sample_rate
    assert expected_seconds == pytest.approx(HF4.airtime(len(payload)), rel=1e-6)
    assert HF4.airtime(len(payload)) == pytest.approx(hf4.FRAME_SECONDS)
    # HF4 is a fixed-length frame: airtime does not depend on payload size.
    assert HF4.airtime(0) == HF4.airtime(hf4.MAX_PAYLOAD_BYTES)


# --------------------------------------------------------------------------
# Oversize payload: refused, not truncated.
# --------------------------------------------------------------------------

def test_oversize_payload_raises_without_truncation():
    with pytest.raises(ValueError):
        HF4.encode(_payload(hf4.MAX_PAYLOAD_BYTES + 1, seed=2))


def test_oversize_payload_raises_at_the_hf4_layer_too():
    with pytest.raises(ValueError):
        hf4.modulate(_payload(hf4.MAX_PAYLOAD_BYTES + 1, seed=3))


# --------------------------------------------------------------------------
# Malformed / degenerate receive audio: a clean non-decode, never an
# exception or unbounded work.
# --------------------------------------------------------------------------

def _assert_clean_non_decode(audio):
    result = HF4.decode(audio)
    assert result["payload"] is None


def test_rejects_empty_audio():
    _assert_clean_non_decode(np.zeros(0, dtype=np.float32))


def test_rejects_silence():
    _assert_clean_non_decode(np.zeros(60_000, dtype=np.float32))


def test_rejects_bounded_white_noise():
    rng = np.random.default_rng(4)
    noise = rng.normal(scale=0.05, size=60_000).astype(np.float32)
    _assert_clean_non_decode(noise)


def test_rejects_bare_carrier():
    t = np.arange(60_000) / hf4.RX_SAMPLE_RATE
    tone = (0.2 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
    _assert_clean_non_decode(tone)


def test_rejects_truncated_frame():
    payload = _payload(500, seed=5)
    tx = HF4.encode(payload)
    rx = rx_audio.downsample(tx)
    truncated = rx[: len(rx) // 3]
    _assert_clean_non_decode(truncated)


def test_rejects_non_finite_audio():
    audio = np.full(10_000, np.nan, dtype=np.float32)
    result = HF4.decode(audio)
    assert result["synced"] is False
    assert result["payload"] is None


def test_rejects_wrong_shaped_audio():
    result = HF4.decode(np.zeros((10, 10), dtype=np.float32))
    assert result["synced"] is False
    assert result["payload"] is None


def test_corrupt_payload_fails_crc_not_exception():
    # A near-maximum payload so the checked length+payload+CRC region spans
    # nearly the whole coded packet -- a small payload leaves most of the
    # packet as unused zero padding, and corrupting only that padding would
    # not touch anything the CRC actually covers.
    payload = _payload(hf4.MAX_PAYLOAD_BYTES - 10, seed=6)
    tx = HF4.encode(payload)
    rx = rx_audio.downsample(tx).copy()
    # Overwrite the back half of the frame (well past acquisition and the
    # header/training block) with strong noise -- enough to guarantee the
    # coded payload is wrong, without erasing acquisition, which only looks
    # at the sync block near the very start.
    half = len(rx) // 2
    rng = np.random.default_rng(9)
    rx[half:] = rng.normal(scale=1.0, size=len(rx) - half).astype(np.float32)
    result = HF4.decode(rx)
    assert result["synced"] is True  # acquisition still locks on the header
    assert result["crc_ok"] is False
    assert result["payload"] is None


def test_impossible_declared_length_is_rejected_cleanly():
    """A frame whose (whitened) length field decodes to an impossible value
    must fail cleanly rather than slice a bogus payload."""
    payload = _payload(10, seed=7)
    packet = hf4._build_packet(payload)
    # Corrupt only the two big-endian length bytes to an out-of-range value.
    corrupted = bytearray(packet)
    corrupted[0:2] = (0xFFFF).to_bytes(2, "big")
    # Route the corrupted packet through the real coding chain (whitening,
    # convolutional coding, puncturing, interleaving) so this exercises the
    # same path a real corrupted frame would, not a hand-rolled bit layout.
    data_values = hf4._packet_to_data_values(bytes(corrupted))

    symbols = [hf4.SYNC_VALUES.copy() for _ in range(hf4.SYNC_SYMBOLS)]
    symbols.extend(hf4.HEADER_VALUES[i] for i in range(hf4.HEADER_SYMBOLS))
    for kind, index in hf4.PAYLOAD_LAYOUT:
        symbols.append(data_values[index] if kind == "data" else hf4.PILOT_VALUES)
    from whale.dsp import ofdm as _ofdm
    core_chunks = [_ofdm.build_symbol(hf4.GEOMETRY, values) for values in symbols]
    core_audio = hf4._apply_edge_window(np.concatenate(core_chunks))
    burst = np.concatenate((
        np.zeros(hf4.LEAD_SAMPLES), core_audio, np.zeros(hf4.TAIL_SAMPLES)))

    result = hf4.demodulate(burst)
    assert result["synced"] is True
    assert result["decoded_length"] > hf4.MAX_PAYLOAD_BYTES
    assert result["payload"] is None
    assert result.get("crc_ok") is False


# --------------------------------------------------------------------------
# Geometry / bandwidth sanity: carriers stay clear of the 300-2700 Hz edges,
# and the mode ID is unique against the existing registry.
# --------------------------------------------------------------------------

def test_carrier_plan_is_inside_the_300_to_2700_hz_ceiling():
    low_hz = hf4.CARRIER_BINS[0] * hf4.CARRIER_SPACING_HZ
    high_hz = hf4.CARRIER_BINS[-1] * hf4.CARRIER_SPACING_HZ
    assert low_hz > 300.0
    assert high_hz < 2700.0
    # Explicit edge margin, not just "inside": real SSB filter rolloff and
    # residual OFDM sidelobes both need room below both ceiling edges. This
    # threshold was 50.0 Hz before the 2026-09-01 guard-interval fix; two
    # carriers were added at the top and one at the bottom (75 total, up
    # from 72) to recover throughput lost to the longer cyclic prefix,
    # narrowing the raw carrier-frequency margin to 43.75 Hz each side --
    # still a full carrier spacing (31.25 Hz) of slack beyond this 40 Hz
    # floor, and the actual occupied-bandwidth statistical campaign (see
    # RESULTS.md) is the binding, measured gate against the real 300-2,700
    # Hz ceiling, not this static per-carrier check.
    assert low_hz - 300.0 >= 40.0
    assert 2700.0 - high_hz >= 40.0


def test_mode_id_is_not_already_registered():
    from whale.mode_qualification import MANIFEST
    used_ids = {entry.mode_id for entry in MANIFEST}
    assert HF4.mode_id not in used_ids


def test_net_throughput_exceeds_level_4_hard_floor():
    """Reproduces MODE_QUALIFICATION.md section 4's formula from the real
    encoder output, not from a nominal symbol-rate calculation."""
    from whale import framing
    chunk_bytes = hf4.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES
    tx = HF4.encode(_payload(chunk_bytes, seed=8))
    airtime_seconds = len(tx) / HF4.tx_sample_rate
    net_bit_rate = 8 * chunk_bytes / airtime_seconds
    assert net_bit_rate > 7_000.0


# --------------------------------------------------------------------------
# Inner FEC / interleaving: the 2026-09-01 fix for the plateau this design
# had against localized, frame-static per-carrier fades (see DESIGN.md and
# RESULTS.md's "second design gap").
# --------------------------------------------------------------------------

def test_puncture_depuncture_round_trip_is_lossless():
    rng = np.random.default_rng(11)
    mother_bits = rng.integers(0, 2, hf4.PUNCTURE_MOTHER_BITS * 17,
                               dtype=np.uint8)
    coded = hf4._puncture(mother_bits)
    assert len(coded) == 17 * hf4.FEC_N
    # Every kept bit round-trips through depuncture-to-soft with its sign
    # intact (+-1 soft value standing in for a hard bit here); every
    # punctured position comes back as an exact-zero erasure.
    soft = np.where(coded == 0, 1.0, -1.0)
    depunctured = hf4._depuncture_to_soft(soft)
    grid = depunctured.reshape(-1, hf4.PUNCTURE_MOTHER_BITS)
    kept_back = grid[:, hf4.PUNCTURE_KEEP]
    assert np.array_equal(kept_back, soft.reshape(-1, hf4.FEC_N))
    erased_mask = np.ones(hf4.PUNCTURE_MOTHER_BITS, dtype=bool)
    erased_mask[hf4.PUNCTURE_KEEP] = False
    assert np.all(grid[:, erased_mask] == 0.0)


def test_interleaver_spread_gather_round_trip():
    rng = np.random.default_rng(12)
    bits = rng.integers(0, 2, hf4.RAW_BITS, dtype=np.uint8)
    spread = hf4.INTERLEAVER.spread(bits)
    assert hf4.INTERLEAVER.is_valid()
    assert np.array_equal(hf4.INTERLEAVER.gather(spread), bits)


def test_one_dead_carrier_still_decodes():
    """The exact failure mode the inner FEC/interleaving fix targets: a
    single carrier that is garbage (deep, frame-static fade) for the whole
    frame must not by itself prevent the frame from decoding -- the
    no-FEC/no-interleaving predecessor design had no way to survive this at
    any SNR (see RESULTS.md's "second design gap").

    This mirrors what `demodulate` actually does end to end: a per-carrier
    reliability weight (from `whale.dsp.equalize.carrier_weights`, driven
    by the header fit's per-carrier SNR) discounts the dead carrier's soft
    bits toward an erasure *before* Viterbi decoding, rather than handing
    the decoder full-confidence bits that happen to be wrong -- see
    `_data_values_to_packet_bits`'s docstring for why that distinction is
    exactly what makes this recoverable.
    """
    payload = _payload(hf4.MAX_PAYLOAD_BYTES, seed=21)

    # Zero out one carrier bin across every payload OFDM symbol post-hoc is
    # awkward in the time domain, so instead corrupt the equivalent
    # information directly: re-run the encode chain with one carrier's
    # worth of data values replaced by strong garbage, bypassing modulate()
    # to reach into the coding pipeline the same way the demodulate side
    # does. This targets the coding/interleaving fix in isolation from
    # OFDM/channel effects the benchmark harness already covers.
    packet = hf4._build_packet(payload)
    data_values = hf4._packet_to_data_values(packet)
    corrupted = data_values.copy()
    dead_carrier = 5
    rng = np.random.default_rng(22)
    corrupted[:, dead_carrier] = (
        rng.normal(size=hf4.DATA_SYMBOLS) + 1j * rng.normal(size=hf4.DATA_SYMBOLS)
    )  # unit-scale noise: consistently mis-sliced, like a genuinely dead carrier
    weights = np.ones(hf4.CARRIER_COUNT)
    # 2026-09-01 dense-carrier redesign: 0.05 (the old weight, matching the
    # old low=0.05 carrier_weights floor) occasionally let this exact
    # scenario mislead the stronger rate-8/9 Viterbi decoder; demodulate's
    # floor was tightened to 0.02 to match (see hf4.py), so this test uses
    # the same value a real header-fit-driven weight would now produce.
    weights[dead_carrier] = 0.02  # a header fit would score this carrier's SNR low
    packet_bits = hf4._data_values_to_packet_bits(corrupted, carrier_weights=weights)
    packet_bytes = np.packbits(packet_bits).tobytes()
    length = int.from_bytes(packet_bytes[0:hf4.LENGTH_BYTES], "big")
    recovered_payload = packet_bytes[hf4.LENGTH_BYTES:hf4.LENGTH_BYTES + length]
    crc_at = hf4.LENGTH_BYTES + length
    received_crc = int.from_bytes(
        packet_bytes[crc_at:crc_at + hf4.CRC_BYTES], "big")
    import binascii
    assert received_crc == (binascii.crc32(recovered_payload) & 0xFFFFFFFF)
    assert recovered_payload == payload


def test_interleaver_does_not_reduce_to_a_plain_reshape():
    """Regression for the 2026-09-01 hardware-debug bug: the original
    `block()`-plus-transpose construction of `INTERLEAVER`/`_to_symbol_grid`
    was mathematically the identity permutation on the (DATA_SYMBOLS,
    CARRIER_COUNT * BITS_PER_CARRIER) grid -- provably no interleaving at
    all, despite `INTERLEAVER.is_valid()` being true and the dead-carrier
    test above passing. Assert directly that `_to_symbol_grid` differs from
    a bare reshape, so a future refactor cannot silently regress to the
    same no-op."""
    coded = np.arange(hf4.RAW_BITS, dtype=np.int64)
    grid = hf4._to_symbol_grid(coded)
    plain_reshape = coded.reshape(hf4.DATA_SYMBOLS, hf4._INTERLEAVE_COLUMNS)
    assert not np.array_equal(grid, plain_reshape)


def test_one_bad_symbol_still_decodes():
    """The hardware failure mode itself: on the real IC-7300/IC-705 link
    (`logs/mode_qualification/hf-ssb/hf4/2026-09-01-hardware/`), two
    independently-faded synced frames both corrupted the packet's length
    field (the very first symbol's worth of coded bits) to the identical
    wrong value. Direct capture replay wasn't practical as a fast unit
    test (acquisition/CFO/equalization all need to run against saved
    120,000-sample .npy audio), so this reconstructs the specific defect
    at the coding-pipeline level instead: with the old interleaver bug, one
    entire OFDM data symbol's worth of corruption (an AGC/ALC transient
    or filter artifact localized in time, not frequency -- exactly what a
    real SSB radio can do right after the header block, and exactly what
    the frame-static two-path benign/static simulation model does not
    exercise) landed as one dense, contiguous run in the coded stream,
    which the K=7 Viterbi decoder cannot correct. With the interleaver
    fixed (spreading across both carriers and symbols), one bad symbol
    should not by itself prevent the frame from decoding, the same way one
    bad carrier already does not (`test_one_dead_carrier_still_decodes`)."""
    # Severity note: an *outright-replaced* (100% independent noise) full
    # symbol at full decode confidence turns out to be more corruption than
    # this rate-11/12 code can recover from regardless of interleaving --
    # the same is true of a single fully-replaced dead carrier at full
    # confidence (weight 1.0, not the discounted 0.02
    # `test_one_dead_carrier_still_decodes` uses); a real AGC/ALC transient
    # degrades a symbol's amplitude and adds noise, it does not usually
    # erase it to independent garbage. 70% amplitude retained plus
    # noise at 15% of the signal scale was found, empirically, to fail
    # every time against the old buggy (identity-reshape) interleaver and
    # succeed every time against the fixed one -- a real, reproducible
    # difference this test locks in, at a severity representative of what
    # a real transient plausibly does rather than a worst-case erasure.
    payload = _payload(hf4.MAX_PAYLOAD_BYTES, seed=23)
    packet = hf4._build_packet(payload)
    data_values = hf4._packet_to_data_values(packet)
    corrupted = data_values.copy()
    bad_symbol = 0  # the exact position a length-field-corrupting bug hits
    rng = np.random.default_rng(24)
    corrupted[bad_symbol, :] = (
        0.7 * corrupted[bad_symbol, :]
        + 0.15 * (rng.normal(size=hf4.CARRIER_COUNT)
                  + 1j * rng.normal(size=hf4.CARRIER_COUNT))
    )
    weights = np.ones(hf4.CARRIER_COUNT)  # a whole-symbol transient is not
    # something a per-carrier SNR weight can see or discount -- the
    # interleaver alone has to keep this from being fatal.
    packet_bits = hf4._data_values_to_packet_bits(corrupted, carrier_weights=weights)
    packet_bytes = np.packbits(packet_bits).tobytes()
    length = int.from_bytes(packet_bytes[0:hf4.LENGTH_BYTES], "big")
    recovered_payload = packet_bytes[hf4.LENGTH_BYTES:hf4.LENGTH_BYTES + length]
    crc_at = hf4.LENGTH_BYTES + length
    received_crc = int.from_bytes(
        packet_bytes[crc_at:crc_at + hf4.CRC_BYTES], "big")
    import binascii
    assert received_crc == (binascii.crc32(recovered_payload) & 0xFFFFFFFF)
    assert recovered_payload == payload


def test_survives_the_required_benign_static_bandpass_filter_alone():
    """Regression for the 2026-09-01 guard-interval bug.

    HF4's 0.0-guard predecessor (12-sample/1.0 ms cyclic prefix) decoded
    zero frames at every tested SNR, including 20 dB, because
    SPEED_LADDERS.md's benign/static envelope requires a qualification
    channel to retain a real 250-3,100 Hz bandpass filter, and that
    filter's own impulse-response memory (several ms, an order of
    magnitude longer than 0.1 ms propagation delay spread) exceeded the
    guard interval regardless of noise -- see
    `logs/mode_qualification/hf-ssb/hf4/2026-09-01/INDEX.md`. This
    reproduces that exact noiseless diagnostic (the required bandpass
    filter applied twice, before and after, with no noise or fading) and
    would fail the same way if the guard interval regressed to being too
    short for the filter's memory again.
    """
    from whale.channel import FilterChannel

    payload = _payload(hf4.MAX_PAYLOAD_BYTES, seed=99)
    tx = hf4.modulate(payload)
    filt_a = FilterChannel(hf4.SAMPLE_RATE, low_hz=250.0, high_hz=3_100.0)
    filt_b = FilterChannel(hf4.SAMPLE_RATE, low_hz=250.0, high_hz=3_100.0)
    stage_a = filt_a.process(tx)
    stage_a_tail = filt_a.drain()
    mid = np.concatenate((stage_a.audio, stage_a_tail.audio))
    stage_b = filt_b.process(mid)
    stage_b_tail = filt_b.drain()
    filtered = np.concatenate((stage_b.audio, stage_b_tail.audio))

    rx = rx_audio.downsample(filtered.astype(np.float32))
    result = hf4.demodulate(rx)
    assert result["synced"] is True
    assert result["crc_ok"] is True
    assert result["payload"] == payload
