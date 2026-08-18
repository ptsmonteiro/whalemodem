"""Pure-software checks for experiments/mfsk/mfsk.py -- no hardware, no radios.

These are invariants, not evidence about the radios. Everything here passing
means the modem is self-consistent and the candidate generator respects the
constraints it claims to; whether any candidate survives the FM audio chain is
sweep_mfsk.py's question, and the repo has plenty of precedent for a placement
that is perfect in software and 0/6 on air.

Run: python experiments/mfsk/test_mfsk.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

import mfsk
import screen_mfsk
from whale import afsk, framing

# One representative per M, all at orthogonal spacing, for the tests that only
# care about the modem working rather than about a specific placement.
REPRESENTATIVE = [mfsk.place(2, 1150, 1.0), mfsk.place(4, 550, 1.0), mfsk.place(8, 225, 1.0)]

# The 4-FSK working point the rest of the tests use: the fastest orthogonal
# one the band allows. place() returns None for anything that does not fit, so
# these are checked rather than assumed -- an off-by-one in the constraints
# would otherwise turn every test below into an AttributeError on None.
QUATERNARY = mfsk.place(4, 550, 1.0)
assert all(p is not None for p in REPRESENTATIVE + [QUATERNARY])


def _rng(seed=20260818):
    return np.random.default_rng(seed)


def test_gray_mapping_is_a_bijection_with_adjacent_tones_one_bit_apart():
    """The point of Gray coding here is the frequency axis, not the value
    axis: the confusions this detector makes are between neighbouring tones,
    so neighbouring tones are what must differ in a single bit."""
    for m in (2, 4, 8, 16):
        values = mfsk._value_for_tone(m)
        assert sorted(values.tolist()) == list(range(m)), m
        inverse = mfsk._tone_for_value(m)
        assert np.array_equal(inverse[values], np.arange(m)), m
        for t in range(m - 1):
            differing = bin(int(values[t]) ^ int(values[t + 1])).count("1")
            assert differing == 1, (m, t, differing)
    print("test_gray_mapping_is_a_bijection_with_adjacent_tones_one_bit_apart OK")


def test_roundtrip_in_clean_audio():
    """Empty, tiny, and full-budget payloads, at every M. The full-budget one
    is the case that matters -- it is the only size the mode is ever run at --
    but the small ones catch symbol-padding arithmetic that a long frame
    would hide."""
    rng = _rng()
    for profile in REPRESENTATIVE:
        for length in (0, 1, 7, 64, profile.max_payload):
            payload = rng.integers(0, 256, length, dtype=np.uint8).tobytes()
            result = mfsk.demodulate(mfsk.modulate(payload, profile), profile)
            assert result.get("payload") == payload, (profile.name, length)
            assert result["confidence"] > 0.9, (profile.name, length, result["confidence"])
    print("test_roundtrip_in_clean_audio OK")


def test_frame_is_found_when_it_does_not_start_at_the_buffer_edge():
    """The decoder searches a rolling RX buffer, so a frame arrives with an
    arbitrary amount of junk in front of it -- never at index 0."""
    rng = _rng()
    profile = QUATERNARY
    payload = rng.integers(0, 256, 100, dtype=np.uint8).tobytes()
    audio = mfsk.modulate(payload, profile)
    for lead_seconds in (0.0, 0.37, 1.5):
        noise = rng.normal(0, 0.02, int(lead_seconds * mfsk.SAMPLE_RATE)).astype(np.float32)
        buffered = np.concatenate([noise, audio, noise])
        assert mfsk.demodulate(buffered, profile).get("payload") == payload, lead_seconds
    print("test_frame_is_found_when_it_does_not_start_at_the_buffer_edge OK")


def test_sync_confidence_separates_frames_from_noise():
    """mfsk.CONFIDENCE_THRESHOLD is inherited from afsk.CONFIDENCE_THRESHOLD,
    which was calibrated for a *scalar* normalised correlation. The
    multi-channel version here is a different statistic, so the gap it was
    chosen to sit in has to be re-checked rather than assumed -- the measure
    afsk replaced scored noise at up to 214 against a threshold of 4."""
    rng = _rng()
    for profile in REPRESENTATIVE:
        payload = rng.integers(0, 256, 120, dtype=np.uint8).tobytes()
        genuine = mfsk.demodulate(mfsk.modulate(payload, profile), profile)["confidence"]

        noise = rng.normal(0, 0.3, int(3.0 * mfsk.SAMPLE_RATE)).astype(np.float32)
        absent = mfsk.demodulate(noise, profile).get("confidence", 0.0)

        # Modulated audio carrying no preamble: harder than noise, because it
        # has the right spectrum and the right symbol timing.
        decoys = rng.integers(0, profile.m, 1200, dtype=np.int64)
        decoy_audio = mfsk._cpfsk(decoys, profile, mfsk.SAMPLE_RATE).astype(np.float32)
        decoy = mfsk.demodulate(decoy_audio, profile).get("confidence", 0.0)

        assert genuine > 0.9, (profile.name, genuine)
        assert absent < mfsk.CONFIDENCE_THRESHOLD, (profile.name, absent)
        assert decoy < mfsk.CONFIDENCE_THRESHOLD, (profile.name, decoy)
    print("test_sync_confidence_separates_frames_from_noise OK")


def test_every_keying_fits_the_budget_and_uses_it():
    """The constraint this whole exercise is under. Both halves matter: the
    cap bounds how long the transmitter holds the channel, and the tightness
    is the throughput -- max_payload is derived from the budget, so room left
    for another whole byte means the derivation has drifted."""
    for profile in mfsk.candidates()[:40]:
        payload = profile.max_payload
        assert mfsk.keying_seconds(profile, payload) <= mfsk.MAX_KEYING_SECONDS + 1e-9, \
            (profile.name, payload)
        assert mfsk.keying_seconds(profile, payload + 1) > mfsk.MAX_KEYING_SECONDS, \
            f"{profile.name} leaves room for more than {payload} bytes"
    print("test_every_keying_fits_the_budget_and_uses_it OK")


def test_keying_budget_still_agrees_with_the_shipped_modem():
    """mfsk.py restates MAX_KEYING_SECONDS and KEYING_OVERHEAD_SECONDS rather
    than importing them, so it stands alone. This is the one place that
    coupling is visible: if whale/ re-measures either -- and the overhead is
    measured, it moved from 0.40 to 0.43 once already -- this copy is stale
    and every payload size derived from it is wrong."""
    assert mfsk.MAX_KEYING_SECONDS == afsk.MAX_KEYING_SECONDS
    assert mfsk.KEYING_OVERHEAD_SECONDS == afsk.KEYING_OVERHEAD_SECONDS
    print("test_keying_budget_still_agrees_with_the_shipped_modem OK")


def test_pad_seconds_still_agree_with_the_shipped_modem():
    """mfsk.py restates HEAD_PAD_SECONDS and TAIL_PAD_SECONDS from
    framing.HEAD_PAD_SECONDS / framing.TAIL_PAD_SECONDS rather than importing
    them, for the same "stands alone" reason as the keying budget above --
    and until this test existed, that coupling was invisible the same way
    the keying budget's used to be.

    It already drifted, silently: framing.HEAD_PAD_SECONDS moved from 0.08 to
    0.15 (an HT that blacks out ~110ms after its squelch opens -- see the
    comment above that constant in whale/framing.py) and this copy was never
    updated to match. Every payload size mfsk.py has derived since that
    commit landed, including the bench-verified 4fsk_650bd_x0.833 winner's
    379 bytes, was computed against a head pad 70ms narrower than the one
    whale/ actually ships today. See RESULTS.md's "stale constants" note for
    what that is worth in payload bits/s, and for why fixing the number here
    would not even fully restore the comparisons that note is about.

    This assertion is being added after the drift happened, not before it,
    which is exactly why it currently FAILS -- that failure is the point: it
    turns a silent mismatch into a loud one. Making it pass means updating
    mfsk.py's copy of HEAD_PAD_SECONDS, which changes what modulate() emits
    and what every candidate's max_payload computes to, so it belongs with
    its own bench session rather than riding in on a documentation pass.
    """
    assert mfsk.HEAD_PAD_SECONDS == framing.HEAD_PAD_SECONDS, \
        (mfsk.HEAD_PAD_SECONDS, framing.HEAD_PAD_SECONDS)
    assert mfsk.TAIL_PAD_SECONDS == framing.TAIL_PAD_SECONDS, \
        (mfsk.TAIL_PAD_SECONDS, framing.TAIL_PAD_SECONDS)
    print("test_pad_seconds_still_agree_with_the_shipped_modem OK")


def test_candidates_respect_the_measured_constraints():
    """The generator's whole job is to not propose things the bench has
    already ruled out. Each of these bounds is a measurement in whale/ or in
    scripts/, not a preference."""
    for profile in mfsk.candidates():
        assert profile.tones[0] >= mfsk.BAND_LOW_HZ - 1e-6, profile.name
        assert profile.tones[-1] <= mfsk.BAND_HIGH_HZ + 1e-6, profile.name
        assert profile.symbol_rate <= mfsk.MAX_SYMBOL_RATE, profile.name
        assert profile.cycles_per_symbol >= mfsk.MIN_CYCLES_PER_SYMBOL - 1e-9, \
            (profile.name, profile.cycles_per_symbol)
    print("test_candidates_respect_the_measured_constraints OK")


def test_no_orthogonal_candidate_can_beat_the_shipped_profile():
    """The result that decides what this experiment can and cannot deliver.
    Kept as an assertion because it is easy to forget, easy to talk yourself
    out of, and expensive to rediscover on the bench.

    Orthogonal MFSK is bandwidth-inefficient by construction: M tones one
    symbol rate apart span (M-1)*Rs, so the rate is log2(M)/(M-1) bits/s per
    Hz of span -- 1.00 at M=2, 0.67 at M=4, 0.43 at M=8. Fold in the 600-2300
    band and the one-cycle rule and the best orthogonal placements come out at

        M=2   1150 baud   1150 raw    909 payload bits/s
        M=4    550 baud   1100 raw    848 payload bits/s
        M=8    225 baud    675 raw    477 payload bits/s

    against the shipped 1200-baud profile's 947. *None of them reaches
    parity*, M=4 included -- and the reason the shipped profile beats them all
    is that it is itself sub-orthogonal, running 1000 Hz of separation at 1200
    baud (a ratio of 0.833).

    So there is no orthogonal MFSK mode that is a throughput win here. Every
    candidate above 947 bits/s in mfsk.candidates() gets there by packing
    tones closer than the detector can cleanly separate, exactly as the
    shipped profile does. That is a bet on this specific hardware, and
    sweep_mfsk.py is how it gets settled.
    """
    shipped = afsk.PROFILE_1200.chunk_size * 8 / mfsk.MAX_KEYING_SECONDS
    best = {m: max(mfsk.candidates(ms=(m,), spacing_ratios=(1.0,)),
                   key=lambda p: p.payload_bitrate) for m in (2, 4, 8)}

    for m, profile in best.items():
        assert profile.payload_bitrate < shipped, \
            f"orthogonal M={m} now beats the shipped profile: " \
            f"{profile.payload_bitrate:.1f} vs {shipped:.1f} -- the band or the " \
            f"constraints moved, and the case for this experiment changed with them"

    # M=4 lands within a few percent of M=2 rather than far below it, which is
    # the whole reason M=4 is the interesting one: log2(M)/M is equal at 2 and
    # 4, so the two only separate because the 600 Hz band floor binds M=4
    # before the one-cycle rule does.
    assert best[4].payload_bitrate > best[2].payload_bitrate * 0.9, best
    assert best[8].payload_bitrate < best[4].payload_bitrate * 0.7, best
    print("test_no_orthogonal_candidate_can_beat_the_shipped_profile OK")


def test_four_fsk_reaches_binary_throughput_at_half_the_symbol_rate():
    """The other side of that coin, and the real argument for MFSK on this
    bench: scripts/sweep_baud_600_2300.py failed 1400 baud at the same two
    tones that cleared 1200, so the wall here is on symbol rate, not on
    bandwidth. A mode delivering the same bits at half the symbol rate is
    buying margin against the thing that actually breaks -- which is why an
    orthogonal 4-FSK mode can be worth having even at 90% of the shipped
    profile's throughput."""
    binary = max(mfsk.candidates(ms=(2,), spacing_ratios=(1.0,)),
                 key=lambda p: p.payload_bitrate)
    quaternary = max(mfsk.candidates(ms=(4,), spacing_ratios=(1.0,)),
                     key=lambda p: p.payload_bitrate)
    assert quaternary.payload_bitrate >= binary.payload_bitrate * 0.9, \
        (quaternary.payload_bitrate, binary.payload_bitrate)
    assert quaternary.symbol_rate <= binary.symbol_rate * 0.55, \
        (quaternary.symbol_rate, binary.symbol_rate)
    print("test_four_fsk_reaches_binary_throughput_at_half_the_symbol_rate OK")


