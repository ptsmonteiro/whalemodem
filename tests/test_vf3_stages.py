"""The VF3 decode stages that no other test pins.

`test_vf3_kernels.py` covers the two vectorized kernels and
`test_vf3_capture_replay.py` covers the whole chain on recorded audio.  In
between sit the stages that the DSP extraction will actually lift: the
OFDM symbol build/analyze pair, acquisition, the packet/CRC codec, the
differential QPSK mapper and the head measurement.  This file holds each
of them on its own, so that when one moves into `whale/dsp/` a break points
at the stage rather than at a capture digest.

Software only -- no radios, no sound cards.
"""

import numpy as np
import pytest
from scipy.signal import hilbert

from whale import rx_audio
from whale.modes import vf3
from whale.modes.vf3 import (CARRIER_BINS, CORE_SAMPLES, GUARD_SAMPLES,
                             HEADER_SYMBOLS, HEADER_VALUES, MAX_PAYLOAD_BYTES,
                             N_CARRIERS, PAYLOAD_BITS, PAYLOAD_SYMBOLS,
                             SYMBOL_SAMPLES, TOTAL_SYMBOLS)

RNG = np.random.default_rng(20260828)


def _payload(size=MAX_PAYLOAD_BYTES):
    return bytes(RNG.integers(0, 256, size, dtype=np.uint8))


# -- OFDM symbol build / analyze -----------------------------------------

def test_symbol_build_and_analyze_round_trip():
    values = vf3.qpsk_from_bits(RNG.integers(0, 2, 2 * N_CARRIERS))
    symbol = vf3.build_symbol(values)
    assert len(symbol) == SYMBOL_SAMPLES
    recovered = vf3.symbol_carriers(symbol)
    assert recovered == pytest.approx(values, abs=1e-9)


def test_symbol_carries_energy_only_on_its_own_bins():
    values = vf3.qpsk_from_bits(RNG.integers(0, 2, 2 * N_CARRIERS))
    core = vf3.build_symbol(values)[GUARD_SAMPLES:]
    spectrum = np.abs(np.fft.fft(core))
    occupied = np.zeros(CORE_SAMPLES, dtype=bool)
    occupied[CARRIER_BINS] = True
    occupied[-CARRIER_BINS] = True
    assert np.max(spectrum[~occupied]) < 1e-9
    assert np.min(spectrum[occupied]) > 1e-3


def test_the_cyclic_prefix_really_is_the_tail_of_the_core():
    symbol = vf3.build_symbol(vf3.SYNC_VALUES)
    assert symbol[:GUARD_SAMPLES] == pytest.approx(symbol[-GUARD_SAMPLES:])


@pytest.mark.parametrize("offset", [0, 37, GUARD_SAMPLES])
def test_analyze_is_invariant_to_the_fft_offset_inside_the_guard(offset):
    """The undo-shift is what lets timing wander inside the prefix."""
    values = vf3.qpsk_from_bits(RNG.integers(0, 2, 2 * N_CARRIERS))
    symbol = vf3.build_symbol(values)
    assert vf3.symbol_carriers(symbol, offset) == pytest.approx(
        values, abs=1e-9)


@pytest.mark.parametrize("offset", [-1, GUARD_SAMPLES + 1])
def test_analyze_rejects_an_offset_outside_the_guard(offset):
    symbol = vf3.build_symbol(vf3.SYNC_VALUES)
    with pytest.raises(ValueError):
        vf3.symbol_carriers(symbol, offset)


def test_build_rejects_a_wrong_carrier_count():
    with pytest.raises(ValueError):
        vf3.build_symbol(np.ones(N_CARRIERS - 1, dtype=np.complex128))


# -- preamble correlation and acquisition --------------------------------

def test_acquisition_finds_a_frame_placed_at_a_known_offset():
    audio = vf3.modulate(_payload(64))
    lead = vf3.lead_in_samples()
    padded = np.concatenate((np.zeros(5_000), audio))
    received = rx_audio.downsample(padded)
    start, confidence = vf3._acquire(hilbert(received))
    assert confidence > vf3.ACQUISITION_THRESHOLD
    # Acquisition may land inside the cyclic prefix, which is exactly what
    # the FFT offset absorbs.
    expected = ((5_000 + lead) // rx_audio.DECIMATION
                + rx_audio.FILTER_DELAY_DECODE_SAMPLES)
    assert abs(start - expected) <= vf3.RX_GUARD_SAMPLES


