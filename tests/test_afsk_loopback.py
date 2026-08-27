"""Pure-software checks for framing + afsk -- no hardware, no radios.

Run: python tests/test_afsk_loopback.py
"""

import threading
import time

import numpy as np

from whale import afsk, framing, link, mode_history
from whale.waveform import ModeRegistry

# The two-Link-in-one-process harness these tests share with
# tests/test_link_recovery.py.
from link_harness import (FakeTransport as _FakeTransport, connected_pair as _connected_pair,
                          silence_once as _silence_once, transfer as _transfer)


def test_framing_roundtrip():
    # 255/256 is where the old 8-bit length field ended, so both sides of
    # that boundary are checked explicitly; 1000 is past anything a keying
    # can carry but well within what the 16-bit field must describe.
    for payload in (b"", b"hello", bytes(range(256))[:255],
                    bytes(range(256)), bytes(i % 256 for i in range(1000))):
        bits = framing.build_frame_bits(payload)
        after_sync = len(framing.head_pad_bits(300)) + len(framing.sync_bits(300))
        decoded = framing.parse_frame_bits(bits[after_sync:])
        assert decoded == payload, (len(payload), len(decoded or b""))
    print("test_framing_roundtrip OK")


def test_link_header_and_body_have_independent_crcs():
    header, body = link._encode_air_header(link.PT_DATA, afsk.PROFILE_600.mode_id,
                                           b"\x07payload")
    bits = framing.build_frame_bits(header + body, baud=600,
                                    include_head=False, include_tail=False)
    after_sync = bits[len(framing.sync_bits(600)):]
    assert framing.header_is_valid(after_sync) is True
    assert framing.parse_frame_bits(after_sync) == header + body

    bad_header = after_sync.copy()
    bad_header[framing.LENGTH_FIELD_BITS + 3] ^= 1
    assert framing.header_is_valid(bad_header) is False

    bad_body = after_sync.copy()
    body_start = (framing.LENGTH_FIELD_BITS
                  + 8 * framing.AIR_HEADER_BYTES + 16)
    bad_body[body_start + 3] ^= 1
    assert framing.header_is_valid(bad_body) is True
    assert framing.parse_frame_bits(bad_body) is None


def test_sync_words_are_full_period_m_sequences():
    """Each profile's sync word must really be an m-sequence, because that
    is the whole reason it works as a correlation target: an m-sequence's
    periodic autocorrelation is a single peak with a flat -1 floor, so the
    correlator has one unambiguous lock point rather than a ridge of
    near-peaks it can settle on a symbol or two off.

    Checked by construction rather than trusted from a table of primitive
    polynomials, since framing._SYNC_TAPS carries one tap set per PN order
    and a wrong entry would still produce a plausible-looking bit string --
    just a short-period one, with sidelobes to match.
    """
    for baud in (300, 600, 1200, 2400):
        bits = framing.sync_bits(baud)
        order = (len(bits) + 1).bit_length() - 1
        assert len(bits) == (1 << order) - 1, (baud, len(bits))

        # Balance: an m-sequence has exactly one more 1 than 0 per period.
        assert sum(bits) == (1 << (order - 1)), (baud, sum(bits))

        # Periodic autocorrelation: +len at zero shift, exactly -1 elsewhere.
        pm = np.where(np.array(bits) == 1, 1, -1)
        for shift in range(1, len(pm)):
            assert int(np.dot(pm, np.roll(pm, shift))) == -1, (baud, shift)

    # And each lasts about framing.SYNC_SECONDS on air -- the point of
    # scaling it at all. See framing.sync_bits for why a fixed bit count
    # left the fastest profile with the least margin.
    for baud in (300, 600, 1200, 2400):
        seconds = len(framing.sync_bits(baud)) / baud
        assert abs(seconds - framing.SYNC_SECONDS) / framing.SYNC_SECONDS < 0.02, \
            (baud, seconds)
    print("test_sync_words_are_full_period_m_sequences OK")


def test_a_frame_does_not_sync_another_profiles_correlator():
    """Every profile has to reject the others' frames, since all three share
    a radio and 300/600 now share a tone pair as well -- so symbol timing
    and the sync word itself are the only things telling them apart.

    This matters at exactly one moment in a session: a mode step. Both ends
    keep decoding at the old profile until a data frame settles the new one
    (see whale/link.py), so for one turnaround the receiver is running a
    correlator at a baud the transmitter has already left.
    """
    payload = bytes(range(64))
    for tx_profile in afsk.PROFILES:
        audio = afsk.modulate(payload, profile=tx_profile)
        for rx_profile in afsk.PROFILES:
            if rx_profile.mode_id == tx_profile.mode_id:
                continue
            result = afsk.demodulate(audio, profile=rx_profile)
            assert result.get("payload") is None, \
                (tx_profile.name, rx_profile.name, "decoded another profile's frame")
            assert result.get("confidence", 0.0) < rx_profile.confidence_threshold, \
                (tx_profile.name, rx_profile.name, result.get("confidence"))
    print("test_a_frame_does_not_sync_another_profiles_correlator OK")


def test_a_lock_survives_losing_the_opening_of_the_sync_word():
    """The structural property behind framing.SYNC_SECONDS.

    A receiver that is still settling when a transmission starts destroys
    the front of the frame -- on the bench HT, ~110ms of blackout after its
    squelch opens, during which what arrives is broadband transient with no
    tone in it. The frame body survives that easily; the sync word is what
    the loss lands on, and a frame that cannot be synced on is lost however
    clean the rest of it is.

    What must hold is that the survivable loss is the same *fraction* of the
    sync word at every profile. Since every profile's sync word is the same
    duration, an equal fraction is an equal number of milliseconds -- so one
    head-pad figure protects all three, instead of protecting the slow ones
    while the fast one silently runs on a fraction of the margin.

    With a fixed 63-bit sync word this failed badly: the same 110ms blackout
    cost 300 baud a quarter of its sync word and 1200 baud all of it.
    """
    payload = bytes(range(100))
    rng = np.random.default_rng(11)

    def decode_rate(profile, fraction, trials=6):
        sps = round(afsk.SAMPLE_RATE / profile.baud)
        sync_start = len(framing.head_pad_bits(profile.baud)) * sps
        n_sync = len(framing.sync_bits(profile.baud))
        ok = 0
        for _ in range(trials):
            audio = afsk.modulate(payload, profile=profile).astype(np.float64)
            end = sync_start + int(fraction * n_sync * sps)
            # Loud broadband noise, not silence: that is what the radio
            # actually delivers, and it is the harder case -- it adds energy
            # to the correlation window without adding signal.
            level = np.sqrt(np.mean(audio[sync_start:sync_start + 10 * sps] ** 2))
            audio[:end] = rng.normal(0, level * 1.5, end)
            ok += int(afsk.demodulate(audio, profile=profile).get("payload") == payload)
        return ok / trials

    for profile in afsk.PROFILES:
        assert decode_rate(profile, 0.4) == 1.0, \
            (profile.name, "should survive losing the first 40% of the sync word")

    # And the cliff is in the same place for every profile -- that sameness
    # is the property, more than the exact figure. If a decoder change moves
    # it, move the 0.4 in framing.HEAD_PAD_SECONDS' sizing rule with it.
    for profile in afsk.PROFILES:
        assert decode_rate(profile, 0.6) == 0.0, \
            (profile.name, "60% of the sync word destroyed should not decode")
    print("test_a_lock_survives_losing_the_opening_of_the_sync_word OK")


