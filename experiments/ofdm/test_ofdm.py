"""Pure-software checks for experiments/ofdm/ofdm.py -- no hardware, no radios.

These are invariants, not evidence about the radios. Everything here passing
means the modem is self-consistent and the candidate generator respects the
constraints it claims to; whether any candidate survives the FM audio chain is
sweep_ofdm.py's question, and the repo has plenty of precedent for a mode that
is perfect in software and 0/6 on air.

Two of these tests are doing more than checking arithmetic, and are the reason
this file is worth reading:

  - test_dispersion_the_prefix_covers_costs_nothing is the whole argument for
    OFDM on this bench, reduced to an assertion. If it ever fails, the mode
    has no reason to exist.
  - test_no_false_sync_on_off_air_captures and test_a_pure_tone_does_not_sync
    are the ones guarding a failure this repo has now made twice with two
    different sync measures. See the sync section of ofdm.py's docstring.

Run: python experiments/ofdm/test_ofdm.py
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

import ofdm
import ldpc
from whale import afsk, framing

# The QPSK working point most tests use: 50 Hz spacing, an eighth of a symbol
# of cyclic prefix. Nothing about it is special beyond being in the middle of
# the candidate space.
QPSK = ofdm.OfdmProfile(name="test_qpsk", n_fft=960, cp=120, bits_per_carrier=2)

# One per constellation, for the tests that only care that the modem works
# rather than about a particular setting.
REPRESENTATIVE = [
    ofdm.OfdmProfile(name=f"test_{1 << b}", n_fft=960, cp=120, bits_per_carrier=b)
    for b in (1, 2, 3, 4, 5)
]

CAPTURE_DIR = Path(__file__).resolve().parents[2] / "scratch_captures_600ack"


def _rng(seed=20260818):
    return np.random.default_rng(seed)


def _noisy(audio, snr_db, rng):
    """AWGN at `snr_db` relative to the signal's own RMS."""
    audio = np.asarray(audio, dtype=np.float64)
    sigma = np.sqrt(np.mean(audio ** 2)) * 10 ** (-snr_db / 20)
    return audio + rng.normal(0.0, sigma, len(audio))


def _dispersive(audio, taps):
    """Convolve with a normalised impulse response. `taps` is (delay_samples,
    amplitude) pairs -- a crude but honest stand-in for the multipath-free,
    filter-induced smearing an FM audio chain applies."""
    length = max(d for d, _ in taps) + 1
    h = np.zeros(length)
    for delay, amp in taps:
        h[delay] += amp
    h /= np.linalg.norm(h)
    return np.convolve(np.asarray(audio, dtype=np.float64), h)[:len(audio)]


def _symbol_varying_phase(audio, profile, radians_per_symbol):
    """Apply a changing all-carrier phase to each complete OFDM symbol.

    This is a controlled stand-in for the within-frame channel movement seen
    in the on-air diagnostic capture. Rebuilding each prefix keeps the test
    about tracking rather than introducing an accidental discontinuity/ISI.
    """
    out = np.asarray(audio, dtype=np.float64).copy()
    start = ofdm._pad_samples(ofdm.HEAD_PAD_SECONDS)
    index = 0
    while start + profile.symbol_samples <= len(out) - ofdm._pad_samples(ofdm.TAIL_PAD_SECONDS):
        useful = out[start + profile.cp:start + profile.symbol_samples]
        spectrum = np.fft.rfft(useful)
        # Hold acquisition and initial training fixed; move only the stream
        # they are being asked to predict.
        age = max(0, index - profile.n_train)
        spectrum[1:-1] *= np.exp(1j * radians_per_symbol * age)
        moved = np.fft.irfft(spectrum, n=profile.n_fft)
        out[start:start + profile.symbol_samples] = np.concatenate(
            [moved[-profile.cp:], moved])
        start += profile.symbol_samples
        index += 1
    return out


def test_soft_16qam_llrs_are_code_independent():
    points = ofdm.constellation(4)
    llrs = ofdm.qam_llrs(points, 4, noise_variance=0.05).reshape(16, 4)
    labels = ((np.arange(16)[:, None] >> np.arange(3, -1, -1)) & 1)
    assert np.array_equal(llrs < 0, labels)


def test_standard_ldpc_corrects_errors_and_has_zero_syndrome():
    rng = _rng(81)
    information = rng.integers(0, 2, ldpc.K, dtype=np.uint8)
    codeword = ldpc.encode(information)
    assert not np.any(ldpc.syndrome(codeword))
    damaged = codeword.copy()
    damaged[rng.choice(ldpc.N, 18, replace=False)] ^= 1
    decoded, _, ok = ldpc.decode((1.0 - 2.0 * damaged) * 3.0)
    assert ok and np.array_equal(decoded, information)


def test_standard_rate_two_thirds_ldpc_corrects_errors():
    rng = _rng(83)
    k = ldpc.INFORMATION_BITS["2/3"]
    information = rng.integers(0, 2, k, dtype=np.uint8)
    codeword = ldpc.encode(information, "2/3")
    assert not np.any(ldpc.syndrome(codeword, "2/3"))
    damaged = codeword.copy()
    damaged[rng.choice(ldpc.N, 12, replace=False)] ^= 1
    decoded, _, ok = ldpc.decode((1.0 - 2.0 * damaged) * 3.0, rate="2/3")
    assert ok and np.array_equal(decoded, information)