def test_acquisition_ignores_symbol_periodic_decoy_energy():
    """The hazard vf3's module docstring names.

    `_acquire` locks by correlating the signal against itself one whole
    symbol apart, so energy that repeats on the 1152-sample symbol period
    -- rather than the 1024-sample core period the real head uses --
    correlates just as well as the header and can swallow the true peak
    into one contiguous proposal group.  The grouping and the
    `_header_candidate_snr` ranking exist to break exactly that tie.

    Note the 0.68 proposal threshold is a tuning constant with slack, not a
    behavioural invariant: it can be moved to 0.50 without changing the
    answer here or on any recorded capture.  What is pinned is the outcome.
    """
    payload = _payload(64)
    audio = vf3.modulate(payload)
    decoy = np.resize(vf3.build_symbol(vf3.SYNC_VALUES),
                      12 * SYMBOL_SAMPLES)
    signal = np.concatenate((decoy, audio)).astype(np.float64)
    true_start = len(decoy) + vf3.lead_in_samples()

    received = rx_audio.downsample(signal)
    start, confidence = vf3._acquire(hilbert(received))
    assert confidence > vf3.ACQUISITION_THRESHOLD
    expected = (true_start // rx_audio.DECIMATION
                + rx_audio.FILTER_DELAY_DECODE_SAMPLES)
    assert abs(start - expected) <= vf3.RX_GUARD_SAMPLES
    assert vf3.demodulate(received)["payload"] == payload


def test_acquisition_is_unconfident_on_noise():
    noise = RNG.normal(0.0, 0.1, 8 * vf3.RX_SYMBOL_SAMPLES)
    _, confidence = vf3._acquire(hilbert(noise))
    assert confidence < vf3.ACQUISITION_THRESHOLD


def test_acquisition_declines_a_capture_shorter_than_its_correlation_span():
    start, confidence = vf3._acquire(hilbert(np.zeros(vf3.RX_SYMBOL_SAMPLES)))
    assert start is None and confidence == 0.0


# -- the packet / CRC codec ----------------------------------------------

@pytest.mark.parametrize("size", [0, 1, 17, 1_000, MAX_PAYLOAD_BYTES])
def test_payload_codec_round_trips_at_every_length(size):
    payload = _payload(size)
    bits = vf3.encode_payload_bits(payload)
    assert len(bits) == PAYLOAD_BITS
    decoded, meta = vf3.decode_payload_bits(bits)
    assert decoded == payload
    assert meta["crc_ok"] and meta["fec_tail_ok"]
    assert meta["decoded_length"] == size


def test_payload_codec_rejects_an_oversized_payload():
    with pytest.raises(ValueError):
        vf3.encode_payload_bits(_payload(MAX_PAYLOAD_BYTES + 1))


def test_the_crc_actually_rejects_a_corrupted_frame():
    """Enough damage to beat the FEC must fail the CRC, not decode wrong."""
    bits = vf3.encode_payload_bits(_payload(200))
    damaged = bits.copy()
    damaged[::3] ^= 1
    payload, meta = vf3.decode_payload_bits(damaged)
    assert payload is None
    assert not meta["crc_ok"]


def test_the_code_corrects_a_burst_the_interleaver_spreads_out():
    payload = _payload(200)
    bits = vf3.encode_payload_bits(payload)
    damaged = bits.copy()
    damaged[5_000:5_120] ^= 1
    assert vf3.decode_payload_bits(damaged)[0] == payload


def test_soft_and_hard_decoding_agree_on_a_clean_frame():
    payload = _payload(300)
    bits = vf3.encode_payload_bits(payload)
    soft = 1.0 - 2.0 * bits.astype(np.float64)
    assert vf3.decode_payload_soft(soft)[0] == payload


@pytest.mark.parametrize("decode", [vf3.decode_payload_bits,
                                    vf3.decode_payload_soft])
def test_decoders_reject_a_wrong_bit_count(decode):
    with pytest.raises(ValueError):
        decode(np.zeros(PAYLOAD_BITS - 2))


