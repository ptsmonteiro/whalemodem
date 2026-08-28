"""The DSP kernels on their own terms.

`tests/test_vf3_*.py` exercise these through VF3, which is the strongest
evidence they are right -- it is the configuration that has been on the
air.  But VF3 uses one geometry, one code and one interleaver, and three
kernels it does not use at all: the block interleaver, the frequency
offset estimators and the pilot equalizer.  This file covers the
parameterization VF3 cannot, and pins the three that no mode calls yet.

Software only -- no radios, no sound cards.
"""

import numpy as np
import pytest
from scipy.signal import hilbert

from whale import dsp
from whale.dsp import (acquire, bits, differential, equalize, fec, framing,
                       freq, head, interleave, mfsk, ofdm, timing)

RNG = np.random.default_rng(20260828)


def geometry(core=256, guard=32, carriers=None, rate=48_000, rms=0.13):
    bins = np.arange(4, 20) if carriers is None else np.asarray(carriers)
    return ofdm.Geometry(sample_rate=rate, core_samples=core,
                         guard_samples=guard,
                         carrier_bins=bins).scaled_to_rms(rms)


def qpsk(count):
    return bits.qpsk_from_bits(RNG.integers(0, 2, 2 * count))


# -- geometry -------------------------------------------------------------

def test_geometry_derives_the_frame_dimensions():
    g = geometry(core=1024, guard=128, carriers=np.arange(10, 68))
    assert g.symbol_samples == 1152
    assert g.carrier_count == 58
    assert g.carrier_spacing_hz == 46.875
    assert g.carrier_hz[0] == 468.75 and g.carrier_hz[-1] == 3140.625


def test_geometry_scaling_hits_the_requested_rms():
    g = geometry(core=1024, guard=128, carriers=np.arange(10, 68), rms=0.13)
    symbol = ofdm.build_symbol(g, qpsk(g.carrier_count))
    assert np.sqrt(np.mean(symbol ** 2)) == pytest.approx(0.13, rel=0.02)


@pytest.mark.parametrize("bins", [
    [0, 4],            # DC has no conjugate partner
    [4, 128],          # Nyquist of a 256-sample core
    [4, 4],            # duplicated
    [],                # empty
])
def test_geometry_refuses_unusable_carrier_bins(bins):
    with pytest.raises(ValueError):
        ofdm.Geometry(sample_rate=48_000, core_samples=256, guard_samples=32,
                      carrier_bins=np.asarray(bins, dtype=np.int32))


def test_geometry_carrier_bins_cannot_be_mutated_underneath_it():
    """The bins are cached into carrier_hz, so they must not drift."""
    g = geometry()
    with pytest.raises(ValueError):
        g.carrier_bins[0] = 99


# -- OFDM build / analyze at other geometries -----------------------------

@pytest.mark.parametrize("core,guard", [(128, 8), (256, 32), (512, 128),
                                         (1024, 0)])