def test_ldpc_16qam_roundtrip_on_synthetic_channels():
    profile = ofdm.OfdmProfile(name="test_16qam_ldpc12", n_fft=960, cp=120,
                               bits_per_carrier=4, fec="802.11n-648-1/2")
    rng = _rng(82)
    payload = rng.integers(0, 256, min(180, profile.max_payload), dtype=np.uint8).tobytes()
    audio = ofdm.modulate(payload, profile)
    assert ofdm.demodulate(audio, profile).get("payload") == payload
    channel = _dispersive(audio, [(0, 1.0), (48, 0.22), (91, -0.10)])
    channel = _noisy(channel, 24.0, rng)
    assert ofdm.demodulate(channel, profile).get("payload") == payload


def test_rate_two_thirds_16qam_roundtrip():
    profile = ofdm.OfdmProfile(name="test_16qam_ldpc23", n_fft=960, cp=120,
                               bits_per_carrier=4, fec="802.11n-648-2/3")
    rng = _rng(84)
    payload = rng.integers(0, 256, min(240, profile.max_payload), dtype=np.uint8).tobytes()
    audio = _noisy(ofdm.modulate(payload, profile), 27.0, rng)
    assert ofdm.demodulate(audio, profile).get("payload") == payload


def test_fec_length_search_does_not_treat_capture_tail_as_tracking_pilots():
    profile = ofdm.OfdmProfile(name="test_16qam_ldpc23_p4", n_fft=960, cp=120,
                               bits_per_carrier=4, pilot_interval=4,
                               fec="802.11n-648-2/3")
    rng = _rng(85)
    payload = rng.integers(0, 256, min(698, profile.max_payload), dtype=np.uint8).tobytes()
    audio = ofdm.modulate(payload, profile)
    # More than one pilot interval of post-frame audio used to create a false
    # channel anchor and corrupt the final two symbols during length search.
    capture = np.concatenate((audio, rng.normal(0, 0.01, 8 * profile.symbol_samples)))
    assert ofdm.demodulate(capture, profile).get("payload") == payload


def test_fec_roundtrip_preserves_variable_payload_lengths():
    """The search optimization must not turn the sweep's full-size payload
    into an accidental fixed-size wire format. Exercise both sides of two
    LDPC block boundaries: rate 2/3 carries 50 payload bytes in one block and
    104 in two once the four framing bytes are included."""
    profile = ofdm.OfdmProfile(name="test_16qam_ldpc23_variable", n_fft=960,
                               cp=120, bits_per_carrier=4, papr_db=30.0,
                               fec="802.11n-648-2/3")
    rng = _rng(86)
    for size in (0, 50, 51, 104, 105):
        payload = rng.integers(0, 256, size, dtype=np.uint8).tobytes()
        assert ofdm.demodulate(ofdm.modulate(payload, profile), profile).get(
            "payload") == payload, size