def test_every_keying_fits_the_budget_and_uses_it():
    """No useful DATA framing may outlast afsk.MAX_USEFUL_FRAME_SECONDS,
    and every DATA frame should come as close to it as the format allows.

    Both halves matter. The cap bounds how long the transmitter holds the
    channel, which sets the cost of a single retransmit and the floor under
    turnaround -- see afsk.MAX_USEFUL_FRAME_SECONDS. The tightness is the
    throughput: chunk sizes are derived from the budget rather than chosen,
    so a profile leaving room for another whole byte means the derivation
    has drifted from the arithmetic, not that someone made a judgement
    call.

    A DATA frame is the long one, but the control plane rides the same air,
    so the smaller frame types are checked too rather than assumed."""
    for profile in afsk.PROFILES:
        payload = production_payload_bytes(profile)
        useful = afsk.useful_data_seconds(payload, profile)
        assert useful <= afsk.MAX_USEFUL_FRAME_SECONDS + 1e-9, \
            (profile.name, payload, round(useful, 3))

        # One more byte must not fit. This used to be conditional, because
        # at 1200 baud what stopped us was the 8-bit length field rather
        # than the clock; with framing.LENGTH_FIELD_BITS at 16 the budget
        # binds at every profile and the exemption is gone.
        assert payload < framing.MAX_PAYLOAD_BYTES, \
            f"{profile.name} is capped by the length field again, not the clock"
        assert afsk.useful_data_seconds(payload + 1, profile) > afsk.MAX_USEFUL_FRAME_SECONDS, \
            f"{profile.name} leaves room for a bigger chunk than {profile.chunk_size}"

    print("test_every_keying_fits_the_budget_and_uses_it OK "
          + ", ".join(f"{p.name} {p.chunk_size}B/"
                      f"{afsk.useful_data_seconds(production_payload_bytes(p), p):.2f}s useful"
                      for p in afsk.PROFILES))


def test_afsk_clean_loopback():
    rng = np.random.default_rng(0)
    payload = bytes(rng.integers(0, 256, size=37, dtype=np.uint8))
    tx = afsk.modulate(payload)
    result = afsk.demodulate(tx)
    assert result["synced"], result
    assert result["payload"] == payload, (result["payload"], payload)
    assert result["head_symbols_received"] == len(framing.head_pad_bits(300))
    assert result["tail_symbols_received"] == len(framing.tail_pad_bits(300))
    print("test_afsk_clean_loopback OK")


def test_outer_symbol_measurements_report_clipping():
    payload = b"outer timing probe"
    profile = afsk.PROFILE_300
    sps = round(afsk.SAMPLE_RATE / profile.baud)
    head_clipped = 17
    tail_clipped = 23
    audio = afsk.modulate(payload, profile=profile)
    clipped = audio[head_clipped * sps:len(audio) - tail_clipped * sps]
    # Continuous capture continues after a transmitter is clipped/unkeyed.
    clipped = np.concatenate([clipped, np.zeros(len(audio))])
    result = afsk.demodulate(clipped, profile=profile)
    assert result["payload"] == payload, result
    assert result["head_symbols_received"] == len(framing.head_pad_bits(profile.baud)) - head_clipped
    assert result["tail_symbols_received"] == len(framing.tail_pad_bits(profile.baud)) - tail_clipped
    print("test_outer_symbol_measurements_report_clipping OK")


def test_afsk_noisy_delayed_loopback():
    rng = np.random.default_rng(1)
    payload = bytes(rng.integers(0, 256, size=200, dtype=np.uint8))
    tx = afsk.modulate(payload)
    lead = np.zeros(int(rng.integers(0, 4000)))
    tail = np.zeros(2000)
    gain = 0.3
    noisy = np.concatenate([lead, tx, tail]) * gain
    noisy = noisy + rng.normal(0, 0.02, size=len(noisy))
    result = afsk.demodulate(noisy)
    assert result["synced"], result
    assert result["payload"] == payload, "payload mismatch"
    print("test_afsk_noisy_delayed_loopback OK")


# -- independent receiver clock ---------------------------------------
#
# Everything above this point modulates and demodulates against one clock,
# which is a thing that never happens on air. Two stations have two sound
# cards with two crystals, and nothing disciplines them to each other. The
# tests below put an offset between the two so the suite can express the
# class of bug that a shared clock hides completely.

# A robustness target, deliberately far beyond anything this bench shows.
#
# scripts/measure_clock_offset.py measured the real figure on the two
# radios: -3.7 ppm ic705->ht and +3.1 ppm ht->ic705, summing to -0.6 ppm.
# The two legs being reciprocal to within 0.6 ppm is what says that is a
# genuine clock difference and not an artefact of the method. The two sound
# cards are, for practical purposes, the same clock -- ~100x too close to
# cost a single bit at any frame size the link sends, and never the cause of
# anything failing on this bench.
#
# The tests below are kept anyway, because "the clocks happen to agree
# today" is not a property of the modem. A different radio, a different
# interface, or a colder shack changes it, and a decoder that silently
# depends on it should be known to. 500 ppm is the bar a modem ought to
# clear; it is not a measurement of this bench.
ASSUMED_WORST_CASE_PPM = 500

