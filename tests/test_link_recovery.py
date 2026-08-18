"""What happens to a session when a *control* frame is lost.

Run: python tests/test_link_recovery.py

ARQ already covers a lost PT_DATA or PT_DATA_ACK, and
tests/test_afsk_loopback.py covers both. The frames here are different in
kind: they are the ones that change what each end is doing, so losing one
leaves the two ends holding different beliefs -- and a retransmit does not
repair a disagreement, it just repeats it.

Two of those used to end the session outright, and neither could be
expressed by the suite as it stood: nothing could drop a chosen frame, so
nothing could reach the state. Every test below therefore starts by losing
one specific frame, using the same WHALE_DROP_PTYPE suppressor the bench
runs use (see link_harness.drop_next).

The lost-MODE_ACK cases are split by transition on purpose. Not all of them
were fatal, and the ones that were not are the ones that hid the bug:

    300 -> 600   survivable   the peer keeps transmitting at 300, which is
                              afsk.CONTROL_PROFILE and therefore always a
                              decode candidate
    600 -> 1200  FATAL        candidates were (CONTROL, 1200); the peer is
                              still at 600, which is neither
    1200 -> 600  FATAL        same, mirrored
    600 -> 300   FATAL        candidates collapse to (CONTROL,) alone

The survivable row is kept as a test in its own right so that a future
change cannot quietly convert it into a fatal one.
"""

import threading
import time

from whale import afsk, link, mode_history

import link_harness as harness

# Long enough for a real exchange over the fake channel, short enough that
# a test which is going to fail does so promptly rather than sitting out
# six ARQ retries at their production timeouts.
FAST_CONTROL_TIMEOUT = 1.5
FAST_DATA_TIMEOUT = 2.0


def _stop(*links):
    for one in links:
        one.stop()


# -- 1. a lost PT_MODE_ACK ---------------------------------------------


def _mode_step_survives_a_lost_ack(start_id, direction):
    """Bring the a->b leg up at `start_id`, have A request a step in
    `direction`, lose B's MODE_ACK, and require that a message still gets
    through and that the two ends agree about the profile afterwards.

    The agreement they converge on is deliberately not asserted to be the
    stepped-to profile. A never learned its request was accepted, so A is
    still transmitting at `start_id` and that is the truth of the matter;
    B's job is to notice and follow, not to insist. If A still wants the
    step it will ask again -- _maybe_adapt is unchanged and keeps its own
    counsel about that."""
    history = {}
    a, b, ta, tb = harness.make_pair(history=history)
    try:
        # connect() proposes whatever history says worked last with this
        # peer, so this is how a session is started at a chosen profile
        # without inventing a second mechanism for it.
        mode_history.record_good_mode(history, a.mycall, b.mycall, start_id)
        ok, peer = harness.handshake(a, b)
        assert ok and peer == a.mycall, (ok, peer)
        assert a.tx_profile.mode_id == start_id, a.tx_profile
        assert b.rx_profile.mode_id == start_id, b.rx_profile

        a.control_ack_timeout = FAST_CONTROL_TIMEOUT
        target = afsk.PROFILES[afsk.PROFILES.index(a.tx_profile) + direction]

        # B accepts the step and moves its rx_profile, but the ack never
        # reaches the air -- indistinguishable, from A, from one that was
        # sent and lost.
        harness.drop_next(b, "MODE_ACK")

        # Two chunks exactly, at whichever profile A is actually using: one
        # to prove the frame decoded at all, one to prove the link kept
        # going afterwards -- and not three, which would trip
        # STEP_UP_AFTER_CLEAN_STREAK and start a second mode step in the
        # middle of the assertions.
        payload = bytes((i * 7 + 11) % 256 for i in range(2 * a.tx_profile.chunk_size))
        got = {}
        receiver = threading.Thread(
            target=lambda: got.update(msg=b.recv_message(timeout=60)))
        receiver.start()
        try:
            a._request_mode_step(direction)
            assert a.tx_profile.mode_id == start_id, \
                "A must not move its tx_profile without an ack"
            assert b.rx_profile.mode_id == target.mode_id, \
                "B should have moved on sending the ack"
            a.send_message(payload)
        finally:
            receiver.join(timeout=60)

        assert got.get("msg") == payload, (len(got.get("msg") or b""), len(payload))
        assert b.rx_profile.mode_id == a.tx_profile.mode_id, \
            ("ends disagree about the profile after recovering",
             b.rx_profile.name, a.tx_profile.name)
        return target
    finally:
        _stop(a, b)


