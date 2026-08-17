"""Pure-software checks for framing + afsk -- no hardware, no radios.

Run: python tests/test_afsk_loopback.py
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from whale import afsk, framing, link, mode_history


def test_framing_roundtrip():
    for payload in (b"", b"hello", bytes(range(256))[:255]):
        bits = framing.build_frame_bits(payload)
        after_sync = len(framing.head_pad_bits(300)) + len(framing.SYNC_BITS)
        decoded = framing.parse_frame_bits(bits[after_sync:])
        assert decoded == payload, (payload, decoded)
    print("test_framing_roundtrip OK")


def test_afsk_clean_loopback():
    rng = np.random.default_rng(0)
    payload = bytes(rng.integers(0, 256, size=37, dtype=np.uint8))
    tx = afsk.modulate(payload)
    result = afsk.demodulate(tx)
    assert result["synced"], result
    assert result["payload"] == payload, (result["payload"], payload)
    print("test_afsk_clean_loopback OK")


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
# cost a single bit at any frame size the link sends. Clock offset was
# never the cause of anything failing on this bench; that was priority scan
# on the HT, see scripts/probe_tx_duration_dropout.py.
#
# The tests below are kept anyway, because "the clocks happen to agree
# today" is not a property of the modem. A different radio, a different
# interface, or a colder shack changes it, and a decoder that silently
# depends on it should be known to. 500 ppm is the bar a modem ought to
# clear; it is not a measurement of this bench.
ASSUMED_WORST_CASE_PPM = 500

# The production frame: link.py sends chunk_size=100 bytes of payload plus
# its own type/seq header, so the AFSK payload is 102 bytes. The bench
# sweeps put the ceiling just above this -- 120 bytes decoded 100% both
# directions, 160 bytes failed 0/5 one way.
PRODUCTION_PAYLOAD_BYTES = 102


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


def _frame_bits(payload_len):
    """Bits from the start of the sync word to the end of the CRC -- the
    span over which a timing error has to stay inside half a symbol."""
    return len(framing.SYNC_BITS) + 8 + 8 * payload_len + 16


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
        for n in (2, PRODUCTION_PAYLOAD_BYTES, 160):
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
    0.5/n_bits -- inversely proportional to frame length, and independent of
    baud, since a faster profile has proportionally shorter symbols.

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
            predicted = 0.5e6 / _frame_bits(n)

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

    Fails today, at every profile, for the reason in
    test_clock_offset_tolerance_is_half_a_symbol_over_the_frame: 102 bytes
    is 903 bits, so the decoder's half-symbol budget runs out at ~550 ppm
    and this asks for 500 in both directions with no margin for the sync
    peak landing a fraction of a symbol off.
    """
    rng = np.random.default_rng(13)
    failures = []
    for profile in afsk.PROFILES:
        payload = bytes(rng.integers(0, 256, size=PRODUCTION_PAYLOAD_BYTES, dtype=np.uint8))
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
    exactly like a frame-size ceiling, and the bench did have a frame-size
    ceiling. It was not this -- the clocks measure 8 ppm apart and the real
    cause is a receiver dropout, see
    scripts/probe_tx_duration_dropout.py. But the two are indistinguishable
    from the decoder's output alone, so if a size ceiling ever shows up
    again, measure the clocks before assuming it is the same thing twice.
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


# -- mid-frame receiver dropout ---------------------------------------
#
# The failure the bench actually had, kept because the shape of it will
# recur with any scanning receiver. Priority scan was enabled on the HT,
# muting it for ~280ms about 3.0s after PTT key-down and again roughly
# every 3.1s. Nothing to do with the modulation, the tones, or frame size.
#
# It was mistaken for a frame-size ceiling because a bigger payload is also
# a longer keying, and every sweep varied both at once. A 40-byte frame -- a
# size the sweeps recorded as 100% reliable -- failed 0/4 when the padding
# in front of it put it on air across the 3.0s mark, and went back to 4/4
# on both sides of that band. With the scan off the ceiling disappeared
# entirely: 255-byte payloads pass 100% both directions at 600 and 1200
# baud.

DROPOUT_SECONDS = 0.28


def punch_dropout(audio, at_seconds, duration=DROPOUT_SECONDS):
    """Silences `duration` seconds of `audio` starting at `at_seconds`,
    the way the HT's receiver silences a span of a transmission."""
    audio = np.asarray(audio, dtype=np.float64).copy()
    start = int(at_seconds * afsk.SAMPLE_RATE)
    end = min(len(audio), start + int(duration * afsk.SAMPLE_RATE))
    if start < len(audio):
        audio[start:end] = 0.0
    return audio