def test_preamble_training_is_unproven_in_software_and_costs_nothing():
    """The per-tone equaliser has NOT earned its place on any software
    evidence, and this test records that rather than dressing it up.

    The reasoning for it was sound and turned out not to bite: an argmax over
    four tones spanning 1700 Hz is decided by whichever came back loudest, so
    a chain that rolls off across the band should bias every symbol, and
    training on the preamble should remove that. Measured, it makes no
    difference anywhere -- a gain slope of 0, 6, 12 and 18 dB, at orthogonal
    spacing and at ratios down to 0.6, with the frame alone in the buffer and
    with four seconds of noise around it, trained and untrained decode
    identically, right up to the point where both fail together.

    Two reasons, both worth knowing before anyone re-derives this:

      - tone_envelopes() already divides each tone by its own RMS over the
        buffer, which is a coarse per-tone equaliser in its own right. The
        training is a refinement of something already present, not a new
        capability.
      - the decision margin at these spacings is far wider than the tilt. With
        the wrong tones' filters seeing mostly noise, a few dB of gain error
        does not come close to flipping an argmax.

    So this asserts equivalence, not benefit. It is kept switchable because
    the model here has no group delay in it, and group delay travelling with a
    real frequency response is the half of the channel that has broken every
    previous tone placement on this bench -- sweep_mfsk.py --no-training is
    how that gets tested on air. If this test ever starts failing, the
    equaliser has begun to matter and the reason is worth chasing down.
    """
    rng = _rng()
    trained = mfsk.place(4, 550, 1.0, train_on_preamble=True)
    untrained = mfsk.place(4, 550, 1.0, train_on_preamble=False)
    pad = np.zeros(int(1.0 * mfsk.SAMPLE_RATE), dtype=np.float32)

    for tilt in (0.0, 6.0, 12.0):
        payload = rng.integers(0, 256, trained.max_payload, dtype=np.uint8).tobytes()
        audio = screen_mfsk.apply_tilt(mfsk.modulate(payload, trained), trained, tilt)
        buffered = np.concatenate([pad, audio, pad])
        for profile in (trained, untrained):
            assert mfsk.demodulate(buffered, profile).get("payload") == payload, \
                (profile.train_on_preamble, tilt)
    print("test_preamble_training_is_unproven_in_software_and_costs_nothing OK")