def test_lost_mode_ack_stepping_up_from_the_control_profile():
    """The survivable case: the peer stays at afsk.CONTROL_PROFILE, which
    every station always tries, so the session limped on even before any of
    this was handled. Pinned down so it stays that way."""
    target = _mode_step_survives_a_lost_ack(afsk.PROFILE_300.mode_id, +1)
    print(f"test_lost_mode_ack_stepping_up_from_the_control_profile OK (-> {target.name})")


def test_lost_mode_ack_stepping_up_between_data_profiles():
    """600 -> 1200. Fatal before the fix: the peer goes on transmitting at
    600, which is neither afsk.CONTROL_PROFILE nor the new rx_profile, so
    nothing it sends decodes at all and the disagreement can never be
    observed."""
    target = _mode_step_survives_a_lost_ack(afsk.PROFILE_600.mode_id, +1)
    print(f"test_lost_mode_ack_stepping_up_between_data_profiles OK (-> {target.name})")


def test_lost_mode_ack_stepping_down_between_data_profiles():
    """1200 -> 600, the mirror of the above and fatal for the same reason.
    Worse in practice: a step down happens because the link is already in
    trouble, which is exactly when the ack is most likely to be the frame
    that gets lost."""
    target = _mode_step_survives_a_lost_ack(afsk.PROFILE_1200.mode_id, -1)
    print(f"test_lost_mode_ack_stepping_down_between_data_profiles OK (-> {target.name})")


def test_lost_mode_ack_stepping_down_to_the_control_profile():
    """600 -> 300. The worst shape of it: the candidate list collapses to
    afsk.CONTROL_PROFILE alone, so there is not even a second profile being
    tried by accident."""
    target = _mode_step_survives_a_lost_ack(afsk.PROFILE_600.mode_id, -1)
    print(f"test_lost_mode_ack_stepping_down_to_the_control_profile OK (-> {target.name})")


def test_recovery_from_a_lost_mode_ack_costs_one_frame():
    """"Bounded number of frames" made specific: the very first data frame
    after the lost ack is the one that settles it, because a frame that
    decoded is not a belief about the peer, it is the peer."""
    history = {}
    a, b, ta, tb = harness.make_pair(history=history)
    try:
        mode_history.record_good_mode(history, a.mycall, b.mycall, afsk.PROFILE_600.mode_id)
        ok, _ = harness.handshake(a, b)
        assert ok
        a.control_ack_timeout = FAST_CONTROL_TIMEOUT
        harness.drop_next(b, "MODE_ACK")

        one_chunk = b"x" * a.tx_profile.chunk_size
        got = {}
        receiver = threading.Thread(target=lambda: got.update(msg=b.recv_message(timeout=60)))
        receiver.start()
        try:
            a._request_mode_step(+1)
            assert b.rx_profile.mode_id == afsk.PROFILE_1200.mode_id
            a.send_message(one_chunk)
        finally:
            receiver.join(timeout=60)

        assert got.get("msg") == one_chunk
        assert b.rx_profile.mode_id == afsk.PROFILE_600.mode_id, b.rx_profile
        assert b._rx_profile_fallback is None, "the fallback should be dropped once settled"
        print("test_recovery_from_a_lost_mode_ack_costs_one_frame OK")
    finally:
        _stop(a, b)