# The production frame: link.py sends chunk_size bytes of payload plus its
# own type/seq header. This is per profile rather than one number now that
# chunk_size is derived from the useful-frame budget -- 78, 161 and 326
# bytes of AFSK payload at 300, 600 and 1200 baud, all three landing on a
# useful frame of at most afsk.MAX_USEFUL_FRAME_SECONDS. Derived rather than restated so
# these tests keep measuring what the link actually sends.
def production_payload_bytes(profile):
    return profile.chunk_size + afsk.DATA_FRAME_HEADER_BYTES


def resample_clock(audio, ppm):
    """`audio` as heard by a receiver whose sample clock is `ppm` parts per
    million away from the transmitter's.

    A receiver clocking fast takes more samples of the same span of time, so
    the signal arrives stretched: a symbol that was `sps` samples long
    becomes sps*(1+ppm/1e6), and every tone lands at freq/(1+ppm/1e6). Both
    effects come out of the one resampling, which is the point -- a crystal
    offset moves timing and frequency together, and simulating the timing
    alone would be simulating something that cannot physically happen.

    Linear interpolation suffices: the highest tone in use (2200 Hz) is
    oversampled ~22x at 48 kHz, so the interpolation error sits far below
    the noise the other tests already decode through. Verified against a
    known offset in test_clock_offset_simulation_is_faithful.
    """
    audio = np.asarray(audio, dtype=np.float64)
    ratio = 1.0 + ppm * 1e-6
    if ppm == 0:
        return audio.copy()
    n_out = int(len(audio) * ratio)
    return np.interp(np.arange(n_out) / ratio, np.arange(len(audio)), audio)


def _frame_bits(payload_len, baud):
    """Bits from the start of the sync word to the end of the CRC -- the
    span over which a timing error has to stay inside half a symbol.

    Takes baud because the sync word's length does (framing.sync_bits)."""
    return len(framing.sync_bits(baud)) + framing.frame_bits_for_length(payload_len)


def expected_failure(reason):
    """Marks a test that documents a known, unfixed defect.

    The test asserts the behaviour we want, so it fails today. Rather than
    leave the suite red -- which trains everyone to ignore it -- the failure
    is caught and reported as XFAIL. If the test ever *passes*, that is
    itself an error: the defect is fixed and the marker has to come off, so
    the assertion moves from documentation to guarantee. Same contract as
    pytest's strict xfail, without taking a pytest dependency in a suite
    that otherwise runs as a plain script.
    """
    def decorate(fn):
        def wrapper(*args, **kwargs):
            try:
                fn(*args, **kwargs)
            except AssertionError as exc:
                first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
                print(f"{fn.__name__} XFAIL ({reason})" + (f": {first}" if first else ""))
                return
            raise AssertionError(
                f"{fn.__name__} passed, but is marked as a known failure "
                f"({reason}). If the decoder now handles this, remove the "
                f"expected_failure marker so the test guards the fix."
            )
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return decorate


def test_clock_offset_simulation_is_faithful():
    """The offset helper is the instrument every test below reads through,
    so check it against something with a known answer before trusting it: a
    pure tone resampled by `ppm` must come back at freq/(1+ppm/1e6)."""
    seconds, freq = 2.0, 1500.0
    t = np.arange(int(seconds * afsk.SAMPLE_RATE)) / afsk.SAMPLE_RATE
    tone = np.cos(2 * np.pi * freq * t)

    assert np.array_equal(resample_clock(tone, 0), tone), "0 ppm must be identity"

    for ppm in (-1000, -250, 250, 1000):
        shifted = resample_clock(tone, ppm)
        ratio = 1.0 + ppm * 1e-6
        assert abs(len(shifted) - len(tone) * ratio) <= 1, ppm

        # Frequency by phase slope, which resolves far below an FFT bin.
        n = np.arange(len(shifted))
        mixed = shifted * np.exp(-1j * 2 * np.pi * (freq / ratio) * n / afsk.SAMPLE_RATE)
        win = int(afsk.SAMPLE_RATE * 0.002)
        smooth = np.convolve(mixed, np.ones(win) / win, mode="valid")
        phase = np.unwrap(np.angle(smooth))
        tt = np.arange(len(phase)) / afsk.SAMPLE_RATE
        residual_hz = np.polyfit(tt, phase, 1)[0] / (2 * np.pi)
        # Expected tone is freq/ratio; anything left over is helper error.
        assert abs(residual_hz) < 0.05, (ppm, residual_hz)
    print("test_clock_offset_simulation_is_faithful OK")


def test_decodes_through_a_small_clock_offset():
    """Well-matched clocks must not be a problem at any frame size the link
    actually sends. This is the regression guard for whatever fixes the
    larger offsets -- a timing estimator that helps at 500 ppm and hurts at
    50 would pass the tests below and still make the link worse."""
    rng = np.random.default_rng(11)
    for profile in afsk.PROFILES:
        for n in (2, production_payload_bytes(profile), 160):
            payload = bytes(rng.integers(0, 256, size=n, dtype=np.uint8))
            tx = afsk.modulate(payload, profile=profile)
            for ppm in (-50, 0, 50):
                rx = resample_clock(tx, ppm)
                result = afsk.demodulate(rx, profile=profile)
                assert result.get("payload") == payload, \
                    (profile.name, n, ppm, result.get("confidence"))
    print("test_decodes_through_a_small_clock_offset OK")


def test_clock_offset_tolerance_is_half_a_symbol_over_the_frame():
    """Characterises exactly where today's decoder gives up, because the
    shape of that boundary is what identifies the cause.

    afsk.demodulate lays symbol sample points on a rigid grid of integer
    `sps` from the sync peak, with no timing recovery, so a clock offset
    accumulates a sampling error that grows along the frame. The frame dies
    when that error reaches half a symbol, which puts the tolerance at
    0.5/n_bits -- inversely proportional to frame length. It is very nearly
    independent of baud too, since a faster profile has proportionally
    shorter symbols; the sync word's length now scales with baud
    (framing.sync_bits), so n_bits differs a little per profile for the same
    payload and the bound is computed per profile rather than once.

    Both of those are the signature the bench saw: a ceiling in *payload
    bytes* that sat at the same byte count for all three profiles. Gradual
    SNR falloff does not do that, and neither does anything in the RF path.

    When timing recovery lands, this test fails -- that is the point of it.
    Re-measure the boundary and update the bound; do not delete the test.
    """
    rng = np.random.default_rng(12)
    for profile in afsk.PROFILES:
        for n in (40, 120, 200):
            payload = bytes(rng.integers(0, 256, size=n, dtype=np.uint8))
            tx = afsk.modulate(payload, profile=profile)
            predicted = 0.5e6 / _frame_bits(n, profile.baud)

            # Comfortably inside the predicted boundary: must decode.
            inside = resample_clock(tx, round(predicted * 0.6))
            assert afsk.demodulate(inside, profile=profile).get("payload") == payload, \
                ("expected decode inside the half-symbol bound", profile.name, n)

            # Comfortably outside it: must not. If this half starts passing,
            # the decoder has gained timing tolerance from somewhere.
            outside = resample_clock(tx, round(predicted * 2.0))
            assert afsk.demodulate(outside, profile=profile).get("payload") != payload, \
                ("expected failure outside the half-symbol bound", profile.name, n)
    print("test_clock_offset_tolerance_is_half_a_symbol_over_the_frame OK")