def test_training_does_not_hurt_a_noisy_channel():
    """Whatever the equaliser may buy on a real chain, it must not cost
    anything without one: it divides by an estimate made from 63 noisy
    symbols, and a bad estimate is worse than not dividing at all."""
    rng = _rng()
    trained = mfsk.place(4, 550, 1.0, train_on_preamble=True)
    untrained = mfsk.place(4, 550, 1.0, train_on_preamble=False)
    for snr in (12.0, 16.0):
        for profile in (trained, untrained):
            payload = rng.integers(0, 256, profile.max_payload, dtype=np.uint8).tobytes()
            audio = screen_mfsk.add_awgn(mfsk.modulate(payload, profile), profile, snr, rng)
            assert mfsk.demodulate(audio, profile).get("payload") == payload, \
                (profile.train_on_preamble, snr)
    print("test_training_does_not_hurt_a_noisy_channel OK")


def _resample(audio, ratio):
    """The receiver's sample clock running `ratio` times the transmitter's.
    Same construction tests/test_afsk_loopback.py uses."""
    n = int(len(audio) / ratio)
    return np.interp(np.arange(n) * ratio, np.arange(len(audio)), audio).astype(np.float32)


def test_clock_offset_tolerance_is_half_a_symbol_over_the_frame():
    """Symbol points are laid on a rigid grid from the sync peak with no
    timing recovery, so a frame dies when accumulated timing error reaches
    half a symbol: the budget is 0.5/n_symbols, and MFSK spends the keying on
    *fewer* symbols than binary FSK does.

    That is a second, quieter argument for M>2 on hardware whose clocks are
    not close. The two sound cards here measure 3.4 ppm apart
    (scripts/measure_clock_offset.py), so it costs nothing on this bench --
    but a 4-FSK frame at 575 baud carries half the symbols of a 1200-baud
    binary one, so it tolerates twice the offset.
    """
    profile = QUATERNARY
    payload = _rng().integers(0, 256, profile.max_payload, dtype=np.uint8).tobytes()
    audio = mfsk.modulate(payload, profile)
    symbols = mfsk.frame_symbols(profile, len(payload))
    budget = 0.5 / symbols

    assert mfsk.demodulate(_resample(audio, 1 + budget * 0.5), profile).get("payload") == payload
    assert mfsk.demodulate(_resample(audio, 1 - budget * 0.5), profile).get("payload") == payload
    assert mfsk.demodulate(_resample(audio, 1 + budget * 4), profile).get("payload") != payload

    bench_offset = 3.4e-6
    assert bench_offset < budget / 50, (bench_offset, budget)

    binary = max(mfsk.candidates(ms=(2,), spacing_ratios=(1.0,)),
                 key=lambda p: p.payload_bitrate)
    binary_symbols = mfsk.frame_symbols(binary, binary.max_payload)
    assert symbols < binary_symbols * 0.6, (symbols, binary_symbols)
    print("test_clock_offset_tolerance_is_half_a_symbol_over_the_frame OK")