def test_a_control_frame_does_not_drag_the_rx_profile_back_down():
    """The trap in treating a decoded frame as evidence. Control-plane
    frames always ride afsk.CONTROL_PROFILE whatever was negotiated, so if
    they counted, every DISC or MODE_REQ would reset rx_profile to 300 and
    the link would never hold a faster one."""
    history = {}
    a, b, ta, tb = harness.make_pair(history=history)
    try:
        mode_history.record_good_mode(history, a.mycall, b.mycall, afsk.PROFILE_1200.mode_id)
        ok, _ = harness.handshake(a, b)
        assert ok and b.rx_profile.mode_id == afsk.PROFILE_1200.mode_id

        # A MODE_REQ from A, decoded by B at the control profile. B's rx
        # expectation for A's *data* must be untouched by it.
        b._handle_raw(bytes([link.PT_MODE_REQ, afsk.PROFILE_1200.mode_id]), afsk.CONTROL_PROFILE)
        assert b.rx_profile.mode_id == afsk.PROFILE_1200.mode_id, b.rx_profile
        print("test_a_control_frame_does_not_drag_the_rx_profile_back_down OK")
    finally:
        _stop(a, b)


# -- 2. a lost PT_CONNECT_ACK ------------------------------------------


def test_lost_connect_ack_still_brings_both_ends_up():
    """The listener answered, the answer was lost, and the caller retries.

    Before the handshake was idempotent this ended with the caller IDLE and
    the listener CONNECTED forever: listen_once had already returned, and
    nothing anywhere handled a PT_CONNECT afterwards -- _wait_packet
    discarded them."""
    a, b, ta, tb = harness.make_pair()
    try:
        a.control_ack_timeout = FAST_CONTROL_TIMEOUT
        harness.drop_next(b, "CONNECT_ACK")
        ok, peer = harness.handshake(a, b, service_b=True)
        assert ok, "caller never got connected"
        assert peer == a.mycall, peer
        assert a.state == "CONNECTED" and b.state == "CONNECTED", (a.state, b.state)
        assert a.peer_call == b.mycall and b.peer_call == a.mycall
        # The re-answer has to be the *same* ack, or the two ends walk away
        # with different profiles for the same session.
        assert a.tx_profile is b.rx_profile, (a.tx_profile.name, b.rx_profile.name)
        assert b.tx_profile is a.rx_profile, (b.tx_profile.name, a.rx_profile.name)
        print("test_lost_connect_ack_still_brings_both_ends_up OK")
    finally:
        _stop(a, b)


def test_a_caller_that_gives_up_does_not_leave_the_listener_connected():
    """Every CONNECT_ACK is lost, so idempotency cannot save it and the
    caller is right to give up. What must not happen is the listener
    staying CONNECTED on its own -- the acceptance criterion is that the
    two ends agree, not that they connect."""
    a, b, ta, tb = harness.make_pair()
    try:
        harness.drop_next(b, "CONNECT_ACK", occurrences=None)
        a.control_ack_timeout = FAST_CONTROL_TIMEOUT
        b.control_ack_timeout = FAST_CONTROL_TIMEOUT
        ok, peer = harness.handshake(a, b, retries=2, service_b=True)
        assert not ok, "connect() should have failed with every ack suppressed"
        assert a.state == "IDLE", a.state

        # b saw the caller's parting DISC while servicing the idle session.
        deadline = time.time() + 10
        while b.state == "CONNECTED" and time.time() < deadline:
            b.service_while_idle()
            time.sleep(0.02)
        assert b.state == "IDLE", "listener left half-open"
        print("test_a_caller_that_gives_up_does_not_leave_the_listener_connected OK")
    finally:
        _stop(a, b)