def test_fec_length_search_is_capped_at_the_profile_capacity():
    profile = ofdm.OfdmProfile(name="test_16qam_ldpc23_bound", n_fft=960,
                               cp=120, bits_per_carrier=4, pilot_interval=4,
                               fec="802.11n-648-2/3")
    k = ldpc.INFORMATION_BITS["2/3"]
    expected_max = -(-framing.frame_bits_for_length(profile.max_payload) // k)
    candidates = tuple(ofdm._fec_candidate_block_counts(
        profile, ofdm.max_credible_data_symbols(profile)))
    assert candidates == tuple(range(1, expected_max + 1))


def test_fec_length_candidate_stops_at_the_first_failed_codeword():
    profile = ofdm.OfdmProfile(name="test_16qam_ldpc23_fail_fast", n_fft=960,
                               cp=120, bits_per_carrier=4,
                               fec="802.11n-648-2/3")
    calls = []

    def reject(llr, rate):
        calls.append((len(llr), rate))
        return np.zeros(ldpc.INFORMATION_BITS[rate], dtype=np.uint8), 30, False

    with patch.object(ldpc, "decode", side_effect=reject):
        bits, iterations = ofdm._decode_fec_codewords(
            np.zeros(3 * ldpc.N), profile)

    assert bits is None
    assert iterations == [30]
    assert calls == [(ldpc.N, "2/3")]


# -- the modem is self-consistent -----------------------------------------


def test_constellations_are_gray_coded_and_unit_power():
    """Neighbouring points must differ in one bit, because neighbours are what
    a noisy decision confuses. Under CRC-only framing this changes nothing
    about whether a frame passes -- any single bit error fails it -- but it is
    what makes the symbol error rate the honest thing for diagnose_ofdm.py to
    report, and it makes adding FEC later a drop-in rather than a format
    change."""
    for bits in (1, 2, 3, 4, 5):
        points = ofdm.constellation(bits)
        assert len(points) == 1 << bits, bits
        assert len(set(np.round(points, 9))) == 1 << bits, f"{bits}: duplicate points"
        assert abs(np.mean(np.abs(points) ** 2) - 1.0) < 1e-9, f"{bits}: not unit power"
        if bits <= 3:
            # PSK: neighbours in angle. Walk the points in phase order.
            order = np.argsort(np.angle(points) % (2 * np.pi))
            for i in range(len(order)):
                a, b = int(order[i]), int(order[(i + 1) % len(order)])
                assert bin(a ^ b).count("1") == 1, (bits, a, b)
        elif bits == 4:
            # 16-QAM: neighbours along each axis independently.
            levels = np.round(points.real * np.sqrt(10)).astype(int)
            for value in range(16):
                right = np.flatnonzero((levels == levels[value] + 2)
                                       & (np.abs(points.imag - points[value].imag) < 1e-9))
                for other in right:
                    assert bin(value ^ int(other)).count("1") == 1, (value, other)
        else:
            # Cross-32-QAM: the four high-energy corners of a 6x6 grid are
            # absent. Perfect Gray coding is impossible; the optimal mapping
            # has only two two-bit nearest-neighbour edges per quadrant.
            scaled = np.round(points * np.sqrt(20)).astype(complex)
            assert set(np.abs(scaled.real)) == {1, 3, 5}
            assert set(np.abs(scaled.imag)) == {1, 3, 5}
            assert not any(abs(p.real) == 5 and abs(p.imag) == 5 for p in scaled)
            edges = []
            for value, point in enumerate(scaled):
                for other in range(value + 1, len(scaled)):
                    distance = abs(point.real - scaled[other].real) \
                        + abs(point.imag - scaled[other].imag)
                    if distance == 2:
                        edges.append(bin(value ^ other).count("1"))
            assert len(edges) == 52
            assert edges.count(1) == 44
            assert edges.count(2) == 8
    print("test_constellations_are_gray_coded_and_unit_power OK")


def test_the_whitening_sequence_is_a_full_period_m_sequence():
    """Verified by construction rather than trusted from a tap table, the same
    way framing._SYNC_TAPS are. A short-period sequence would still whiten,
    but it would repeat inside a long frame, and the point of whitening is
    that the transmitted symbols look independent of the payload."""
    period = (1 << ofdm._WHITENING_ORDER) - 1
    bits = ofdm._whitening_bits(period + 1)
    assert int(bits[:period].sum()) == 1 << (ofdm._WHITENING_ORDER - 1), "not balanced"
    assert bits[period] == bits[0], "does not wrap at the full period"
    longest = max(p.max_payload for p in ofdm.candidates())
    assert period > framing.frame_bits_for_length(longest), (
        "sequence repeats inside the largest frame in the candidate space")
    print("test_the_whitening_sequence_is_a_full_period_m_sequence OK")


def test_a_repetitive_payload_is_no_worse_than_a_random_one():
    """The bug whitening was added for, kept on file.

    An all-zero payload used to put constellation point 0 on every subcarrier
    -- identical phases across the band, which is an impulse. That symbol
    measured 17.1 dB of PAPR against a random one's 12.2, the clipper
    flattened it, and the frame failed with 3 symbol errors in 35 on a
    channel containing no noise, no dispersion and no radios.

    Note what would *not* have caught it: sweep_ofdm.py sends random payloads
    on purpose, so every bench trial would have passed while real traffic --
    which is full of zero padding and runs of 0xFF -- failed.
    """
    rng = _rng(43)
    for profile in REPRESENTATIVE:
        size = min(profile.max_payload, 400)
        random_papr = ofdm.measured_papr_db(
            ofdm.modulate(rng.integers(0, 256, size, dtype=np.uint8).tobytes(), profile))
        for payload in (b"", bytes(size), bytes([0xFF]) * size,
                        bytes([0x00, 0xFF]) * (size // 2)):
            audio = ofdm.modulate(payload, profile)
            papr = ofdm.measured_papr_db(audio)
            assert papr < random_papr + 1.0, (
                f"{profile.name}: repetitive payload reached {papr:.1f} dB PAPR")
            assert ofdm.demodulate(np.asarray(audio, dtype=np.float64),
                                   profile).get("payload") == payload, (
                f"{profile.name}: repetitive payload failed to decode")
    print("test_a_repetitive_payload_is_no_worse_than_a_random_one OK")


def test_roundtrip_in_clean_audio():
    """Empty, tiny, and full-budget payloads, at every constellation. The
    full-budget one is the case that matters -- it is the only size the mode
    is ever run at -- but the small ones catch symbol-padding arithmetic that
    a long frame would hide."""
    rng = _rng()
    for profile in REPRESENTATIVE:
        for size in (0, 1, 17, profile.max_payload):
            payload = rng.integers(0, 256, size, dtype=np.uint8).tobytes()
            result = ofdm.demodulate(ofdm.modulate(payload, profile), profile)
            assert result.get("payload") == payload, (profile.name, size)
    print("test_roundtrip_in_clean_audio OK")


def test_dft_spread_roundtrip_and_energy():
    """DFT spreading is an invertible, unitary change of data waveform."""
    from dataclasses import replace
    rng = _rng(3)
    for base in REPRESENTATIVE:
        profile = replace(base, name=f"{base.name}_dfts", dft_spread=True)
        for size in (0, 1, 17, profile.max_payload):
            payload = rng.integers(0, 256, size, dtype=np.uint8).tobytes()
            spread = ofdm.data_symbol_values(payload, profile)
            decisions = ofdm._despread_data(spread, profile)
            assert np.allclose(np.sum(np.abs(spread) ** 2, axis=1),
                               np.sum(np.abs(decisions) ** 2, axis=1))
            assert ofdm.demodulate(ofdm.modulate(payload, profile), profile).get(
                "payload") == payload
    print("test_dft_spread_roundtrip_and_energy OK")


def test_dft_spreading_reduces_unclipped_data_symbol_papr():
    """The new mode must change the peak statistics it was added to improve."""
    from dataclasses import replace
    rng = _rng(4)
    plain = replace(QPSK, papr_db=30.0)
    spread = replace(plain, name="test_qpsk_dfts", dft_spread=True)
    payload = rng.integers(0, 256, 300, dtype=np.uint8).tobytes()

    def symbol_paprs(profile):
        out = []
        for row in ofdm.data_symbol_values(payload, profile):
            wave = ofdm._symbol_audio(profile, profile.carriers, row)
            out.append(ofdm.measured_papr_db(wave))
        return np.asarray(out)

    ordinary = symbol_paprs(plain)
    dfts = symbol_paprs(spread)
    assert np.percentile(dfts, 95) < np.percentile(ordinary, 95) - 2.0, (
        np.percentile(ordinary, 95), np.percentile(dfts, 95))

    # Pads are part of the same globally normalised waveform and must not
    # restore the peaks removed from the data section.
    ordinary_frame = ofdm.modulate(payload, plain)
    spread_frame = ofdm.modulate(payload, spread)
    assert ofdm.measured_papr_db(spread_frame) < (
        ofdm.measured_papr_db(ordinary_frame) - 2.0)
    print("test_dft_spreading_reduces_unclipped_data_symbol_papr OK")


def test_dft_spread_roundtrip_with_excluded_physical_carriers():
    """A spectral hole changes capacity but not DFT spreading's invertibility."""
    from dataclasses import replace
    profile = replace(QPSK, name="test_qpsk_dfts_notch", dft_spread=True,
                      excluded_bands=((1700.0, 1800.0),))
    assert list(profile.carriers * profile.spacing) == [
        f for f in QPSK.carriers * QPSK.spacing if not 1700 <= f <= 1800]
    assert profile.n_carriers == QPSK.n_carriers - 3
    assert profile.max_payload < QPSK.max_payload
    payload = _rng(6).integers(0, 256, profile.max_payload, dtype=np.uint8).tobytes()
    assert ofdm.demodulate(ofdm.modulate(payload, profile), profile).get("payload") == payload
    print("test_dft_spread_roundtrip_with_excluded_physical_carriers OK")


def test_tracking_pilots_refresh_a_moving_channel():
    """Front-loaded EQ fails a channel that moves through a long frame;
    periodic full-band pilots interpolate it and retain the payload."""
    from dataclasses import replace
    rng = _rng(5)
    plain = replace(QPSK, pilot_interval=0)
    tracked = replace(QPSK, pilot_interval=8)
    payload = rng.integers(0, 256, 300, dtype=np.uint8).tobytes()
    rate = 0.04

    plain_audio = _symbol_varying_phase(ofdm.modulate(payload, plain), plain, rate)
    tracked_audio = _symbol_varying_phase(ofdm.modulate(payload, tracked), tracked, rate)
    assert ofdm.demodulate(plain_audio, plain).get("payload") != payload, \
        "the impairment is too weak to state what the pilots repair"
    assert ofdm.demodulate(tracked_audio, tracked).get("payload") == payload
    print("test_tracking_pilots_refresh_a_moving_channel OK")


def test_tracking_pilot_layout_and_budget_are_exact():
    from dataclasses import replace
    for interval in (4, 8, 16):
        profile = replace(QPSK, pilot_interval=interval)
        for n_data in range(0, 3 * interval + 2):
            physical = ofdm.physical_symbols_for_data(profile, n_data)
            assert physical == n_data + n_data // interval
            assert ofdm.data_symbols_from_physical(profile, physical) == n_data
        assert ofdm.keying_seconds(profile, profile.max_payload) \
            <= ofdm.MAX_KEYING_SECONDS + 1e-9
        assert ofdm.keying_seconds(profile, profile.max_payload + 20) \
            > ofdm.MAX_KEYING_SECONDS
    print("test_tracking_pilot_layout_and_budget_are_exact OK")


def test_random_payloads_roundtrip_at_full_size():
    """A fixed payload can pass on a decoder that reconstructs what it
    expects. These are random and different every trial, and checked
    byte-for-byte, which is also what sweep_ofdm.py sends over the air."""
    rng = _rng(99)
    for profile in ofdm.candidates(bits=(1, 2, 3))[:6]:
        for _ in range(2):
            payload = rng.integers(0, 256, profile.max_payload, dtype=np.uint8).tobytes()
            got = ofdm.demodulate(ofdm.modulate(payload, profile), profile).get("payload")
            assert got == payload, profile.name
    print("test_random_payloads_roundtrip_at_full_size OK")


def test_frame_is_found_when_it_does_not_start_at_the_buffer_edge():
    """A real capture has the frame somewhere in the middle of seconds of
    receiver noise, which is also the only case where the energy
    normalisation in _match_score matters."""
    rng = _rng(7)
    payload = rng.integers(0, 256, 200, dtype=np.uint8).tobytes()
    frame = ofdm.modulate(payload, QPSK).astype(np.float64)
    quiet = 0.02 * np.sqrt(np.mean(frame ** 2))
    buffer = np.concatenate([rng.normal(0, quiet, 48000), frame,
                             rng.normal(0, quiet, 24000)])
    result = ofdm.demodulate(buffer, QPSK)
    assert result.get("payload") == payload
    assert abs(result["start_index"] - (48000 + ofdm._pad_samples(ofdm.HEAD_PAD_SECONDS)
                                        + QPSK.cp)) <= 2, result["start_index"]
    print("test_frame_is_found_when_it_does_not_start_at_the_buffer_edge OK")


# -- the claims OFDM is being tried on ------------------------------------


def test_dispersion_the_prefix_covers_costs_nothing():
    """The entire argument for OFDM on this bench, as an assertion.

    A channel that smears each symbol across several milliseconds is what
    killed 1400 baud at tones 1200 baud was fine with, and what
    experiments/mfsk's post-mortem put its own cliff on. Here the same
    smearing is applied deliberately, and as long as it fits inside the cyclic
    prefix the frame decodes with no equaliser beyond one complex division per
    subcarrier.

    The second half of the test is what makes the first half meaningful:
    dispersion *longer* than the prefix does break the frame. If it did not,
    the prefix would not be doing anything and the test would be passing for
    the wrong reason.
    """
    rng = _rng(11)
    payload = rng.integers(0, 256, 300, dtype=np.uint8).tobytes()
    frame = ofdm.modulate(payload, QPSK)

    inside = [(0, 1.0), (QPSK.cp // 3, 0.6), (2 * QPSK.cp // 3, 0.35)]
    assert ofdm.demodulate(_dispersive(frame, inside), QPSK).get("payload") == payload, \
        "dispersion inside the prefix should cost nothing"

    outside = [(0, 1.0), (3 * QPSK.cp, 0.6)]
    assert ofdm.demodulate(_dispersive(frame, outside), QPSK).get("payload") != payload, \
        "dispersion past the prefix should break the frame -- if it does not, " \
        "the prefix is not what is carrying the test above"
    print("test_dispersion_the_prefix_covers_costs_nothing OK")


def test_a_timing_error_inside_the_prefix_costs_nothing():
    """The receiver never corrects timing, and does not need to. An FFT window
    placed early, anywhere inside the prefix, sees a cyclically rotated symbol
    -- a linear phase ramp across the subcarriers. The training symbol is read
    through the same window, so it carries the same ramp, and the division
    removes it exactly.

    Tested by moving the window rather than the signal, because that is the
    error that actually happens: sync locates the preamble to within a prefix
    and _WINDOW_GUARD_FRACTION deliberately places the window early inside it.
    """
    rng = _rng(13)
    payload = rng.integers(0, 256, 200, dtype=np.uint8).tobytes()
    audio = np.asarray(ofdm.modulate(payload, QPSK), dtype=np.float64)
    truth = ofdm._pad_samples(ofdm.HEAD_PAD_SECONDS) + QPSK.cp

    for offset in range(-QPSK.cp // 2, QPSK.cp // 2 + 1, 10):
        result = ofdm._decode_at(audio, QPSK, truth + offset, 1.0)
        assert result.get("payload") == payload, f"offset {offset} inside the prefix failed"

    # And past it, in the direction that reads into the next symbol, it does
    # break -- so the tolerance above is the prefix doing its job.
    late = ofdm._decode_at(audio, QPSK, truth + 2 * QPSK.cp, 1.0)
    assert late.get("payload") != payload
    print("test_a_timing_error_inside_the_prefix_costs_nothing OK")


def test_sample_clock_offset_tolerance_is_set_by_the_top_carrier():
    """The one place this mode is *less* tolerant than the FSK profiles, and
    the reason is not the one the prefix would suggest.

    The prefix absorbs clock drift as timing without difficulty. What it does
    not absorb is phase: tau samples of accumulated drift rotate subcarrier k
    by 2*pi*k*tau/n_fft, and the frame dies when the top subcarrier's rotation
    reaches the constellation's decision boundary. That predicts

        max ppm ~ 1 / (2^(bits+1) * top_frequency * frame_seconds)

    and predicts, in particular, that the tolerance does not depend on
    subcarrier spacing at all -- a wider spacing puts fewer carriers in the
    same band, and it is the band edge in Hz that binds. Both halves are
    checked, because the second is the surprising one and is what says the
    model is right rather than merely fitted.

    whale/afsk.py's profiles tolerate 235-745 ppm by comparison. These two
    cards measure 3.4 ppm apart (scripts/measure_clock_offset.py), so QPSK has
    about 6x margin and 8PSK about 2.4x.
    """
    rng = _rng(17)

    def survives(profile, ppm):
        payload = rng.integers(0, 256, profile.max_payload, dtype=np.uint8).tobytes()
        audio = np.asarray(ofdm.modulate(payload, profile), dtype=np.float64)
        n = len(audio)
        resampled = np.interp(np.arange(n) * (1 + ppm * 1e-6), np.arange(n), audio)
        return ofdm.demodulate(resampled, profile).get("payload") == payload

    measured = {}
    for bits, expected in ((1, 40), (2, 20), (3, 8)):
        profile = ofdm.OfdmProfile(name=f"clk{bits}", n_fft=960, cp=120,
                                   bits_per_carrier=bits)
        assert survives(profile, 3.4), f"{bits} bits fails at the bench's own 3.4 ppm"
        assert survives(profile, expected), f"{bits} bits should survive {expected} ppm"
        assert not survives(profile, 3 * expected), \
            f"{bits} bits survived {3 * expected} ppm -- the model is wrong"
        measured[bits] = expected

        predicted = 1e6 / (2 ** (bits + 1) * profile.tone_high
                           * ofdm.frame_seconds(profile, profile.max_payload))
        assert 0.5 < predicted / expected < 2.0, (bits, predicted, expected)

    # The surprising half: spacing does not enter into it.
    for n_fft in (1600, 640):
        profile = ofdm.OfdmProfile(name=f"clk_sp{n_fft}", n_fft=n_fft,
                                   cp=n_fft // 8, bits_per_carrier=2)
        assert survives(profile, measured[2]), f"n_fft {n_fft} broke early"
    print("test_sample_clock_offset_tolerance_is_set_by_the_top_carrier OK")


# -- sync does not lock onto things that are not frames --------------------


def test_a_pure_tone_does_not_sync():
    """The trap in the standard OFDM sync, and the reason this module does not
    use it alone.

    A steady tone whose period divides the half-symbol is *exactly* periodic at
    the lag the repetition metric looks at, so it scores 1.000 -- perfectly
    indistinguishable from a real preamble by that measure. This RX buffer is
    routinely full of steady tones: an AFSK head pad, a self-echo, another
    station's carrier.

    So the test asserts both halves: the repetition metric really is fooled
    (if it stops being, this test is no longer testing anything), and the
    matched filter that decides is not.
    """
    t = np.arange(int(0.6 * ofdm.SAMPLE_RATE)) / ofdm.SAMPLE_RATE
    for freq in (1200.0, 1500.0, 1800.0, 2200.0):
        tone = np.sin(2 * np.pi * freq * t)
        repetition = ofdm._repetition_score(tone, QPSK.n_fft // 2)
        assert repetition.max() > 0.99, f"{freq} Hz no longer fools the repetition metric"

        result = ofdm.demodulate(tone, QPSK)
        assert result.get("payload") is None, freq
        assert result.get("confidence", 0.0) < QPSK.confidence_threshold, \
            f"{freq} Hz scored {result.get('confidence'):.3f} on the matched filter"
    print("test_a_pure_tone_does_not_sync OK")


def test_an_afsk_frame_does_not_sync_the_ofdm_correlator():
    """Both modes would share a channel during any migration, and the shipped
    profiles' head pad is an alternating tone pattern -- the family the test
    above shows the repetition metric cannot reject on its own."""
    payload = bytes(range(60))
    for profile in afsk.PROFILES:
        audio = np.asarray(afsk.modulate(payload, profile=profile), dtype=np.float64)
        result = ofdm.demodulate(audio, QPSK)
        assert result.get("payload") is None, profile.name
        assert result.get("confidence", 0.0) < QPSK.confidence_threshold, \
            f"{profile.name} scored {result.get('confidence'):.3f}"
    print("test_an_afsk_frame_does_not_sync_the_ofdm_correlator OK")


def test_the_pads_do_not_sync():
    """The head pad sits immediately in front of the preamble and is built
    from the same subcarriers, so it is the single most likely thing to be
    mistaken for one. It uses a different phase sequence for exactly this
    reason."""
    for seconds, seed in ((ofdm.HEAD_PAD_SECONDS, 1), (ofdm.TAIL_PAD_SECONDS, 2)):
        pad = ofdm._pad_audio(QPSK, max(seconds, 0.5), seed)
        result = ofdm.demodulate(pad, QPSK)
        assert result.get("confidence", 0.0) < QPSK.confidence_threshold, \
            f"pad seed {seed} scored {result.get('confidence'):.3f}"

    # And a training symbol, which is repeated several times per frame and is
    # periodic in the way the proposer looks for.
    train = ofdm._symbol_audio(QPSK, QPSK.carriers, ofdm.training_values(QPSK))
    tiled = np.tile(train, 12)
    assert ofdm.demodulate(tiled, QPSK).get("confidence", 0.0) < QPSK.confidence_threshold
    print("test_the_pads_do_not_sync OK")


def test_no_false_sync_on_off_air_captures():
    """The corpus afsk.CONFIDENCE_THRESHOLD was calibrated on: real captures
    off these radios, none of which contains an OFDM preamble. The previous
    two sync measures in this repo both reported locks on recordings like
    these -- the ratio measure on all 30 of them, and the uncentred
    multi-channel one on Gaussian noise -- so a new measure gets checked here
    before it gets airtime, not after.
    """
    captures = sorted(CAPTURE_DIR.rglob("*.npy"))
    if not captures:
        print("test_no_false_sync_on_off_air_captures SKIPPED (no captures)")
        return
    worst = 0.0
    for path in captures:
        audio = np.load(path).astype(np.float64)
        if len(audio) < 4 * QPSK.symbol_samples:
            continue
        result = ofdm.demodulate(audio, QPSK)
        assert result.get("payload") is None, path.name
        worst = max(worst, float(result.get("confidence", 0.0)))
    assert worst < QPSK.confidence_threshold, \
        f"best false score {worst:.3f} against threshold {QPSK.confidence_threshold}"
    print(f"test_no_false_sync_on_off_air_captures OK "
          f"({len(captures)} captures, worst {worst:.3f})")


def test_sync_confidence_separates_frames_from_noise():
    """The gap the threshold sits in, measured rather than assumed -- and
    measured through the impairments that actually narrow it, since a
    separation only demonstrated on clean audio is not evidence about a
    radio."""
    rng = _rng(23)
    payload = rng.integers(0, 256, 200, dtype=np.uint8).tobytes()
    frame = ofdm.modulate(payload, QPSK)

    lowest = 1.0
    for snr in (30, 20, 12, 8, 6):
        dispersed = _dispersive(frame, [(0, 1.0), (QPSK.cp // 3, 0.5)])
        result = ofdm.demodulate(_noisy(dispersed, snr, rng), QPSK)
        lowest = min(lowest, float(result.get("confidence", 0.0)))
    assert lowest > QPSK.confidence_threshold + 0.1, \
        f"genuine preamble fell to {lowest:.3f}, too close to the threshold"

    highest = 0.0
    for _ in range(20):
        noise = rng.normal(0.0, 1.0, 3 * QPSK.symbol_samples)
        highest = max(highest, float(ofdm.demodulate(noise, QPSK).get("confidence", 0.0)))
    assert highest < QPSK.confidence_threshold - 0.1, f"noise reached {highest:.3f}"
    print(f"test_sync_confidence_separates_frames_from_noise OK "
          f"(genuine >= {lowest:.3f}, absent <= {highest:.3f})")


# -- the decoder's contract with a caller ---------------------------------


def test_an_implausible_declared_length_is_a_dead_sync_not_a_frame_in_flight():
    """A false lock yields a uniformly random 16-bit length, and honouring one
    means waiting for a frame that will never arrive. whale/link.py's decode
    loop reads 'no end_index' as 'still arriving' and stops pruning, which
    turns a cheap poll into an expensive one landing on the turnaround --
    afsk.MAX_CREDIBLE_FRAME_SECONDS exists for this and the same rule is
    enforced here."""
    rng = _rng(29)
    audio = np.asarray(ofdm.modulate(b"", QPSK), dtype=np.float64)
    start = ofdm._pad_samples(ofdm.HEAD_PAD_SECONDS) + QPSK.cp

    # Overwrite the first data symbol so the length field *decodes* as 0xFFFF.
    # Built through the whitener rather than by setting every subcarrier to
    # one point: the receiver de-whitens before parsing, so a symbol that
    # looks constant on air is not what produces a constant bit pattern.
    wanted = np.ones(QPSK.bits_per_symbol, dtype=np.int64)
    values = ofdm._bits_to_values(ofdm._whiten(wanted), QPSK.bits_per_carrier)
    huge = ofdm.constellation(QPSK.bits_per_carrier)[values]
    # Indices name the *useful* part of a symbol, so the prefixed block that
    # has to be overwritten begins one prefix earlier.
    first_symbol_start = start + QPSK.n_fft + QPSK.cp
    block = first_symbol_start + QPSK.n_train * QPSK.symbol_samples - QPSK.cp
    audio[block:block + QPSK.symbol_samples] = ofdm._with_prefix(
        QPSK, ofdm._symbol_audio(QPSK, QPSK.carriers, huge))

    result = ofdm._decode_at(audio, QPSK, start, 1.0)
    assert result.get("payload") is None
    assert "end_index" in result, "an implausible length must end the sync, not extend it"
    assert result["end_index"] == result["sync_end_index"]
    print("test_an_implausible_declared_length_is_a_dead_sync_not_a_frame_in_flight OK")


def test_a_partial_frame_reads_as_still_arriving():
    """Half a frame in the buffer must not be reported as a dead end:
    discarding a frame mid-flight costs a retransmit, waiting one more poll
    costs nothing."""
    rng = _rng(31)
    payload = rng.integers(0, 256, 400, dtype=np.uint8).tobytes()
    full = np.asarray(ofdm.modulate(payload, QPSK), dtype=np.float64)
    partial = full[:len(full) // 2]
    result = ofdm.demodulate(partial, QPSK)
    assert result.get("payload") is None
    assert "end_index" not in result, "a frame still arriving must not be skipped past"
    assert result.get("confidence", 0.0) > QPSK.confidence_threshold, \
        "the preamble is fully present, so it should still be found"
    print("test_a_partial_frame_reads_as_still_arriving OK")


def test_earliest_frame_wins_not_the_loudest():
    """The RX buffer routinely holds a garbled self-echo of our own last
    transmission alongside the peer's real reply, and the echo is often the
    louder. Consuming up to the strongest peak throws away everything before
    it, the real frame included."""
    rng = _rng(37)
    first = rng.integers(0, 256, 120, dtype=np.uint8).tobytes()
    second = rng.integers(0, 256, 120, dtype=np.uint8).tobytes()
    quiet = np.asarray(ofdm.modulate(first, QPSK), dtype=np.float64) * 0.25
    loud = np.asarray(ofdm.modulate(second, QPSK), dtype=np.float64)
    buffer = np.concatenate([quiet, np.zeros(4000), loud])
    assert ofdm.demodulate(buffer, QPSK).get("payload") == first
    print("test_earliest_frame_wins_not_the_loudest OK")


# -- the budget, and what it is worth -------------------------------------


def test_keying_budget_still_agrees_with_the_shipped_modem():
    """This module copies MAX_KEYING_SECONDS and KEYING_OVERHEAD_SECONDS out
    of whale/afsk.py so it can stand alone. That copy is the only coupling
    between them, and this is the only place it shows."""
    assert ofdm.MAX_KEYING_SECONDS == afsk.MAX_KEYING_SECONDS
    assert ofdm.KEYING_OVERHEAD_SECONDS == afsk.KEYING_OVERHEAD_SECONDS
    assert ofdm.HEAD_PAD_SECONDS == framing.HEAD_PAD_SECONDS
    assert ofdm.TAIL_PAD_SECONDS == framing.TAIL_PAD_SECONDS
    assert ofdm.MAX_CREDIBLE_FRAME_SECONDS == afsk.MAX_CREDIBLE_FRAME_SECONDS
    print("test_keying_budget_still_agrees_with_the_shipped_modem OK")


def test_every_keying_fits_the_budget_and_uses_it():
    """max_payload is the largest payload fitting MAX_KEYING_SECONDS, so one
    more symbol must not fit. Checked across the whole candidate space, since
    an off-by-one here would put every bench trial over the cap the whole
    exercise is conducted under."""
    for profile in ofdm.candidates():
        used = ofdm.keying_seconds(profile, profile.max_payload)
        assert used <= ofdm.MAX_KEYING_SECONDS + 1e-9, (profile.name, used)
        # One more data symbol's worth of payload must not fit.
        over = profile.max_payload + (profile.bits_per_symbol // 8) + 1
        assert ofdm.keying_seconds(profile, over) > ofdm.MAX_KEYING_SECONDS, \
            (profile.name, "budget not fully used")
    print("test_every_keying_fits_the_budget_and_uses_it OK")


def test_the_prefix_is_the_only_thing_the_candidates_trade():
    """Throughput ordering should be explicable, not mysterious: at a fixed
    constellation, a candidate is faster exactly when it spends less airtime
    on cyclic prefix. If that stops holding, the candidate generator has
    grown a second axis and the ladder's ordering no longer means what
    sweep_ofdm.py says it means."""
    for bits in (1, 2, 3, 4):
        pool = [p for p in ofdm.candidates(bits=(bits,))]
        rates = [p.payload_bitrate for p in pool]
        overheads = [p.prefix_overhead for p in pool]
        assert rates == sorted(rates, reverse=True), bits
        # Spearman-free check: the fastest must not have the largest prefix.
        assert overheads[0] == min(overheads), bits
    print("test_the_prefix_is_the_only_thing_the_candidates_trade OK")


def test_qpsk_beats_both_shipped_and_mfsk_throughput_on_arithmetic():
    """What the exercise is chasing, on file so nobody has to rediscover it
    the expensive way.

    PROFILE_1200 delivers 947 payload bits/s and experiments/mfsk's bench
    winner 1011. QPSK over the same measured band should roughly double that,
    and the reason is not bandwidth -- it is that every mode in this repo so
    far throws the phase away.
    """
    reference = afsk.PROFILE_1200.chunk_size * 8 / afsk.MAX_KEYING_SECONDS
    best_qpsk = ofdm.candidates(bits=(2,))[0]
    assert best_qpsk.payload_bitrate > 2 * reference, \
        f"{best_qpsk.payload_bitrate:.0f} vs {reference:.0f}"
    assert best_qpsk.payload_bitrate > 2 * 1010.7, "should also beat the MFSK winner 2x"
    print(f"test_qpsk_beats_both_shipped_and_mfsk_throughput_on_arithmetic OK "
          f"({best_qpsk.payload_bitrate:.0f} vs {reference:.0f} bits/s)")


def test_clipping_reaches_its_target_and_the_default_is_the_optimum():
    """papr_db is the parameter with the least evidence behind it, so the two
    things that can be checked in software are checked: the clipper actually
    reaches the ratio it is asked for, and the default sits where the
    modelled trade is best (see _clip_and_filter's table)."""
    rng = _rng(41)
    payload = rng.integers(0, 256, 300, dtype=np.uint8).tobytes()
    from dataclasses import replace
    for target in (6.0, 8.0, 9.0, 10.0):
        profile = replace(QPSK, papr_db=target)
        achieved = ofdm.measured_papr_db(ofdm.modulate(payload, profile))
        assert achieved <= target + ofdm._PAPR_TOLERANCE_DB + 0.05, (target, achieved)
        assert achieved > target - 1.5, (target, achieved, "clipping overshot badly")
    assert QPSK.papr_db == 9.0, "the default moved without the table above moving with it"
    print("test_clipping_reaches_its_target_and_the_default_is_the_optimum OK")


if __name__ == "__main__":
    test_soft_16qam_llrs_are_code_independent()
    test_standard_ldpc_corrects_errors_and_has_zero_syndrome()
    test_standard_rate_two_thirds_ldpc_corrects_errors()
    test_ldpc_16qam_roundtrip_on_synthetic_channels()
    test_rate_two_thirds_16qam_roundtrip()
    test_fec_length_search_does_not_treat_capture_tail_as_tracking_pilots()
    test_fec_roundtrip_preserves_variable_payload_lengths()
    test_fec_length_search_is_capped_at_the_profile_capacity()
    test_fec_length_candidate_stops_at_the_first_failed_codeword()
    test_constellations_are_gray_coded_and_unit_power()
    test_the_whitening_sequence_is_a_full_period_m_sequence()
    test_a_repetitive_payload_is_no_worse_than_a_random_one()
    test_roundtrip_in_clean_audio()
    test_dft_spread_roundtrip_and_energy()
    test_dft_spreading_reduces_unclipped_data_symbol_papr()
    test_dft_spread_roundtrip_with_excluded_physical_carriers()
    test_tracking_pilots_refresh_a_moving_channel()
    test_tracking_pilot_layout_and_budget_are_exact()
    test_random_payloads_roundtrip_at_full_size()
    test_frame_is_found_when_it_does_not_start_at_the_buffer_edge()
    test_dispersion_the_prefix_covers_costs_nothing()
    test_a_timing_error_inside_the_prefix_costs_nothing()
    test_sample_clock_offset_tolerance_is_set_by_the_top_carrier()
    test_a_pure_tone_does_not_sync()
    test_an_afsk_frame_does_not_sync_the_ofdm_correlator()
    test_the_pads_do_not_sync()
    test_no_false_sync_on_off_air_captures()
    test_sync_confidence_separates_frames_from_noise()
    test_an_implausible_declared_length_is_a_dead_sync_not_a_frame_in_flight()
    test_a_partial_frame_reads_as_still_arriving()
    test_earliest_frame_wins_not_the_loudest()
    test_keying_budget_still_agrees_with_the_shipped_modem()
    test_every_keying_fits_the_budget_and_uses_it()
    test_the_prefix_is_the_only_thing_the_candidates_trade()
    test_qpsk_beats_both_shipped_and_mfsk_throughput_on_arithmetic()
    test_clipping_reaches_its_target_and_the_default_is_the_optimum()
    print("all tests OK")