def test_a_dropout_outside_the_frame_is_harmless():
    """The requirement the link actually depends on. Retransmits have to
    land in a clear window and decode there, even though the buffer they
    arrive in still holds the hole that killed the previous attempt."""
    rng = np.random.default_rng(16)
    profile = afsk.PROFILE_600
    payload = bytes(rng.integers(0, 256, size=40, dtype=np.uint8))
    frame = afsk.modulate(payload, profile=profile)

    lead = np.zeros(int(1.5 * afsk.SAMPLE_RATE), dtype=np.float32)
    tail = np.zeros(int(1.5 * afsk.SAMPLE_RATE), dtype=np.float32)
    buffer = np.concatenate([lead, frame, tail])
    frame_end = (len(lead) + len(frame)) / afsk.SAMPLE_RATE

    for at in (0.4, frame_end + 0.4):
        holed = punch_dropout(buffer, at)
        holed = holed + rng.normal(0, 0.01, size=len(holed))
        result = afsk.demodulate(holed, profile=profile)
        assert result.get("payload") == payload, \
            (f"a dropout at {at:.2f}s, clear of the frame, broke the decode",
             result.get("confidence"))
    print("test_a_dropout_outside_the_frame_is_harmless OK")


def test_a_dropout_inside_the_frame_fails_without_lying():
    """A hole in the middle of a frame is unrecoverable and that is fine --
    280ms is ~170 bits at 600 baud, and there is no FEC to rebuild them.
    What must not happen is a *false* decode: the CRC has to catch it, so
    the link retransmits rather than handing up corrupt data.

    The sync word still scores high, because it is 63 symbols at the very
    front and the hole is far behind it. That is why the bench saw
    'confidence 0.99, CRC failed' and read it as a sync problem.
    """
    rng = np.random.default_rng(17)
    profile = afsk.PROFILE_600
    payload = bytes(rng.integers(0, 256, size=40, dtype=np.uint8))
    frame = afsk.modulate(payload, profile=profile)
    lead = np.zeros(int(0.5 * afsk.SAMPLE_RATE), dtype=np.float32)
    buffer = np.concatenate([lead, frame, np.zeros(4000, dtype=np.float32)])

    frame_seconds = len(frame) / afsk.SAMPLE_RATE
    hit = 0
    for offset in np.arange(0.15, frame_seconds - 0.3, 0.1):
        holed = punch_dropout(buffer, 0.5 + offset)
        holed = holed + rng.normal(0, 0.01, size=len(holed))
        result = afsk.demodulate(holed, profile=profile)
        assert result.get("payload") in (None, payload), "decoder invented a payload"
        if result.get("payload") is None:
            hit += 1
    assert hit > 0, "expected at least one dropout position to break the frame"
    print(f"test_a_dropout_inside_the_frame_fails_without_lying OK "
          f"({hit} positions broke the frame, none decoded falsely)")


def test_high_confidence_survives_the_offset_that_kills_the_payload():
    """Why the ceiling looked mysterious rather than obvious.

    The sync word is 63 symbols; a frame is twenty times that. An offset
    that has barely moved the sampling point by the end of the sync word has
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

    # And the length byte still reads correctly, because it sits at bits
    # 63-71 where the accumulated error is still negligible. This is worth
    # pinning down: the sweep scripts reported a near-miss end_index ~1.2x
    # the expected frame length and it was read as a false sync lock on
    # garbage. It is not -- end_index is an absolute offset into the RX
    # buffer, and the frame simply started ~1s into it.
    assert "end_index" in result and "start_index" in result, result
    span = result["end_index"] - result["start_index"]
    sps = round(afsk.SAMPLE_RATE / profile.baud)
    assert abs(span - sps * _frame_bits(len(payload))) < sps, \
        ("length byte decoded wrong", span, sps * _frame_bits(len(payload)))
    print("test_high_confidence_survives_the_offset_that_kills_the_payload OK")


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
    body = link._encode_call_and_modes("STA1", "STA2", [0, 1], 1)
    a, b, supported, extra = link._decode_call_and_modes(body)
    assert (a, b, supported, extra) == ("STA1", "STA2", [0, 1], 1), (a, b, supported, extra)
    print("test_connect_body_roundtrip OK")


def test_connect_ack_body_roundtrip():
    body = link._encode_connect_ack("STA2", "STA1", [0, 1, 2], 1, 0)
    a, b, supported, accepted_id, own_id = link._decode_connect_ack(body)
    assert (a, b, supported, accepted_id, own_id) == ("STA2", "STA1", [0, 1, 2], 1, 0), \
        (a, b, supported, accepted_id, own_id)
    print("test_connect_ack_body_roundtrip OK")


def test_negotiate_mode():
    assert link._negotiate_mode([0, 1], 1) == 1
    assert link._negotiate_mode([0], 1) == afsk.CONTROL_PROFILE.mode_id
    assert link._negotiate_mode([0, 1], 0) == 0
    print("test_negotiate_mode OK")


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


class _FakeTransport:
    """In-memory stand-in for whale.transport.RadioTransport: send() writes
    straight into the paired transport's buffer instead of playing audio."""

    def __init__(self):
        self._buf = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self.peer = None
        # Optional f(audio) -> audio applied to one transmission, so a test
        # can lose a DATA frame or an ACK outright.
        self.corrupt = None
        self.keyings = 0

    def start_receiving(self):
        pass

    def stop_receiving(self):
        pass

    def is_transmitting(self):
        # send() below writes straight into the peer's buffer synchronously,
        # so there's no "mid-transmit" window for the decode loop to race.
        return False

    def snapshot_rx(self):
        with self._lock:
            return self._buf.copy()

    def consume_rx(self, upto_sample):
        with self._lock:
            self._buf = self._buf[upto_sample:]

    def send(self, tx_audio, **kwargs):
        self.keyings += 1
        if self.corrupt is not None:
            tx_audio = self.corrupt(tx_audio)
        with self.peer._lock:
            self.peer._buf = np.concatenate([self.peer._buf, tx_audio])
        return len(tx_audio) / afsk.SAMPLE_RATE