def test_build_and_analyze_invert_each_other_at_any_geometry(core, guard):
    g = geometry(core=core, guard=guard, carriers=np.arange(3, core // 4))
    values = qpsk(g.carrier_count)
    symbol = ofdm.build_symbol(g, values)
    assert len(symbol) == g.symbol_samples
    assert ofdm.symbol_carriers(g, symbol) == pytest.approx(values, abs=1e-9)


def test_a_built_symbol_is_real_audio():
    g = geometry()
    symbol = ofdm.build_symbol(g, qpsk(g.carrier_count))
    assert symbol.dtype == np.float64
    assert np.all(np.isfinite(symbol))


def test_carrier_bank_reports_none_when_the_frame_runs_off_the_end():
    g = geometry()
    audio = np.zeros(3 * g.symbol_samples, dtype=np.complex128)
    assert ofdm.carrier_bank(g, audio, 0, 3) is not None
    assert ofdm.carrier_bank(g, audio, 0, 4) is None
    # Off the front, too -- far enough that the guard offset cannot
    # absorb it back into the buffer.
    assert ofdm.carrier_bank(g, audio, -10 * g.symbol_samples, 1) is None


def test_carrier_bank_follows_a_sloped_sample_clock():
    """Each symbol is read at intercept + slope*i, not at a fixed stride."""
    g = geometry()
    count = 6
    drift = 5  # samples of extra delay accumulated per symbol
    values = [qpsk(g.carrier_count) for _ in range(count)]
    audio = np.zeros(count * g.symbol_samples + count * drift + g.core_samples)
    for i, row in enumerate(values):
        at = i * g.symbol_samples + i * drift
        audio[at:at + g.symbol_samples] = ofdm.build_symbol(g, row)
    analytic = hilbert(audio)

    sloped = ofdm.carrier_bank(g, analytic, 0, count, slope=drift)
    straight = ofdm.carrier_bank(g, analytic, 0, count)
    expected = np.vstack(values)

    def alignment(bank):
        """How well the bank's phases match the transmitted symbols."""
        ratio = bank / (expected * g.time_scale)
        return float(np.mean(np.abs(np.mean(
            ratio / np.abs(ratio), axis=1))))

    # Following the drift keeps every symbol coherent; ignoring it lets
    # the FFT window walk off the symbols it is meant to be reading.
    assert alignment(sloped) > 0.99
    assert alignment(straight) < alignment(sloped)


# -- interleavers ---------------------------------------------------------

@pytest.mark.parametrize("size,stride", [(23_084, 8101), (100, 7), (17, 5)])
def test_multiplicative_interleaver_is_a_permutation(size, stride):
    assert interleave.multiplicative(size, stride).is_valid()


def test_multiplicative_interleaver_rejects_a_non_coprime_stride():
    """A shared factor collapses the mapping and silently drops bits."""
    with pytest.raises(ValueError):
        interleave.multiplicative(100, 10)


@pytest.mark.parametrize("rows,columns", [(4, 5), (37, 11), (1, 9), (9, 1)])
def test_block_interleaver_is_a_permutation(rows, columns):
    assert interleave.block(rows, columns).is_valid()


def test_block_interleaver_separates_neighbouring_bits_by_its_row_count():
    """The property that makes burst length a design parameter."""
    rows, columns = 8, 5
    order = interleave.block(rows, columns)
    # Where each original bit ends up after spreading.
    landing = order.inverse
    for i in range(rows * columns - 1):
        assert abs(int(landing[i + 1]) - int(landing[i])) >= min(rows, columns)


@pytest.mark.parametrize("order", [
    interleave.multiplicative(1_000, 7),
    interleave.block(25, 40),
])
def test_interleavers_round_trip_hard_and_soft_values(order):
    hard = RNG.integers(0, 2, order.size).astype(np.uint8)
    assert np.array_equal(order.gather(order.spread(hard)), hard)
    soft = RNG.normal(0, 1, order.size)
    assert np.array_equal(order.gather(order.spread(soft)), soft)
    assert order.gather(order.spread(soft)).dtype == soft.dtype


def test_interleavers_reject_a_wrong_length_input():
    order = interleave.block(4, 5)
    with pytest.raises(ValueError):
        order.spread(np.zeros(19))


def test_a_block_interleaver_beats_a_burst_the_code_alone_cannot():
    """The reason the kernel exists: spread a burst below the code's limit."""
    rows, columns = 64, 40
    size = rows * columns
    order = interleave.block(rows, columns)
    codec = framing.PacketCodec(payload_bits=size, interleaver=order,
                                whitener_seed=0x17E35)
    payload = bytes(RNG.integers(0, 256, codec.max_payload_bytes,
                                 dtype=np.uint8))
    coded = codec.encode(payload)

    burst = coded.copy()
    burst[500:500 + rows] ^= 1
    assert codec.decode_hard(burst)[0] == payload

    # Without the interleaver the same burst lands contiguously on the
    # code and is not correctable.
    plain = framing.PacketCodec(
        payload_bits=size, interleaver=interleave.Interleaver(
            np.arange(size, dtype=np.int64)), whitener_seed=0x17E35)
    contiguous = plain.encode(payload).copy()
    contiguous[500:500 + rows] ^= 1
    assert plain.decode_hard(contiguous)[0] != payload


# -- the convolutional code ----------------------------------------------

def test_hard_and_soft_decoders_agree_on_clean_coded_bits():
    information = np.zeros(400, dtype=np.uint8)
    information[:394] = RNG.integers(0, 2, 394)
    coded = dsp.K7.encode(information)
    assert np.array_equal(dsp.K7.decode_hard(coded), information)
    soft = 1.0 - 2.0 * coded.astype(np.float64)
    assert np.array_equal(dsp.K7.decode_soft(soft), information)


def test_soft_decoding_beats_hard_decoding_on_a_noisy_codeword():
    """The point of carrying reliabilities instead of throwing them away."""
    information = np.zeros(2_000, dtype=np.uint8)
    information[:1_994] = RNG.integers(0, 2, 1_994)
    coded = dsp.K7.encode(information)
    noisy = (1.0 - 2.0 * coded.astype(np.float64)) + RNG.normal(
        0, 1.1, len(coded))
    hard_errors = np.count_nonzero(
        dsp.K7.decode_hard((noisy < 0).astype(np.uint8)) != information)
    soft_errors = np.count_nonzero(dsp.K7.decode_soft(noisy) != information)
    assert soft_errors < hard_errors


def test_the_code_is_parameterized_not_hardcoded():
    """A shorter constraint length must still encode and decode."""
    code = fec.ConvolutionalCode(polynomials=(0o7, 0o5), constraint=3)
    assert code.states == 4 and code.tail_bits == 2
    information = np.zeros(200, dtype=np.uint8)
    information[:198] = RNG.integers(0, 2, 198)
    coded = code.encode(information)
    assert np.array_equal(code.decode_hard(coded), information)
    assert np.array_equal(code.decode_soft(1.0 - 2.0 * coded), information)


@pytest.mark.parametrize("decode", ["decode_hard", "decode_soft"])
def test_decoders_reject_an_odd_coded_bit_count(decode):
    with pytest.raises(ValueError):
        getattr(dsp.K7, decode)(np.zeros(11))


# -- the payload codec at other sizes -------------------------------------

def test_codec_dimensions_follow_from_the_frame_size():
    codec = framing.PacketCodec(
        payload_bits=23_084,
        interleaver=interleave.multiplicative(23_084, 8101),
        whitener_seed=0x17E35)
    assert codec.information_bits == 11_542
    assert codec.packet_bytes == 1_442
    assert codec.unused_information_bits == 0
    assert codec.max_payload_bytes == 1_436


def test_codec_rejects_an_interleaver_of_the_wrong_width():
    with pytest.raises(ValueError):
        framing.PacketCodec(payload_bits=1_000,
                            interleaver=interleave.multiplicative(999, 7),
                            whitener_seed=1)


def test_codec_rejects_a_frame_too_small_to_carry_a_packet():
    with pytest.raises(ValueError):
        framing.PacketCodec(payload_bits=8,
                            interleaver=interleave.multiplicative(8, 3),
                            whitener_seed=1)


def test_whitening_actually_breaks_up_a_constant_payload():
    """An all-zero payload must not become an all-zero carrier grid."""
    codec = framing.PacketCodec(
        payload_bits=4_000, interleaver=interleave.multiplicative(4_000, 7),
        whitener_seed=0x17E35)
    coded = codec.encode(bytes(codec.max_payload_bytes))
    assert 0.4 < float(np.mean(coded)) < 0.6


# -- acquisition ----------------------------------------------------------

def test_correlate_repeat_peaks_on_a_repeated_block():
    g = geometry()
    symbol = ofdm.build_symbol(g, qpsk(g.carrier_count))
    signal = hilbert(np.concatenate((RNG.normal(0, 1e-3, 500),
                                     np.tile(symbol, 4))))
    scores, rms = correlate = acquire.correlate_repeat(
        signal, g.symbol_samples, 2 * g.symbol_samples)
    assert np.max(scores) > 0.99
    assert len(scores) == len(rms)


def test_acquire_declines_a_signal_shorter_than_its_span():
    g = geometry()
    assert acquire.acquire(g, np.zeros(10, dtype=np.complex128),
                           sync_symbols=5) == (None, 0.0)


def test_acquire_declines_pure_silence():
    g = geometry()
    silence = np.zeros(20 * g.symbol_samples, dtype=np.complex128)
    assert acquire.acquire(g, silence, sync_symbols=5) == (None, 0.0)


def test_acquire_ranking_chooses_between_separated_candidates():
    """The seam that keeps acquisition off periodic energy that is not
    the header.

    Ranking scores one candidate per *contiguous* run above threshold, so
    what it arbitrates between is separated bursts of periodic energy --
    which is exactly the situation it exists for.  It cannot move the
    answer within a single plateau, and is not asked to.
    """
    rng = np.random.default_rng(11)
    g = geometry()
    strong = ofdm.build_symbol(
        g, bits.qpsk_from_bits(rng.integers(0, 2, 2 * g.carrier_count)))
    weak = strong * 0.5
    gap = np.zeros(4 * g.symbol_samples)
    signal = hilbert(np.concatenate(
        (np.tile(strong, 8), gap, np.tile(weak, 8))))
    second_burst = 8 * g.symbol_samples + len(gap)

    # Which burst wins unranked is not the point, and is not obvious --
    # the correlation is amplitude-normalized, so the quieter burst
    # scores just as well as the louder one.  That is precisely why a
    # mode supplies a scorer.  Steer it either way and it goes.
    early, _ = acquire.acquire(
        g, signal, sync_symbols=5,
        rank=lambda start: float(start < second_burst))
    late, _ = acquire.acquire(
        g, signal, sync_symbols=5,
        rank=lambda start: float(start >= second_burst))
    assert early < second_burst <= late


# -- timing ---------------------------------------------------------------

def test_timing_finds_a_constant_offset():
    # The search window must stay well inside the guard: at a shift of a
    # whole guard the prefix window lands on the neighbouring symbol's
    # prefix and correlates just as well, which is ambiguous by
    # construction rather than a failure of the estimator.
    rng = np.random.default_rng(12)
    # VF3's own proportions: a 128-sample guard searched +-32.  A short
    # guard over few carriers gives a correlation too shallow to locate a
    # boundary to within a sample, which is a property of the geometry
    # rather than of the estimator.
    g = geometry(core=1024, guard=128, carriers=np.arange(10, 68))
    symbols = np.concatenate([
        ofdm.build_symbol(g, bits.qpsk_from_bits(
            rng.integers(0, 2, 2 * g.carrier_count))) for _ in range(8)])
    offset = 7
    signal = hilbert(np.concatenate((np.zeros(offset), symbols)))
    fit = timing.estimate(g, signal, 0, np.arange(1, 7))
    assert fit.intercept == pytest.approx(offset, abs=1.5)
    assert fit.slope == pytest.approx(0.0, abs=0.4)
    assert fit.confidence > 0.9


def test_timing_follows_a_drifting_sample_clock():
    rng = np.random.default_rng(13)
    g = geometry(core=1024, guard=128, carriers=np.arange(10, 68))
    drift = 2  # samples per symbol
    count = 10
    audio = np.zeros(count * (g.symbol_samples + drift) + g.core_samples)
    for i in range(count):
        at = i * g.symbol_samples + i * drift
        audio[at:at + g.symbol_samples] = ofdm.build_symbol(
            g, bits.qpsk_from_bits(rng.integers(0, 2, 2 * g.carrier_count)))
    fit = timing.estimate(g, hilbert(audio), 0, np.arange(1, count - 1))
    assert fit.slope == pytest.approx(drift, abs=0.5)


def test_timing_reports_zero_slope_on_a_signal_with_no_prefix():
    g = geometry(guard=0)
    fit = timing.estimate(g, hilbert(RNG.normal(0, 1, 20 * g.symbol_samples)),
                          0, np.arange(1, 7))
    assert fit.slope == 0.0 and fit.confidence == 0.0


def test_clock_offset_converts_slope_to_parts_per_million():
    g = geometry(core=1024, guard=128)
    fit = timing.TimingFit(intercept=0.0, slope=0.001152, confidence=1.0)
    assert fit.clock_offset_ppm(g) == pytest.approx(1.0)
    assert fit.shift_at(0) == 0
    assert fit.drift_samples(101) == pytest.approx(0.1152)


# -- frequency offset -----------------------------------------------------

def _offset_frame(offset_hz, seed=14):
    rng = np.random.default_rng(seed)
    g = geometry(core=1024, guard=128, carriers=np.arange(10, 68))
    reference = np.vstack([
        bits.qpsk_from_bits(rng.integers(0, 2, 2 * g.carrier_count))
        for _ in range(10)])
    audio = np.concatenate([ofdm.build_symbol(g, row) for row in reference])
    shifted = freq.derotate(hilbert(audio), -offset_hz, g.sample_rate)
    return g, reference, shifted


@pytest.mark.parametrize("offset_hz", [0.0, 0.5, 3.0, -7.5, 15.0])
def test_coarse_and_fine_offsets_recover_an_injected_shift(offset_hz):
    g, reference, shifted = _offset_frame(offset_hz)
    coarse = freq.coarse_offset_hz(g, shifted, 0, np.arange(1, 10))
    fine = freq.fine_offset_hz(
        g, ofdm.carrier_bank(g, shifted, 0, 10), reference)
    # Both stay within ~0.5% of a carrier spacing across the range.
    tolerance = 0.005 * g.carrier_spacing_hz
    assert coarse == pytest.approx(offset_hz, abs=tolerance)
    assert fine == pytest.approx(offset_hz, abs=tolerance)


@pytest.mark.parametrize("offset_hz", [0.0, 0.3, -0.8])
def test_the_fine_estimator_earns_its_name_near_zero(offset_hz):
    """Fine is more precise once the offset is small -- which is why it
    belongs after a coarse correction, not instead of one.  Further out,
    inter-carrier leakage biases it and the ordering does not hold."""
    g, reference, shifted = _offset_frame(offset_hz)
    coarse = freq.coarse_offset_hz(g, shifted, 0, np.arange(1, 10))
    fine = freq.fine_offset_hz(
        g, ofdm.carrier_bank(g, shifted, 0, 10), reference)
    assert abs(fine - offset_hz) < abs(coarse - offset_hz)


def test_derotate_undoes_an_offset_and_respects_its_start_sample():
    g = geometry()
    signal = hilbert(RNG.normal(0, 1, 4_000))
    shifted = freq.derotate(signal, -5.0, g.sample_rate)
    assert freq.derotate(shifted, 5.0, g.sample_rate) == pytest.approx(signal)
    # A slice must stay phase-consistent with the whole.
    whole = freq.derotate(signal, 5.0, g.sample_rate)
    part = freq.derotate(signal[1_000:], 5.0, g.sample_rate,
                         start_sample=1_000)
    assert part == pytest.approx(whole[1_000:])


def test_coarse_offset_reports_nothing_when_it_cannot_measure():
    g = geometry(guard=0)
    assert freq.coarse_offset_hz(g, np.zeros(10_000, dtype=np.complex128),
                                 0, np.arange(1, 5)) == 0.0


def test_fine_offset_needs_two_symbols_to_see_a_step():
    g = geometry()
    with pytest.raises(ValueError):
        freq.fine_offset_hz(g, np.ones((1, 4)), np.ones((1, 4)))


# -- equalization ---------------------------------------------------------

def test_header_fit_recovers_a_known_per_carrier_channel():
    carriers = 16
    reference = np.vstack([qpsk(carriers) for _ in range(12)])
    gain = RNG.uniform(0.3, 2.0, carriers) * np.exp(
        1j * RNG.uniform(-np.pi, np.pi, carriers))
    observed = reference * gain[None, :]
    fit = equalize.fit_header(observed, reference)
    assert fit.gain == pytest.approx(gain, abs=1e-9)
    assert fit.equalize(observed) == pytest.approx(reference, abs=1e-9)


def test_header_fit_separates_an_additive_interferer_from_the_gain():
    """The reason the fit has an offset term at all."""
    carriers = 16
    reference = np.vstack([qpsk(carriers) for _ in range(12)])
    gain = np.full(carriers, 0.8 + 0.2j)
    offset = np.full(carriers, 0.3 - 0.1j)
    fit = equalize.fit_header(reference * gain[None, :] + offset[None, :],
                              reference)
    assert fit.gain == pytest.approx(gain, abs=1e-9)
    assert fit.offset == pytest.approx(offset, abs=1e-9)


def test_header_fit_snr_falls_as_noise_rises():
    carriers = 16
    reference = np.vstack([qpsk(carriers) for _ in range(12)])
    clean = equalize.fit_header(reference, reference)
    noisy = equalize.fit_header(
        reference + RNG.normal(0, 0.2, reference.shape)
        + 1j * RNG.normal(0, 0.2, reference.shape), reference)
    assert np.median(clean.snr_db) > np.median(noisy.snr_db) + 10


def test_present_carriers_counts_only_what_is_within_the_floor():
    fit = equalize.ChannelFit(
        gain=np.array([1.0, 1.0, 1e-4, 1e-4], dtype=np.complex128),
        offset=np.zeros(4, dtype=np.complex128), snr_db=np.zeros(4))
    assert fit.present_carriers(35.0) == 2
    assert fit.present_carriers(100.0) == 4


def test_carrier_weights_discount_weak_carriers_within_their_clip():
    snr_db = np.array([0.0, 0.0, 0.0, -40.0, 40.0])
    weights = equalize.carrier_weights(snr_db, low=0.5, high=2.0)
    assert weights[3] == pytest.approx(0.5)
    assert weights[4] == pytest.approx(2.0)
    assert weights[0] == pytest.approx(1.0)


def test_pilot_equalizer_tracks_a_phase_that_drifts_through_the_frame():
    """What a header-only fit cannot do, and an HF path demands."""
    symbols, carriers = 60, 16
    pilot_positions = np.arange(9, symbols, 10)
    reference = np.vstack([qpsk(carriers) for _ in range(symbols)])
    pilot_values = qpsk(carriers)
    sent = reference.copy()
    sent[pilot_positions] = pilot_values
    initial_reference = qpsk(carriers)

    # A phase that walks a full radian across the frame.
    drift = np.linspace(0.0, 1.0, symbols)[:, None] * np.ones(carriers)[None, :]
    received = sent * np.exp(1j * drift)

    corrected, track = equalize.pilot_phase(
        received, pilot_positions, pilot_values, initial_reference,
        initial_reference)
    assert track.shape == (symbols, carriers)
    # Between the anchors the interpolation is not exact, but it must beat
    # leaving the drift in place by a wide margin.
    residual = np.max(np.abs(np.angle(corrected * np.conj(sent))))
    assert residual < 0.02
    assert residual < np.max(np.abs(drift)) / 10


def test_pilot_equalizer_is_exact_at_its_anchors():
    symbols, carriers = 40, 8
    pilot_positions = np.arange(4, symbols, 5)
    pilot_values = qpsk(carriers)
    sent = np.vstack([qpsk(carriers) for _ in range(symbols)])
    sent[pilot_positions] = pilot_values
    initial = qpsk(carriers)
    rotation = np.exp(1j * 0.7)
    corrected, _ = equalize.pilot_phase(
        sent * rotation, pilot_positions, pilot_values, initial * rotation,
        initial)
    assert corrected[pilot_positions] == pytest.approx(
        sent[pilot_positions], abs=1e-9)


# -- differential QPSK ----------------------------------------------------

def test_differential_round_trips_at_an_arbitrary_shape():
    symbols, carriers = 30, 12
    data = RNG.integers(0, 2, symbols * carriers * 2).astype(np.uint8)
    initial = qpsk(carriers)
    values = differential.encode(data, initial, symbols, carriers)
    observed = differential.observations(values, initial)
    assert np.array_equal(differential.hard_bits(observed), data)
    assert differential.decisions(observed) == pytest.approx(observed,
                                                             abs=1e-9)


def test_differential_soft_bits_are_larger_when_the_symbol_is_cleaner():
    carriers = 8
    initial = qpsk(carriers)
    data = RNG.integers(0, 2, 10 * carriers * 2).astype(np.uint8)
    values = differential.encode(data, initial, 10, carriers)
    clean = differential.observations(values, initial)
    noisy = differential.observations(
        values + RNG.normal(0, 0.4, values.shape), initial)
    weights = np.ones(carriers)
    assert (np.mean(np.abs(differential.soft_bits(clean, weights)))
            > np.mean(np.abs(differential.soft_bits(noisy, weights))))


# -- head measurement -----------------------------------------------------

def test_head_measure_counts_whole_repeated_blocks():
    reference = RNG.normal(0, 1, 512)
    samples = np.tile(reference, 6)
    count, score = head.measure(samples, len(samples), reference)
    assert count == 6 and score > 0.99


def test_head_measure_stops_at_silence_and_at_the_buffer_start():
    reference = RNG.normal(0, 1, 512)
    samples = np.concatenate((np.zeros(2 * 512), np.tile(reference, 3)))
    assert head.measure(samples, len(samples), reference)[0] == 3
    assert head.measure(np.tile(reference, 3), 3 * 512, reference)[0] == 3


def test_head_measure_ignores_a_block_that_is_mostly_missing():
    """A part-silent block correlates well but did not arrive whole."""
    reference = RNG.normal(0, 1, 512)
    samples = np.concatenate((np.tile(reference, 2), np.tile(reference, 3)))
    samples[512:1024] *= 0.1  # a block that is present but far too quiet
    assert head.measure(samples, len(samples), reference)[0] == 3


def test_head_measure_reports_nothing_for_a_signal_that_is_not_the_head():
    reference = RNG.normal(0, 1, 512)
    count, score = head.measure(RNG.normal(0, 1, 4 * 512), 4 * 512, reference)
    assert count == 0 and score < head.MATCH_THRESHOLD


def test_head_measure_holds_the_alignment_exactly_by_default():
    """One sample of slip ends the count when no tolerance is asked for.

    This is VF3's behaviour and the reason `phase_tolerance` defaults to 0:
    a mode measuring the audio as received wants the strict check.
    """
    reference = RNG.normal(0, 1, 512)
    samples = np.concatenate((np.tile(reference, 3), np.roll(reference, 1),
                              np.tile(reference, 2)))
    assert head.measure(samples, len(samples), reference)[0] == 2


def test_head_measure_can_follow_an_alignment_that_drifts():
    """What a frequency-corrected mode needs.

    Correcting an offset multiplies the capture by a slow phase ramp, which
    walks the correlation peak by a sample every so often -- a drift, not a
    discontinuity.  With a tolerance the count follows it.
    """
    reference = RNG.normal(0, 1, 512)
    # Newest block first: the walk goes backwards from `start`, so the
    # alignment slips by one sample every two blocks going back.
    blocks = [np.roll(reference, -(i // 2)) for i in range(6)]
    samples = np.concatenate(list(reversed(blocks)))
    # Strictly, the count stops at the first slip -- two blocks in.
    assert head.measure(samples, len(samples), reference)[0] == 2
    assert head.measure(samples, len(samples), reference,
                        phase_tolerance=1)[0] == 6


def test_a_tolerance_still_stops_at_a_real_discontinuity():
    """The tolerance must not turn the phase check off.

    A block that is the reference at a wholly different alignment is the
    "the head ended and something else correlates here" case the check
    exists for, and it jumps rather than drifts.
    """
    reference = RNG.normal(0, 1, 512)
    samples = np.concatenate((np.tile(reference, 2), np.roll(reference, 200),
                              np.tile(reference, 3)))
    assert head.measure(samples, len(samples), reference,
                        phase_tolerance=1)[0] == 3


# -- MFSK -----------------------------------------------------------------

def tone_bank(symbol=512, first=8, tones=16):
    return mfsk.ToneBank(sample_rate=48_000, symbol_samples=symbol,
                         first_bin=first, tone_count=tones)


def test_tone_bank_derives_one_number_three_ways():
    """Symbol rate, tone spacing and FFT bin width are the same quantity.

    That is what orthogonal non-coherent MFSK forces, and it is why a tone
    is an exact bin rather than something to interpolate for.
    """
    bank = tone_bank()
    assert bank.symbol_rate == bank.spacing_hz == 93.75
    assert bank.bits_per_symbol == 4
    assert bank.bandwidth_hz == 1_500.0
    assert bank.tone_hz[0] == 750.0 and bank.tone_hz[-1] == 2_156.25
    assert bank.offset_limit_hz == 46.875


@pytest.mark.parametrize("bad", [
    dict(tones=12),                 # not a power of two
    dict(first=0),                  # DC
    dict(first=250, tones=16),      # past Nyquist of a 512-sample symbol
])
def test_tone_bank_refuses_unusable_geometry(bad):
    with pytest.raises(ValueError):
        tone_bank(**bad)


def test_gray_mapping_round_trips_and_neighbours_differ_by_one_bit():
    bank = tone_bank()
    bits = RNG.integers(0, 2, 40 * bank.bits_per_symbol).astype(np.uint8)
    tones = bank.symbols_from_bits(bits)
    assert np.array_equal(bank.bits_from_symbols(tones), bits)
    # The error the channel actually makes is reading a tone as its
    # neighbour; Gray coding is what keeps that to one bit.
    labels = bank.bits_from_symbols(np.arange(bank.tone_count))
    labels = labels.reshape(bank.tone_count, bank.bits_per_symbol)
    for i in range(bank.tone_count - 1):
        assert np.count_nonzero(labels[i] != labels[i + 1]) == 1


def test_modulation_is_phase_continuous():
    """Every tone is a whole number of cycles per symbol, so a symbol
    boundary is not a discontinuity and nothing has to be carried across
    it: two symbols of one tone are exactly that tone, run on."""
    bank = tone_bank()
    audio = mfsk.modulate(bank, [3, 3, 11, 0, 15])
    assert len(audio) == 5 * bank.symbol_samples
    span = 2 * bank.symbol_samples
    single = np.cos(2 * np.pi * bank.bins[3] * np.arange(span)
                    / bank.symbol_samples)
    assert np.allclose(audio[:span], single, atol=1e-12)


def test_modulation_is_constant_envelope():
    """One tone at a time, so a peak-limited transmitter can be driven
    much harder than an OFDM waveform allows."""
    bank = tone_bank()
    audio = mfsk.modulate(bank, RNG.integers(0, bank.tone_count, 40))
    crest = np.max(np.abs(audio)) / np.sqrt(np.mean(audio ** 2))
    assert crest == pytest.approx(np.sqrt(2.0), abs=0.02)


def test_analyze_recovers_the_transmitted_tone():
    bank = tone_bank()
    tones = RNG.integers(0, bank.tone_count, 20)
    values = mfsk.analyze(bank, mfsk.modulate(bank, tones), 0, len(tones))
    assert np.array_equal(np.argmax(np.abs(values), axis=1), tones)


def test_analyze_takes_a_frequency_hypothesis():
    bank = tone_bank()
    tones = RNG.integers(0, bank.tone_count, 20)
    audio = mfsk.modulate(bank, tones)
    index = np.arange(len(audio))
    shifted = np.real(hilbert(audio) * np.exp(2j * np.pi * 40.0 * index / 48_000))

    def matched(values):
        return float(np.sum(np.abs(values)[np.arange(len(tones)), tones]))

    uncorrected = mfsk.analyze(bank, shifted, 0, len(tones))
    corrected = mfsk.analyze(bank, shifted, 0, len(tones), offset_hz=40.0)
    assert matched(corrected) > 1.3 * matched(uncorrected)


def test_analyze_declines_a_window_that_runs_off_the_end():
    bank = tone_bank()
    assert mfsk.analyze(bank, np.zeros(1_000), 0, 4) is None


def test_soft_bits_are_signed_by_the_transmitted_bit():
    bank = tone_bank()
    bits = RNG.integers(0, 2, 30 * bank.bits_per_symbol).astype(np.uint8)
    tones = bank.symbols_from_bits(bits)
    values = mfsk.analyze(bank, mfsk.modulate(bank, tones), 0, len(tones))
    soft = mfsk.soft_bits(bank, np.abs(values))
    assert np.array_equal((soft < 0).astype(np.uint8), bits)


def test_soft_bits_ignore_the_receiver_gain():
    """Only the contrast between the tones in one symbol carries
    information, which is what makes this indifferent to an AGC."""
    bank = tone_bank()
    tones = RNG.integers(0, bank.tone_count, 12)
    values = np.abs(mfsk.analyze(bank, mfsk.modulate(bank, tones), 0, len(tones)))
    assert np.allclose(mfsk.soft_bits(bank, values),
                       mfsk.soft_bits(bank, 17.0 * values))


def test_correlate_finds_a_pattern_and_stays_quiet_on_noise():
    bank = tone_bank()
    pattern = RNG.integers(0, bank.tone_count, 24)
    audio = np.concatenate((RNG.normal(0, 0.05, 3_000),
                            mfsk.modulate(bank, pattern, 0.18),
                            RNG.normal(0, 0.05, 3_000)))
    scores, step = mfsk.correlate(bank, audio, pattern)
    assert abs(int(np.argmax(scores)) * step - 3_000) <= step
    assert np.max(scores) > 0.4

    noise_only, _ = mfsk.correlate(bank, RNG.normal(0, 0.05, 60_000), pattern)
    assert np.max(noise_only) < 0.15


def test_the_across_tone_mean_is_what_keeps_noise_from_scoring():
    """The bug `experiments/mfsk` records paying for.

    Tone magnitudes are all non-negative, so every channel carries a large
    common component; correlating them raw scores that against itself and
    reads pure noise as a lock. Removing the across-tone mean at each
    instant makes the channels sum to zero and the score collapse.
    """
    bank = tone_bank()
    pattern = RNG.integers(0, bank.tone_count, 24)
    noise = RNG.normal(0, 0.05, 60_000)
    step = bank.symbol_samples // mfsk.SEARCH_DIVISOR
    centred, _ = mfsk.correlate(bank, noise, pattern)

    magnitudes, _ = mfsk._magnitude_grid(bank, noise, step)
    per = bank.symbol_samples // step
    count = len(magnitudes) - (len(pattern) - 1) * per
    hit = sum(magnitudes[i * per:i * per + count, tone]
              for i, tone in enumerate(pattern))
    total = sum(np.sum(magnitudes[i * per:i * per + count], axis=1)
                for i in range(len(pattern)))
    raw = hit / total
    # The mechanism, stated exactly: the raw statistic has a floor at 1/M
    # that is there whatever the input, because it is scoring the common
    # component against itself. The centred one is zero-mean on noise, so
    # every part of its score is signal.
    assert np.mean(raw) == pytest.approx(1.0 / bank.tone_count, rel=0.15)
    assert abs(np.mean(centred)) < 0.01
    assert np.max(raw) > np.max(centred)


def test_refine_finds_the_boundary_the_score_cannot():
    """`pattern_score` saturates: once the right tone dominates every
    symbol it reads its ceiling whatever the timing. Matched tone energy
    has an actual maximum at the symbol boundary."""
    bank = tone_bank()
    pattern = RNG.integers(0, bank.tone_count, 24)
    audio = np.concatenate((np.zeros(3_000),
                            mfsk.modulate(bank, pattern, 0.18),
                            np.zeros(3_000)))

    flat = [mfsk.pattern_score(bank, audio, pattern, 3_000 + d)
            for d in (-48, 0, 48)]
    assert max(flat) - min(flat) < 1e-6          # no timing information at all
    assert mfsk.refine(bank, audio, pattern, 3_000 - 96, radius=128) == 3_000


def test_offset_is_measured_from_repeated_pairs_at_any_timing():
    """A symbol's phase carries a timing term that depends on which tone it
    used, so only a pair sharing a tone gives an estimate that a timing
    error cannot corrupt."""
    bank = tone_bank()
    pattern = np.repeat(RNG.integers(0, bank.tone_count, 12), 2)
    # Every deliberate pair, and possibly more where the draw happened to
    # put the same tone in two neighbouring pairs -- which is not a problem
    # but a longer run of the same measurement.
    pairs = mfsk.repeated_pairs(pattern)
    assert set(range(0, 24, 2)) <= set(pairs.tolist())

    audio = mfsk.modulate(bank, pattern)
    index = np.arange(len(audio))
    shifted = np.real(hilbert(audio) * np.exp(2j * np.pi * 11.0 * index / 48_000))
    padded = np.concatenate((np.zeros(1_000), shifted, np.zeros(1_000)))
    for error in (-48, 0, 48):
        assert mfsk.offset_hz(bank, padded, 1_000 + error,
                              pattern) == pytest.approx(11.0, abs=0.5)


def test_a_pattern_without_repeats_declines_to_estimate():
    """Zero is a mode saying it does not want the measurement, not a
    failure -- there is no pair to take a phase step across."""
    bank = tone_bank()
    pattern = np.arange(8)
    assert not len(mfsk.repeated_pairs(pattern))
    assert mfsk.offset_hz(bank, mfsk.modulate(bank, pattern), 0, pattern) == 0.0