# -- differential QPSK ---------------------------------------------------

def test_differential_encode_and_observe_invert_each_other():
    bits = RNG.integers(0, 2, PAYLOAD_BITS).astype(np.uint8)
    initial = HEADER_VALUES[-1]
    values = vf3.differential_encode(bits, initial)
    assert values.shape == (PAYLOAD_SYMBOLS, N_CARRIERS)
    observed = vf3.differential_observations(values, initial)
    assert np.array_equal(vf3.differential_bits(observed), bits)


def test_differential_decoding_survives_an_arbitrary_constant_phase():
    """The point of DQPSK: only phase *changes* carry information."""
    bits = RNG.integers(0, 2, PAYLOAD_BITS).astype(np.uint8)
    initial = HEADER_VALUES[-1]
    values = vf3.differential_encode(bits, initial)
    rotation = np.exp(1j * 0.9)
    observed = vf3.differential_observations(values * rotation,
                                             initial * rotation)
    assert np.array_equal(vf3.differential_bits(observed), bits)


def test_soft_bit_signs_agree_with_the_hard_decisions():
    bits = RNG.integers(0, 2, PAYLOAD_BITS).astype(np.uint8)
    initial = HEADER_VALUES[-1]
    observed = vf3.differential_observations(
        vf3.differential_encode(bits, initial), initial)
    soft = vf3.differential_soft_bits(observed, np.ones(N_CARRIERS))
    assert np.array_equal((soft < 0).astype(np.uint8), bits)


def test_carrier_weights_scale_the_reliabilities_they_belong_to():
    bits = RNG.integers(0, 2, PAYLOAD_BITS).astype(np.uint8)
    initial = HEADER_VALUES[-1]
    observed = vf3.differential_observations(
        vf3.differential_encode(bits, initial), initial)
    weights = np.ones(N_CARRIERS)
    weights[7] = 0.0
    soft = vf3.differential_soft_bits(observed, weights).reshape(
        PAYLOAD_SYMBOLS, N_CARRIERS, 2)
    assert np.all(soft[:, 7, :] == 0.0)
    assert np.all(soft[:, 8, :] != 0.0)


# -- head measurement ----------------------------------------------------

def test_head_measurement_counts_the_cores_that_were_sent():
    head_seconds = 0.5
    audio = vf3.modulate(_payload(64), head_seconds=head_seconds)
    start = vf3.lead_in_samples(head_seconds)
    received = rx_audio.downsample(audio)
    rx_start = (start // rx_audio.DECIMATION
                + rx_audio.FILTER_DELAY_DECODE_SAMPLES)
    count, score = vf3._measure_head(received, rx_start)
    assert score > vf3.HEAD_MATCH_THRESHOLD
    # The ramped-up first core and the partial core left by the resize are
    # not required to count; everything between them is.
    assert count >= start // CORE_SAMPLES - 2


def test_head_measurement_stops_at_a_blackout():
    head_seconds = 0.5
    audio = vf3.modulate(_payload(64), head_seconds=head_seconds).astype(
        np.float64)
    start = vf3.lead_in_samples(head_seconds)
    audio[:start - 3 * CORE_SAMPLES] = 0.0
    received = rx_audio.downsample(audio)
    rx_start = (start // rx_audio.DECIMATION
                + rx_audio.FILTER_DELAY_DECODE_SAMPLES)
    count, _ = vf3._measure_head(received, rx_start)
    assert count == 3


# -- frame geometry ------------------------------------------------------

def test_frame_length_tracks_the_requested_head():
    assert vf3.frame_samples(0.0) == vf3.FRAME_SAMPLES
    assert len(vf3.modulate(b"x", head_seconds=1.0)) == vf3.frame_samples(1.0)
    assert vf3.frame_samples(1.0) > vf3.frame_samples(0.045)


def test_a_negative_head_is_refused():
    with pytest.raises(ValueError):
        vf3.lead_in_samples(-0.1)


def test_the_constellation_is_the_header_then_the_payload():
    values = vf3.frame_constellation(_payload(64))
    assert values.shape == (TOTAL_SYMBOLS, N_CARRIERS)
    assert values[:HEADER_SYMBOLS] == pytest.approx(HEADER_VALUES)