def test_an_implausible_declared_length_is_a_dead_sync_not_a_frame_in_flight():
    """A length field cannot be checked before the CRC after it arrives, so an
    implausible claim has to be rejected on its face. Honouring one is not
    free: it reports 'still arriving', which tells a decode loop to keep
    re-searching the whole buffer instead of pruning it."""
    profile = QUATERNARY
    bits = framing.bytes_to_bits((60000).to_bytes(2, "big") + b"\x00" * 8)
    bpsym = profile.bits_per_symbol
    values = [int("".join(str(b) for b in bits[i:i + bpsym]), 2)
              for i in range(0, len(bits) - bpsym + 1, bpsym)]
    symbols = np.concatenate([
        mfsk._pad_symbols(profile, mfsk.HEAD_PAD_SECONDS),
        mfsk.preamble_symbols(profile),
        mfsk._tone_for_value(profile.m)[np.array(values, dtype=np.int64)],
    ])
    audio = mfsk._cpfsk(symbols, profile, mfsk.SAMPLE_RATE).astype(np.float32)

    result = mfsk.demodulate(audio, profile)
    assert result["payload"] is None
    assert "end_index" in result, "an incredible length must end the sync, not extend it"
    assert result["end_index"] == result["sync_end_index"], result
    print("test_an_implausible_declared_length_is_a_dead_sync_not_a_frame_in_flight OK")