@expected_failure("no symbol timing recovery in afsk.demodulate")
def test_decodes_at_production_size_under_bench_clock_offset():
    """The requirement, stated as the link needs it: a production-sized
    frame survives the clock offset two independent sound cards can present,
    in either direction, on every profile.

    Fails today, for the reason in
    test_clock_offset_tolerance_is_half_a_symbol_over_the_frame: the
    decoder's half-symbol budget is 0.5/n_bits, so each profile's production
    frame has its own tolerance --

        300 baud    78 bytes   1319 bits    ~379 ppm
        600 baud   161 bytes   2647 bits    ~189 ppm
       1200 baud   326 bytes   5295 bits     ~94 ppm

    -- and the two faster profiles are the ones that cannot hold 500. Note
    the shape: the frames are sized by *airtime*, so a faster profile spends
    its budget on more bits, and more bits is exactly what this decoder
    cannot keep timing across. The tolerance therefore falls as the link
    speeds up, which is the opposite of the reassuring direction. It costs
    nothing on this bench (the two cards measure 3.4 ppm apart, ~70x inside
    even the 1200 baud figure) and it is the first thing to check on
    hardware whose clocks are not this close.
    """
    rng = np.random.default_rng(13)
    failures = []
    for profile in afsk.PROFILES:
        payload = bytes(rng.integers(0, 256, size=production_payload_bytes(profile),
                                     dtype=np.uint8))
        tx = afsk.modulate(payload, profile=profile)
        for ppm in (-ASSUMED_WORST_CASE_PPM, ASSUMED_WORST_CASE_PPM):
            result = afsk.demodulate(resample_clock(tx, ppm), profile=profile)
            if result.get("payload") != payload:
                failures.append((profile.name, ppm, round(result.get("confidence") or 0, 3)))
    assert not failures, f"no decode at (profile, ppm, confidence): {failures}"
    print("test_decodes_at_production_size_under_bench_clock_offset OK")


@expected_failure("no symbol timing recovery in afsk.demodulate")
def test_long_frames_lose_to_clock_offset_before_short_ones():
    """Long frames are the first thing a clock offset takes away.

    Worth pinning down because it is a trap, not because it is currently
    biting: a decoder with no timing recovery degrades in a way that looks
    exactly like a frame-size ceiling, and the bench has had frame-size
    ceilings that were nothing of the kind. The clocks measure 3.4 ppm
    apart, far too close to be the cause of any of them -- but from the
    decoder's output alone a clock ceiling and any other kind are
    indistinguishable, so if a size ceiling shows up, measure the clocks
    before assuming it is the same thing twice.
    """
    rng = np.random.default_rng(14)
    profile = afsk.PROFILE_600
    ppm = 400  # inside 120 bytes' budget (478 ppm), outside 160 bytes' (366)

    small = bytes(rng.integers(0, 256, size=120, dtype=np.uint8))
    large = bytes(rng.integers(0, 256, size=160, dtype=np.uint8))
    small_rx = afsk.demodulate(resample_clock(afsk.modulate(small, profile=profile), ppm),
                               profile=profile)
    large_rx = afsk.demodulate(resample_clock(afsk.modulate(large, profile=profile), ppm),
                               profile=profile)

    # The 120-byte half is not the defect and must hold regardless.
    assert small_rx.get("payload") == small, "120 bytes should survive 400 ppm"
    assert large_rx.get("payload") == large, \
        f"160 bytes failed at {ppm} ppm (confidence {large_rx.get('confidence')})"
    print("test_long_frames_lose_to_clock_offset_before_short_ones OK")


def test_high_confidence_survives_the_offset_that_kills_the_payload():
    """Why the ceiling looked mysterious rather than obvious.

    The sync word is 127 symbols at this profile; a frame is ten times that.
    An offset that has barely moved the sampling point by the end of it has
    walked most of a symbol by the end of the payload, so the receiver
    reports a near-perfect lock and then fails CRC. Any diagnostic that
    reads confidence as "the frame arrived cleanly" is reading the first 5%
    of the frame.
    """
    rng = np.random.default_rng(15)
    profile = afsk.PROFILE_600
    payload = bytes(rng.integers(0, 256, size=160, dtype=np.uint8))
    tx = afsk.modulate(payload, profile=profile)

    result = afsk.demodulate(resample_clock(tx, 800), profile=profile)
    assert result.get("payload") is None, "expected the frame to fail at 800 ppm"
    assert result.get("confidence", 0) > 0.9, \
        f"expected a high-confidence near-miss, got {result.get('confidence')}"

    # And the length field still reads correctly, because it sits in the
    # bits immediately after the sync word, where the accumulated error is
    # still negligible. This is worth
    # pinning down: the sweep scripts reported a near-miss end_index ~1.2x
    # the expected frame length and it was read as a false sync lock on
    # garbage. It is not -- end_index is an absolute offset into the RX
    # buffer, and the frame simply started ~1s into it.
    assert "end_index" in result and "start_index" in result, result
    span = result["end_index"] - result["start_index"]
    sps = round(afsk.SAMPLE_RATE / profile.baud)
    expected_bits = _frame_bits(len(payload), profile.baud)
    assert abs(span - sps * expected_bits) < sps, \
        ("length field decoded wrong", span, sps * expected_bits)
    print("test_high_confidence_survives_the_offset_that_kills_the_payload OK")