def test_a_connected_station_gives_up_on_a_peer_that_says_nothing():
    """The backstop, for the peer that neither reconnects nor disconnects
    -- switched off, out of range, or crashed. Nothing else in the link can
    notice that, because nothing else is waiting on anything.

    Checked at both places a CONNECTED station waits: service_while_idle,
    where it sits before its data connection exists, and recv_message,
    where it sits after."""
    saved = link.INACTIVITY_TIMEOUT
    for waiter in ("service_while_idle", "recv_message"):
        a, b, ta, tb = harness.make_pair()
        try:
            ok, _ = harness.handshake(a, b)
            assert ok and b.state == "CONNECTED"
            b.control_ack_timeout = FAST_CONTROL_TIMEOUT
            # Silence the peer for real: with its decode loop stopped, A
            # will not answer B's parting DISC either, which is the whole
            # point -- a peer that has gone away goes away completely.
            a.stop()

            link.INACTIVITY_TIMEOUT = 0.3
            try:
                time.sleep(0.5)
                if waiter == "service_while_idle":
                    assert b.service_while_idle() is False, "stale session not noticed"
                else:
                    assert b.recv_message(timeout=5) is None
                assert b.state == "IDLE", (waiter, b.state)
            finally:
                link.INACTIVITY_TIMEOUT = saved
        finally:
            _stop(a, b)
    print("test_a_connected_station_gives_up_on_a_peer_that_says_nothing OK")


def test_a_stale_connect_does_not_reset_a_live_session():
    """A duplicate CONNECT is answered; it is not obeyed.

    The distinction only exists because the caller's session id is on air.
    Without it, "retry of the call you already answered" and "I restarted,
    call you again" are the same bytes, and guessing the second resets the
    sequence state underneath a transfer with chunks in flight -- silently
    corrupting it, since both ends go on believing their own counters."""
    a, b, ta, tb = harness.make_pair()
    try:
        ok, _ = harness.handshake(a, b)
        assert ok and b.state == "CONNECTED"

        # Put the session mid-transfer.
        b._rx_expect_seq = 9
        b._partial_rx_buf = bytearray(b"half a message")
        keyings_before = tb.keyings

        # (a) the caller's own retry, same session id: re-answered, and
        #     nothing about the live session moves.
        own_supported = [p.mode_id for p in afsk.PROFILES]
        retry = link._encode_call_and_modes(a.mycall, b.mycall, own_supported,
                                            a.tx_profile.mode_id, b._session_id)
        b._rx_packets.put((link.PT_CONNECT, retry))
        assert b.service_while_idle() is True
        assert tb.keyings == keyings_before + 1, "duplicate CONNECT went unanswered"
        assert b._rx_expect_seq == 9, b._rx_expect_seq
        assert bytes(b._partial_rx_buf) == b"half a message"
        assert b.state == "CONNECTED"

        # (b) a different session id -- a genuinely new call. Not answered
        #     here at any price; the live session's state is worth more
        #     than a fast reconnect, and INACTIVITY_TIMEOUT will let the
        #     caller in soon enough.
        other = (b._session_id % 255) + 1
        fresh = link._encode_call_and_modes(a.mycall, b.mycall, own_supported,
                                            a.tx_profile.mode_id, other)
        b._rx_packets.put((link.PT_CONNECT, fresh))
        assert b.service_while_idle() is True
        assert tb.keyings == keyings_before + 1, "a new-session CONNECT was answered"
        assert b._rx_expect_seq == 9, b._rx_expect_seq
        assert bytes(b._partial_rx_buf) == b"half a message"
        print("test_a_stale_connect_does_not_reset_a_live_session OK")
    finally:
        _stop(a, b)


def test_a_connect_ack_for_an_earlier_session_is_ignored():
    """The caller's half of the same idea. A CONNECT_ACK left over from a
    previous call describes profiles negotiated for a session that no
    longer exists; taking it would bring the link up misconfigured."""
    # Wired to transmit nowhere and answered from a script, the way the ARQ
    # tests in test_afsk_loopback.py do it -- connect() drains its packet
    # queue on entry, so a stale ack cannot simply be queued up in advance.
    a = link.Link(harness.FakeTransport(), "STA1")
    a.control_ack_timeout = 0.2
    a._tx_packet = lambda ptype, body: None
    stale = link._encode_connect_ack("STA2", "STA1", [0, 1, 2],
                                     afsk.PROFILE_1200.mode_id,
                                     afsk.PROFILE_1200.mode_id, 0x22)
    replies = [(link.PT_CONNECT_ACK, stale)]
    a._wait_packet = lambda types, timeout: replies.pop(0) if replies else None

    saved = link._new_session_id
    link._new_session_id = lambda: 0x11
    try:
        assert a.connect("STA2", retries=1) is False, "a stale ack was taken as an answer"
        assert a.state == "IDLE", a.state
        assert a.tx_profile is afsk.CONTROL_PROFILE, a.tx_profile
    finally:
        link._new_session_id = saved
    print("test_a_connect_ack_for_an_earlier_session_is_ignored OK")