def _silence_once(transport):
    """Wipes the next transmission from this transport and then gets out of
    the way -- one frame or ACK lost to the channel."""
    def corrupt(audio):
        transport.corrupt = None
        return np.zeros(len(audio), dtype=np.float32)
    return corrupt


def _connected_pair():
    """Two Links, connected over paired fake transports, with the timing
    constants wound down -- the settling delays exist for real radios and
    only slow this down."""
    link.TX_TURNAROUND_DELAY = 0.02
    link.DECODE_POLL_INTERVAL = 0.01
    ta, tb = _FakeTransport(), _FakeTransport()
    ta.peer, tb.peer = tb, ta
    history = {}
    a = link.Link(ta, "STA1", mode_history_store=history)
    b = link.Link(tb, "STA2", mode_history_store=history)
    a.start()
    b.start()
    result = {}
    t = threading.Thread(target=lambda: result.update(peer=b.listen_once(timeout=20)))
    t.start()
    ok = a.connect("STA2", retries=3)
    t.join(timeout=20)
    assert ok, "connect() failed"
    assert result.get("peer") == "STA1", result
    return a, b, ta, tb


def _transfer(a, b, data, timeout=90):
    """a.send_message(data) with b receiving concurrently, as vara_server's
    pump does. Returns what b reassembled."""
    got = {}
    t = threading.Thread(target=lambda: got.update(msg=b.recv_message(timeout=timeout)))
    t.start()
    a.send_message(data)
    t.join(timeout=timeout)
    return got.get("msg")


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
    test_afsk_clean_loopback()
    test_afsk_noisy_delayed_loopback()
    test_clock_offset_simulation_is_faithful()
    test_decodes_through_a_small_clock_offset()
    test_clock_offset_tolerance_is_half_a_symbol_over_the_frame()
    test_high_confidence_survives_the_offset_that_kills_the_payload()
    test_decodes_at_production_size_under_bench_clock_offset()
    test_long_frames_lose_to_clock_offset_before_short_ones()
    test_a_dropout_outside_the_frame_is_harmless()
    test_a_dropout_inside_the_frame_fails_without_lying()
    test_link_packet_roundtrip()
    test_connect_body_roundtrip()
    test_connect_ack_body_roundtrip()
    test_negotiate_mode()
    test_mode_change_packet_roundtrip()
    test_demodulate_returns_the_earliest_frame_not_the_loudest()
    test_sync_confidence_does_not_depend_on_surrounding_silence()
    test_seq_ahead_wraps()
    test_await_turnaround_is_anchored_on_peer_audio()
    test_spare_ack_for_an_earlier_chunk_does_not_provoke_a_retransmit()
    test_ack_for_a_duplicate_still_advances_the_sender()
    test_unanswered_chunk_gives_up_after_max_retries()
    test_link_negotiation_and_mode_step()
    test_link_multi_chunk_message_roundtrip()
    test_link_recovers_from_lost_data_frame()
    test_link_survives_lost_ack_without_duplicating_data()
    print("all tests OK")