def _hand_built_frame(profile, length_field, payload_bytes):
    """Audio for a frame whose length field is written directly, so it can
    disagree with the payload that follows. modulate() cannot express this
    -- it derives the field from the payload, which is the whole point."""
    bits = (framing.head_pad_bits(profile.baud) + framing.sync_bits(profile.baud)
            + framing.bytes_to_bits(
                length_field.to_bytes(framing.LENGTH_FIELD_BITS // 8, "big") + payload_bytes)
            + framing.tail_pad_bits(profile.baud))
    sps = round(afsk.SAMPLE_RATE / profile.baud)
    return afsk._cpfsk_tone(bits, sps, afsk.SAMPLE_RATE, profile.freq0, profile.freq1)


def test_an_implausible_declared_length_is_a_dead_sync_not_a_frame_in_flight():
    """The hazard the 16-bit length field introduces, and the check that
    answers it.

    Nothing can validate a length field before buffering everything it
    claims -- the CRC sits after the payload it describes -- so the decoder
    has to judge the claim on its face. A false sync on noise yields a
    uniformly random 16-bit value, and at 1200 baud ~98% of those describe a
    frame longer than transport.RX_BUFFER_SECONDS can ever hold. Reporting
    one of those as 'still arriving' tells whale/link.py's decode loop to
    stop pruning and re-search the whole buffer every poll (see
    _prune_stale), which lands straight on the turnaround.

    Under the old 8-bit field this could not happen: 255 bytes at 300 baud
    is 7.1s, inside the buffer, so every value a garbage length byte could
    take was one the decoder would collect in full and reject on CRC. That
    property is now explicit rather than incidental -- see
    afsk.MAX_CREDIBLE_FRAME_SECONDS.
    """
    profile = afsk.PROFILE_600
    audio = _hand_built_frame(profile, 60000, b"nowhere near 60000 bytes")

    result = afsk.demodulate(audio, profile=profile)
    assert result.get("payload") is None, "a frame this broken must not decode"
    assert result.get("confidence", 0) >= profile.confidence_threshold, \
        f"the sync word should still lock, got {result.get('confidence')}"
    assert "end_index" in result, \
        "an unwaitable declared length must read as a dead sync, not as still-arriving"
    print("test_an_implausible_declared_length_is_a_dead_sync_not_a_frame_in_flight OK")


def test_a_plausible_frame_still_reads_as_still_arriving_while_incomplete():
    """The other half, and the reason the check is a bound rather than a
    blanket refusal: a real frame that is genuinely half-way through must
    still suppress end_index, or the decode loop discards frames mid-flight
    and pays a retransmit for each one."""
    profile = afsk.PROFILE_600
    payload = bytes([link.PT_DATA, 0]) + b"x" * 120
    full = afsk.modulate(payload, profile=profile)

    # Cut just past the length field, so the declared length is readable and
    # credible but most of the payload has yet to arrive.
    head = len(framing.head_pad_bits(profile.baud)) + len(framing.sync_bits(profile.baud)) + 32
    sps = round(afsk.SAMPLE_RATE / profile.baud)
    partial = full[:head * sps]

    result = afsk.demodulate(partial, profile=profile)
    assert result.get("payload") is None, "the frame is not complete yet"
    assert "end_index" not in result, \
        "a credible length still arriving must not be reported as a dead end"

    # And the whole thing decodes once it has all landed.
    assert afsk.demodulate(full, profile=profile).get("payload") == payload
    print("test_a_plausible_frame_still_reads_as_still_arriving_while_incomplete OK")


def test_link_packet_roundtrip():
    """Same shape as whale.link's packet encode: type byte + body, through
    modulate/demodulate."""
    from whale.link import PT_DATA, EOF_BIT

    body = bytes([0x00 | EOF_BIT]) + b"x" * 200
    payload = bytes([PT_DATA]) + body
    tx = afsk.modulate(payload)
    result = afsk.demodulate(tx)
    assert result["payload"] == payload
    print("test_link_packet_roundtrip OK")


def test_connect_body_roundtrip():
    body = link._encode_call_and_modes("STA1", "STA2", [0, 1], 1, 0x5A)
    a, b, supported, extra, session = link._decode_call_and_modes(body)
    assert (a, b, supported, extra, session) == ("STA1", "STA2", [0, 1], 1, 0x5A), \
        (a, b, supported, extra, session)
    print("test_connect_body_roundtrip OK")


def test_connect_ack_body_roundtrip():
    body = link._encode_connect_ack("STA2", "STA1", [0, 1, 2], 1, 0, 0x5A)
    a, b, supported, accepted_id, own_id, session = link._decode_connect_ack(body)
    assert (a, b, supported, accepted_id, own_id, session) == \
        ("STA2", "STA1", [0, 1, 2], 1, 0, 0x5A), \
        (a, b, supported, accepted_id, own_id, session)
    print("test_connect_ack_body_roundtrip OK")


def test_negotiate_mode():
    assert link._negotiate_mode([0, 1], 1) == 1
    assert link._negotiate_mode([0], 1) == afsk.CONTROL_PROFILE.mode_id
    assert link._negotiate_mode([0, 1], 0) == 0
    print("test_negotiate_mode OK")


def test_link_uses_waveform_mode_contract():
    """Link dispatches through a mode's codec rather than CPFSK directly."""
    class SpyCodec:
        sample_rate = afsk.SAMPLE_RATE

        def __init__(self):
            self.encoded = 0
            self.decoded = 0

        def encode(self, payload, profile, *, include_head=True, include_tail=True):
            self.encoded += 1
            return afsk.modulate(payload, profile=profile, include_head=include_head,
                                 include_tail=include_tail)

        def decode(self, audio, profile):
            self.decoded += 1
            return afsk.demodulate(audio, profile=profile)

        def airtime(self, payload_len, profile):
            return afsk.frame_seconds(payload_len, profile)

    codec = SpyCodec()
    custom = afsk.Profile(
        name="custom-waveform", mode_id=42, baud=600, freq0=700, freq1=1500,
        chunk_size=80, codec=codec)
    registry = ModeRegistry((afsk.PROFILE_300, custom), afsk.PROFILE_300)
    ta, tb = _FakeTransport(), _FakeTransport()
    ta.peer, tb.peer = tb, ta
    a = link.Link(ta, "STA1", mode_registry=registry)
    b = link.Link(tb, "STA2", mode_registry=registry)
    a._apply_tx_profile(custom)
    b._apply_rx_profile(custom)
    a._await_turnaround = lambda: None

    a._tx_packet(link.PT_DATA, bytes([link.EOF_BIT]) + b"contract")
    assert codec.encoded == 1
    assert b._decode_one(tb.snapshot_rx())
    assert codec.decoded >= 1
    ptype, body = b._rx_packets.get_nowait()
    assert ptype == link.PT_DATA
    assert body[1:] == b"contract"
    print("test_link_uses_waveform_mode_contract OK")


def test_mode_change_packet_roundtrip():
    """PT_MODE_REQ/PT_MODE_ACK bodies through modulate/demodulate at
    PROFILE_600 -- confirms the existing framing/codec machinery, already
    profile-parameterized, works unchanged at a non-control profile."""
    for ptype in (link.PT_MODE_REQ, link.PT_MODE_ACK):
        payload = bytes([ptype, afsk.PROFILE_600.mode_id])
        tx = afsk.modulate(payload, profile=afsk.PROFILE_600)
        result = afsk.demodulate(tx, profile=afsk.PROFILE_600)
        assert result["payload"] == payload, (ptype, result)
    print("test_mode_change_packet_roundtrip OK")


def test_demodulate_returns_the_earliest_frame_not_the_loudest():
    """The RX buffer can hold more than one sync-like thing -- a garbled
    self-echo of our own last transmission alongside the peer's reply, say
    -- so demodulate() has to return the *earliest* frame and the caller has
    to be able to walk the buffer using end_index. The third frame here is
    deliberately the loudest: under the old argmax-only sync search it would
    have been decoded first and the two before it thrown away unread."""
    payloads = [bytes([afsk.PROFILE_600.mode_id]) + b"first",
                b"second" * 10,
                b"third and loudest"]
    frames = [afsk.modulate(p, profile=afsk.PROFILE_600) for p in payloads]
    frames[2] = frames[2] * 3.0
    rng = np.random.default_rng(7)
    audio = np.concatenate([np.zeros(2000, dtype=np.float32)] + frames)
    audio = audio + rng.normal(0, 0.01, size=len(audio)).astype(np.float32)

    for expected in payloads:
        result = afsk.demodulate(audio, profile=afsk.PROFILE_600)
        assert result.get("payload") == expected, (expected, result.get("payload"))
        audio = audio[result["end_index"]:]
    print("test_demodulate_returns_the_earliest_frame_not_the_loudest OK")


def test_sync_confidence_does_not_depend_on_surrounding_silence():
    """The same frame must score the same whether it sits in six seconds of
    idle noise or fills the buffer on its own -- the old
    peak/median-noise-floor measure collapsed from 255 to 12 across exactly
    that change, close enough to its own threshold that noise could
    outscore a frame."""
    prof = afsk.PROFILE_600
    rng = np.random.default_rng(4)
    payload = bytes([link.PT_DATA, 0]) + b"burst" * 12
    frame = afsk.modulate(payload, profile=prof)

    def noisy(x):
        return (x + rng.normal(0, 0.05, size=len(x))).astype(np.float32)

    quiet = np.zeros(int(3.0 * afsk.SAMPLE_RATE), dtype=np.float32)
    sparse = afsk.demodulate(noisy(np.concatenate([quiet, frame, quiet])), profile=prof)
    dense = afsk.demodulate(noisy(frame), profile=prof)
    assert sparse.get("payload") == payload and dense.get("payload") == payload

    assert abs(sparse["confidence"] - dense["confidence"]) < 0.1, \
        (sparse["confidence"], dense["confidence"])
    assert min(sparse["confidence"], dense["confidence"]) > prof.confidence_threshold

    # Noise alone must stay well clear of the threshold, in a buffer of any
    # length: real off-air recordings scored 7.8-214 under the old measure
    # against a threshold of 4.0.
    for seconds in (0.5, 2.0, 6.0):
        n = noisy(np.zeros(int(seconds * afsk.SAMPLE_RATE), dtype=np.float32))
        result = afsk.demodulate(n, profile=prof)
        assert result.get("payload") is None
        assert result.get("confidence", 0.0) < prof.confidence_threshold, \
            (seconds, result.get("confidence"))
    print("test_sync_confidence_does_not_depend_on_surrounding_silence OK")


def test_seq_ahead_wraps():
    assert link._seq_ahead(5, 3) == 2
    assert link._seq_ahead(3, 3) == 0
    assert link._seq_ahead(0, link.SEQ_MODULO - 1) == 1  # the wrap itself
    print("test_seq_ahead_wraps OK")


def test_await_turnaround_is_anchored_on_peer_audio():
    """The wait is measured from when the peer's audio ended, so time
    already spent decoding does not get charged twice."""
    a = link.Link(_FakeTransport(), "STA1")
    saved = link.TX_TURNAROUND_DELAY
    link.TX_TURNAROUND_DELAY = 0.4
    try:
        # An anchor from seconds ago does not mean the channel went quiet
        # seconds ago -- it means we lost track of the peer that long ago,
        # which is the worst moment to key up. Wait in full instead.
        a._peer_unkeyed_at = time.monotonic() - 5.0
        start = time.monotonic()
        a._await_turnaround()
        assert time.monotonic() - start >= 0.35, "stale anchor must not skip the wait"

        # Anchor is consumed, so the next transmission has nothing to
        # measure from and waits the whole allowance.
        assert a._peer_unkeyed_at is None
        start = time.monotonic()
        a._await_turnaround()
        assert time.monotonic() - start >= 0.35, "no anchor should wait in full"

        # A fresh anchor waits out only the remainder.
        a._peer_unkeyed_at = time.monotonic() - 0.3
        start = time.monotonic()
        a._await_turnaround()
        waited = time.monotonic() - start
        assert 0.02 < waited < 0.25, waited
    finally:
        link.TX_TURNAROUND_DELAY = saved
    print("test_await_turnaround_is_anchored_on_peer_audio OK")


def test_link_multi_chunk_message_roundtrip():
    """A whole message across several chunks, each acked before the next
    goes out. Sequence numbers are started near the top of the space so the
    transfer runs through a wrap."""
    a, b, ta, tb = _connected_pair()
    try:
        a._tx_seq = link.SEQ_MODULO - 3
        b._rx_expect_seq = link.SEQ_MODULO - 3

        data = bytes((i * 7 + 11) % 256 for i in range(400))
        got = _transfer(a, b, data)

        assert got == data, (len(got or b""), len(data))
        # Both ends must agree on where the sequence got to, having wrapped
        # through zero on the way.
        assert a._tx_seq == b._rx_expect_seq, (a._tx_seq, b._rx_expect_seq)
        assert a._tx_seq < link.SEQ_MODULO - 3, f"sequence never wrapped (at {a._tx_seq})"
        print(f"test_link_multi_chunk_message_roundtrip OK "
              f"({len(data)} bytes, seq wrapped to {a._tx_seq})")
    finally:
        a.stop()
        b.stop()


def test_link_recovers_from_lost_data_frame():
    """The first DATA frame never arrives. The sender must retransmit it
    and the message must still come out byte for byte."""
    a, b, ta, tb = _connected_pair()
    try:
        a.data_ack_timeout = 2.0
        ta.corrupt = _silence_once(ta)

        data = bytes((i * 3 + 1) % 256 for i in range(300))
        got = _transfer(a, b, data)
        assert got == data, (len(got or b""), len(data))
        assert ta.corrupt is None, "the corruption never fired"
        print("test_link_recovers_from_lost_data_frame OK")
    finally:
        a.stop()
        b.stop()


def test_link_survives_lost_ack_without_duplicating_data():
    """The receiver's first ACK is lost, so the sender retransmits a chunk
    the receiver has already taken. It must be recognised as a duplicate and
    dropped rather than appended twice, and the re-sent ACK must still move
    the sender forward."""
    a, b, ta, tb = _connected_pair()
    try:
        a.data_ack_timeout = 2.0
        tb.corrupt = _silence_once(tb)

        data = bytes((i * 5 + 3) % 256 for i in range(300))
        got = _transfer(a, b, data)
        assert got == data, (len(got or b""), len(data))
        assert tb.corrupt is None, "the corruption never fired"
        print("test_link_survives_lost_ack_without_duplicating_data OK")
    finally:
        a.stop()
        b.stop()


def _arq_sender(acks):
    """A Link wired up to transmit nowhere, answering each keying from
    `acks` (a list of DATA_ACK bodies). Returns (link, keyings)."""
    a = link.Link(_FakeTransport(), "STA1")
    a.state = "CONNECTED"
    a.data_ack_timeout = 1.0
    keyings = []
    a._tx_packet = lambda ptype, body: keyings.append(body)
    a._await_turnaround = lambda: None
    queue_ = list(acks)
    a._wait_packet = lambda types, timeout: (
        (link.PT_DATA_ACK, queue_.pop(0)) if queue_ else None)
    return a, keyings


def test_spare_ack_for_an_earlier_chunk_does_not_provoke_a_retransmit():
    """The bug a cumulative-only ACK caused, and the reason DATA_ACK carries
    the frame it answers.

    The receiver acks every DATA it decodes, duplicates included, so one
    lost ACK leaves a spare copy queued at the sender. Under a bare
    "next expected" ACK that spare reads as "the frame you just sent did not
    arrive": the sender retransmits for nothing, the retransmit is itself a
    duplicate and draws another spare, and the link settles into two keyings
    per chunk for the rest of the session."""
    # Waiting on 0x07. First the spare ack for 0x06 -- which names 0x07 as
    # what the peer wants next, exactly the value the frame in flight would
    # be acked with -- then the real answer.
    a, keyings = _arq_sender([bytes([0x06, 0x07]), bytes([0x07, 0x08])])
    assert a._send_chunk_with_arq(0x07, b"aaaa", False) == 1
    assert len(keyings) == 1, f"{len(keyings)} keyings for one chunk -- retransmitted on a stale ACK"
    print("test_spare_ack_for_an_earlier_chunk_does_not_provoke_a_retransmit OK")


def test_ack_for_a_duplicate_still_advances_the_sender():
    """The other half of the same format: when the sender retransmits after
    a lost ACK, the peer's answer is about a frame it has already taken and
    moved past. That must still count as acked, or the transfer stalls."""
    a, keyings = _arq_sender([bytes([0x07, 0x08])])
    assert a._send_chunk_with_arq(0x07, b"aaaa", True) == 1
    print("test_ack_for_a_duplicate_still_advances_the_sender OK")


def test_unanswered_chunk_gives_up_after_max_retries():
    """A chunk the peer never decodes draws no ACK at all -- the receiver
    only ever transmits in response to a frame it decoded -- so the timeout
    is the only signal, and the sender must exhaust its retries and report
    failure rather than blocking forever."""
    a, keyings = _arq_sender([])
    a.data_ack_timeout = 0.05
    assert a._send_chunk_with_arq(0x07, b"aaaa", True) is None
    assert len(keyings) == link.MAX_RETRIES, keyings
    print("test_unanswered_chunk_gives_up_after_max_retries OK")


def test_roles_assigned_at_connect():
    """The connecting station starts holding the floor (ISS); the listener
    starts waiting for it (IRS) -- mirroring how PACTOR/VARA/WINMOR-style ARQ
    modems assign ISS/IRS at connect time rather than letting either side
    originate DATA on a whim."""
    a, b, ta, tb = _connected_pair()
    try:
        assert a.role == "ISS", a.role
        assert b.role == "IRS", b.role
        print("test_roles_assigned_at_connect OK")
    finally:
        a.stop()
        b.stop()


def test_irs_can_request_and_use_the_floor():
    """The listening side (IRS) may not originate DATA until it asks the
    connecting side (ISS) for the floor. send_message() must do that
    transparently -- request, wait for grant, then send -- and both ends'
    roles must flip once it's granted."""
    a, b, ta, tb = _connected_pair()
    try:
        assert a.role == "ISS" and b.role == "IRS", (a.role, b.role)
        data = bytes((i * 17 + 5) % 256 for i in range(300))
        got = _transfer(b, a, data)  # b (IRS) sends, a (ISS) receives
        assert got == data, (len(got or b""), len(data))
        assert b.role == "ISS" and a.role == "IRS", (a.role, b.role)
        print("test_irs_can_request_and_use_the_floor OK")
    finally:
        a.stop()
        b.stop()


def _pump_send_recv(link_, outbox, inbox, stop_event, timeout=0.3):
    """Minimal stand-in for vara_server.py's session pump: alternates trying
    to send whatever's queued in `outbox` (mutated like a list-backed queue)
    with polling for incoming messages, appending anything received to
    `inbox`. Used to reproduce the scenario that used to let both ends key up
    DATA over each other -- both sides deciding to send at once -- without
    pulling in vara_server.py's sockets."""
    while not stop_event.is_set():
        if outbox:
            data = outbox.pop(0)
            link_.send_message(data)
            continue
        msg = link_.recv_message(timeout=timeout)
        if msg is not None:
            inbox.append(msg)


def test_concurrent_send_attempts_do_not_collide():
    """Both sides decide to send at once -- the scenario that used to let
    both key up PT_DATA over each other with nothing arbitrating who goes
    first. With ISS/IRS roles, only the ISS may originate DATA; the IRS's
    send_message() blocks acquiring the floor first instead of colliding
    with it. Both messages must still arrive intact and neither pump loop
    may raise."""
    a, b, ta, tb = _connected_pair()
    try:
        a.control_ack_timeout = 0.3  # keep a lost floor request's retry fast
        b.control_ack_timeout = 0.3

        data_ab = bytes((i * 11 + 1) % 256 for i in range(300))
        data_ba = bytes((i * 13 + 2) % 256 for i in range(300))

        a_out, b_out = [data_ab], [data_ba]
        a_in, b_in = [], []
        stop_a, stop_b = threading.Event(), threading.Event()

        thread_a = threading.Thread(target=_pump_send_recv, args=(a, a_out, a_in, stop_a))
        thread_b = threading.Thread(target=_pump_send_recv, args=(b, b_out, b_in, stop_b))
        thread_a.start()
        thread_b.start()

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (a_in and b_in):
            time.sleep(0.05)

        stop_a.set()
        stop_b.set()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)

        assert a_in and a_in[0] == data_ba, "A never received B's message intact"
        assert b_in and b_in[0] == data_ab, "B never received A's message intact"
        print("test_concurrent_send_attempts_do_not_collide OK")
    finally:
        a.stop()
        b.stop()


def test_link_negotiation_and_mode_step():
    """Each direction of the link negotiates and adapts independently: A's
    TX rate to B need not match B's TX rate to A (see whale/afsk.py's
    measured per-direction SNR on the real bench, which is what motivates
    this)."""
    link.TX_TURNAROUND_DELAY = 0.05  # keep the test fast; real hardware needs the settling time, this doesn't

    ta, tb = _FakeTransport(), _FakeTransport()
    ta.peer, tb.peer = tb, ta
    history = {}
    a = link.Link(ta, "STA1", mode_history_store=history)
    b = link.Link(tb, "STA2", mode_history_store=history)
    a.start()
    b.start()
    try:
        # History says STA1->STA2 last spoke at PROFILE_600, but STA2->STA1
        # has no history at all -- connect should bring up an asymmetric
        # link: A's tx (and B's rx) at 600baud, B's tx (and A's rx) still
        # at the control profile since B has nothing to go on yet.
        mode_history.record_good_mode(history, "STA1", "STA2", afsk.PROFILE_600.mode_id)

        listen_result = {}

        def do_listen():
            listen_result["peer"] = b.listen_once(timeout=20)

        t = threading.Thread(target=do_listen)
        t.start()
        ok = a.connect("STA2", retries=3)
        t.join(timeout=20)

        assert ok, "connect() failed"
        assert listen_result["peer"] == "STA1", listen_result
        assert a.tx_profile.mode_id == afsk.PROFILE_600.mode_id, a.tx_profile
        assert a.rx_profile.mode_id == afsk.CONTROL_PROFILE.mode_id, a.rx_profile
        assert b.rx_profile.mode_id == afsk.PROFILE_600.mode_id, b.rx_profile
        assert b.tx_profile.mode_id == afsk.CONTROL_PROFILE.mode_id, b.tx_profile
        assert a.peer_supported_modes == {p.mode_id for p in afsk.PROFILES}
        assert b.peer_supported_modes == {p.mode_id for p in afsk.PROFILES}

        # Mid-session step down of A's tx (600 -> 300): B must be listening
        # (recv_message) to catch and ack A's PT_MODE_REQ. B's own tx
        # direction (already at the control profile) must be unaffected.
        def do_recv():
            b.recv_message(timeout=20)

        t = threading.Thread(target=do_recv)
        t.start()
        a._request_mode_step(-1)
        t.join(timeout=20)

        assert a.tx_profile.mode_id == afsk.PROFILE_300.mode_id, a.tx_profile
        assert b.rx_profile.mode_id == afsk.PROFILE_300.mode_id, b.rx_profile
        assert b.tx_profile.mode_id == afsk.CONTROL_PROFILE.mode_id, b.tx_profile
        print("test_link_negotiation_and_mode_step OK")
    finally:
        a.stop()
        b.stop()


if __name__ == "__main__":
    test_framing_roundtrip()
    test_sync_words_are_full_period_m_sequences()
    test_a_frame_does_not_sync_another_profiles_correlator()
    test_a_lock_survives_losing_the_opening_of_the_sync_word()
    test_every_keying_fits_the_budget_and_uses_it()
    test_afsk_clean_loopback()
    test_afsk_noisy_delayed_loopback()
    test_clock_offset_simulation_is_faithful()
    test_decodes_through_a_small_clock_offset()
    test_clock_offset_tolerance_is_half_a_symbol_over_the_frame()
    test_high_confidence_survives_the_offset_that_kills_the_payload()
    test_decodes_at_production_size_under_bench_clock_offset()
    test_long_frames_lose_to_clock_offset_before_short_ones()
    test_an_implausible_declared_length_is_a_dead_sync_not_a_frame_in_flight()
    test_a_plausible_frame_still_reads_as_still_arriving_while_incomplete()
    test_link_packet_roundtrip()
    test_connect_body_roundtrip()
    test_connect_ack_body_roundtrip()
    test_negotiate_mode()
    test_link_uses_waveform_mode_contract()
    test_mode_change_packet_roundtrip()
    test_demodulate_returns_the_earliest_frame_not_the_loudest()
    test_sync_confidence_does_not_depend_on_surrounding_silence()
    test_seq_ahead_wraps()
    test_await_turnaround_is_anchored_on_peer_audio()
    test_roles_assigned_at_connect()
    test_irs_can_request_and_use_the_floor()
    test_concurrent_send_attempts_do_not_collide()
    test_spare_ack_for_an_earlier_chunk_does_not_provoke_a_retransmit()
    test_ack_for_a_duplicate_still_advances_the_sender()
    test_unanswered_chunk_gives_up_after_max_retries()
    test_link_negotiation_and_mode_step()
    test_link_multi_chunk_message_roundtrip()
    test_link_recovers_from_lost_data_frame()
    test_link_survives_lost_ack_without_duplicating_data()
    print("all tests OK")