def test_a_partial_frame_reads_as_still_arriving():
    """The other half of that judgement: a frame whose declared length is
    plausible but whose bits have not all landed must NOT be reported as a
    dead end, or the caller discards a frame mid-flight and pays a retransmit
    for what one more poll would have settled."""
    profile = QUATERNARY
    payload = _rng().integers(0, 256, 200, dtype=np.uint8).tobytes()
    audio = mfsk.modulate(payload, profile)
    truncated = audio[:int(len(audio) * 0.6)]
    result = mfsk.demodulate(truncated, profile)
    assert result["payload"] is None
    assert "end_index" not in result, result
    print("test_a_partial_frame_reads_as_still_arriving OK")


def test_earliest_frame_wins_not_the_loudest():
    """The RX buffer routinely holds a garbled, louder self-echo of our own
    last transmission alongside the peer's real reply. Consuming up to the
    strongest peak throws the real frame away -- see afsk._sync_peaks."""
    rng = _rng()
    profile = QUATERNARY
    first = rng.integers(0, 256, 40, dtype=np.uint8).tobytes()
    second = rng.integers(0, 256, 40, dtype=np.uint8).tobytes()
    quiet = mfsk.modulate(first, profile) * 0.25
    loud = mfsk.modulate(second, profile)
    gap = np.zeros(int(0.2 * mfsk.SAMPLE_RATE), dtype=np.float32)
    result = mfsk.demodulate(np.concatenate([quiet, gap, loud]), profile)
    assert result.get("payload") == first, "the louder later frame won"
    print("test_earliest_frame_wins_not_the_loudest OK")