# -- 3. the suppression hook itself -------------------------------------


def test_drop_hook_parses_the_environment_the_bench_uses():
    """The software tests and the hardware runs share this parser, so it is
    worth one test of its own: a WHALE_DROP_PTYPE typo that would silently
    do nothing on the bench has to be visible here."""
    off = link._TxSuppressor.from_env({})
    assert off.should_drop(link.PT_MODE_ACK) is False

    first = link._TxSuppressor.from_env({"WHALE_DROP_PTYPE": "MODE_ACK"})
    assert first.should_drop(link.PT_MODE_ACK) is True
    assert first.should_drop(link.PT_MODE_ACK) is False   # default is "1"
    assert first.should_drop(link.PT_DATA) is False

    every = link._TxSuppressor.from_env(
        {"WHALE_DROP_PTYPE": "CONNECT_ACK,DATA_ACK", "WHALE_DROP_NTH": "all"})
    assert all(every.should_drop(link.PT_CONNECT_ACK) for _ in range(4))
    assert every.should_drop(link.PT_DATA_ACK) is True

    picked = link._TxSuppressor.from_env(
        {"WHALE_DROP_PTYPE": "0x08", "WHALE_DROP_NTH": "2,3"})
    assert [picked.should_drop(link.PT_MODE_ACK) for _ in range(4)] == \
        [False, True, True, False]

    try:
        link._TxSuppressor.from_env({"WHALE_DROP_PTYPE": "MODE_AKC"})
    except ValueError:
        pass
    else:
        raise AssertionError("a misspelt packet type must not be silently ignored")
    print("test_drop_hook_parses_the_environment_the_bench_uses OK")


def test_mode_step_script_parses_the_environment_the_bench_uses():
    assert link._mode_step_script({}) == {}
    assert link._mode_step_script({"WHALE_MODE_STEP_SCRIPT": "2:up,5:down"}) == {2: +1, 5: -1}
    assert link._forced_mode_id({}) is None
    assert link._forced_mode_id({"WHALE_FORCE_MODE": "2"}) == afsk.PROFILE_1200.mode_id
    assert link._forced_mode_id({"WHALE_FORCE_MODE": "99"}) is None
    # mode_id 0 is a real profile and is falsy, so "no override" has to be
    # None rather than anything the callers can confuse with zero.
    assert link._forced_mode_id({"WHALE_FORCE_MODE": "0"}) == afsk.PROFILE_300.mode_id
    print("test_mode_step_script_parses_the_environment_the_bench_uses OK")


if __name__ == "__main__":
    test_drop_hook_parses_the_environment_the_bench_uses()
    test_mode_step_script_parses_the_environment_the_bench_uses()
    test_lost_mode_ack_stepping_up_from_the_control_profile()
    test_lost_mode_ack_stepping_up_between_data_profiles()
    test_lost_mode_ack_stepping_down_between_data_profiles()
    test_lost_mode_ack_stepping_down_to_the_control_profile()
    test_recovery_from_a_lost_mode_ack_costs_one_frame()
    test_a_control_frame_does_not_drag_the_rx_profile_back_down()
    test_lost_connect_ack_still_brings_both_ends_up()
    test_a_caller_that_gives_up_does_not_leave_the_listener_connected()
    test_a_connected_station_gives_up_on_a_peer_that_says_nothing()
    test_a_stale_connect_does_not_reset_a_live_session()
    test_a_connect_ack_for_an_earlier_session_is_ignored()
    print("all link recovery tests OK")