def test_random_payloads_roundtrip_at_full_size():
    """A fixed payload can pass on a decoder that reconstructs what it
    expects. These are random and different every trial, and checked
    byte-for-byte, which is also what sweep_mfsk.py sends over the air."""
    rng = _rng(99)
    for profile in mfsk.candidates()[:8]:
        for _ in range(3):
            payload = rng.integers(0, 256, profile.max_payload, dtype=np.uint8).tobytes()
            assert mfsk.demodulate(mfsk.modulate(payload, profile), profile).get("payload") \
                == payload, profile.name
    print("test_random_payloads_roundtrip_at_full_size OK")


if __name__ == "__main__":
    test_gray_mapping_is_a_bijection_with_adjacent_tones_one_bit_apart()
    test_roundtrip_in_clean_audio()
    test_frame_is_found_when_it_does_not_start_at_the_buffer_edge()
    test_sync_confidence_separates_frames_from_noise()
    test_every_keying_fits_the_budget_and_uses_it()
    test_keying_budget_still_agrees_with_the_shipped_modem()
    test_candidates_respect_the_measured_constraints()
    test_no_orthogonal_candidate_can_beat_the_shipped_profile()
    test_four_fsk_reaches_binary_throughput_at_half_the_symbol_rate()
    test_preamble_training_is_unproven_in_software_and_costs_nothing()
    test_training_does_not_hurt_a_noisy_channel()
    test_clock_offset_tolerance_is_half_a_symbol_over_the_frame()
    test_an_implausible_declared_length_is_a_dead_sync_not_a_frame_in_flight()
    test_a_partial_frame_reads_as_still_arriving()
    test_earliest_frame_wins_not_the_loudest()
    test_random_payloads_roundtrip_at_full_size()
    print("all tests OK")
