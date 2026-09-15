"""Pure-software checks for whale.link's protocol and ARQ behaviour.

Run: python tests/test_afsk_loopback.py

This file used to also carry the physical-layer unit tests for the 300/600
baud CPFSK modes (whale/afsk.py's Profile/modulate/demodulate). Those modes
and their machinery were removed from the product -- see
whale/modes/vf14.py's VF14_4, the current FM control mode -- and their tests
went with them. What remains here is protocol-level: packet formats and the
generic ARQ/session behaviour, which link_harness.py exercises over the real
default FM ladder (VF14_4 in control), not over a mock.
"""

import threading
import time

import numpy as np

from whale import link, mode_history, modes
from whale.modes.vf13 import VF13
from whale.modes.vf14 import VF14_4

# The two-Link-in-one-process harness these tests share with
# tests/test_link_recovery.py.
from link_harness import (FakeTransport as _FakeTransport, connected_pair as _connected_pair,
                          drop_next as _drop_next, silence_once as _silence_once,
                          transfer as _transfer)


def test_connect_body_roundtrip():
    body = link._encode_call_and_modes("STA1", "STA2", [0, 1], 1, 0x5A)
    a, b, supported, extra, session = link._decode_call_and_modes(body)
    assert (a, b, supported, extra, session) == ("STA1", "STA2", [0, 1], 1, 0x5A), \
        (a, b, supported, extra, session)
    print("test_connect_body_roundtrip OK")


def test_connect_ack_body_roundtrip():
    body = link._encode_connect_ack("STA2", "STA1", [0, 1, 2], 1, 0, 0x5A)
    decoded = link._decode_connect_ack(body)
    assert decoded == ("STA2", "STA1", [0, 1, 2], 1, 0, 0x5A), decoded


def test_negotiate_mode():
    assert link._negotiate_mode([0, 1], 1) == 1
    assert link._negotiate_mode([0], 1) == VF14_4.mode_id
    assert link._negotiate_mode([0, 1], 0) == 0
    print("test_negotiate_mode OK")


def test_data_ack_carries_received_mode():
    """The ACK identifies both the sequence result and DATA mode decoded."""
    header, remainder = link._encode_air_header(
        link.PT_DATA_ACK, VF14_4.mode_id,
        bytes([7, 8, VF13.mode_id]))
    assert len(remainder) == 1
    decoded = link._decode_air_header(header)
    assert decoded[-1] == bytes([7, 8])
    assert remainder == bytes([VF13.mode_id])
    print("test_data_ack_carries_received_mode OK")


def test_seq_ahead_wraps():
    assert link._seq_ahead(5, 3) == 2
    assert link._seq_ahead(3, 3) == 0
    assert link._seq_ahead(0, link.SEQ_MODULO - 1) == 1  # the wrap itself
    print("test_seq_ahead_wraps OK")


def test_await_turnaround_applies_the_channel_policy(monkeypatch):
    """FM now waits a fixed span after a decoded frame before replying: a
    zero-delay reply keyed within ~50ms of the peer's un-key lost 4 of 9
    control-mode DATA_ACKs on the VHF bench (see policy.FM's
    tx_turnaround_delay note). Read the delay from policy.FM rather than
    hard-coding it -- it has already moved once as bench data came in."""
    from whale.policy import FM

    a = link.Link(_FakeTransport(), "STA1")
    sleeps = []
    monkeypatch.setattr(link.time, "sleep", sleeps.append)
    monkeypatch.setattr(link.time, "monotonic", lambda: 100.0)

    a._peer_unkeyed_at = 99.9
    a._peer_unkeyed_observed_at = 99.9
    a._await_turnaround()
    assert abs(sleeps.pop() - (FM.tx_turnaround_delay - 0.1)) < 1e-9
    assert a._peer_unkeyed_at is None

    a._peer_unkeyed_at = None
    a._peer_unkeyed_observed_at = None
    a._await_turnaround()
    assert abs(sleeps.pop() - FM.tx_turnaround_delay) < 1e-9

    a._peer_unkeyed_at = 90.0
    a._peer_unkeyed_observed_at = 90.0
    a._await_turnaround()
    assert abs(sleeps.pop() - FM.tx_turnaround_delay) < 1e-9

    # A costly decoder may only establish an old audio-end anchor now. It is
    # fresh evidence that the full turnaround delay has already elapsed,
    # not a reason to sleep that delay again.
    a._peer_unkeyed_at = 90.0
    a._peer_unkeyed_observed_at = 100.0
    a._await_turnaround()
    assert not sleeps


def test_link_multi_chunk_message_roundtrip():
    """A whole message across several chunks, each acked before the next
    goes out. Sequence numbers are started near the top of the space so the
    transfer runs through a wrap.

    Sized past 3 chunks even at the default ladder's largest chunk_size
    (VF12's, currently 2900 bytes), so the wrap is forced regardless of
    which rung mid-session adaptation steps the transfer onto."""
    a, b, ta, tb = _connected_pair()
    try:
        a._tx_seq = link.SEQ_MODULO - 3
        b._rx_expect_seq = link.SEQ_MODULO - 3

        data = bytes((i * 7 + 11) % 256 for i in range(9000))
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
    mode = modes.default_registry().control.mode_id
    a, keyings = _arq_sender([bytes([0x06, 0x07, mode]),
                              bytes([0x07, 0x08, mode])])
    assert a._send_chunk_with_arq(0x07, b"aaaa", False) == 1
    assert len(keyings) == 1, f"{len(keyings)} keyings for one chunk -- retransmitted on a stale ACK"
    print("test_spare_ack_for_an_earlier_chunk_does_not_provoke_a_retransmit OK")


def test_ack_for_a_duplicate_still_advances_the_sender():
    """The other half of the same format: when the sender retransmits after
    a lost ACK, the peer's answer is about a frame it has already taken and
    moved past. That must still count as acked, or the transfer stalls."""
    a, keyings = _arq_sender([bytes([0x07, 0x08, modes.default_registry().control.mode_id])])
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


def test_floor_req_while_data_unacked_retransmits_without_waiting_out_the_timeout():
    """A DATA_ACK wait that sees PT_FLOOR_REQ instead breaks immediately and
    retransmits, rather than sitting out the full data_ack_timeout -- the
    bug seen on the bench: B ACKed A's final chunk, A missed the ACK, and B
    (with a reply already queued) sent FLOOR_REQ before A's long timeout
    ever fired. See Link._send_chunk_with_arq's PT_FLOOR_REQ branch."""
    a = link.Link(_FakeTransport(), "STA1")
    a.state = "CONNECTED"
    a.data_ack_timeout = 5.0  # old code would have to sit this whole thing out
    keyings = []
    a._tx_packet = lambda ptype, body: keyings.append(body)
    a._await_turnaround = lambda: None
    mode = modes.default_registry().control.mode_id
    queue_ = [(link.PT_FLOOR_REQ, b""),
             (link.PT_DATA_ACK, bytes([0x07, 0x08, mode]))]
    a._wait_packet = lambda types, timeout: queue_.pop(0) if queue_ else None

    start = time.monotonic()
    attempts = a._send_chunk_with_arq(0x07, b"aaaa", False)
    elapsed = time.monotonic() - start

    assert attempts == 2, f"expected one retransmit after the floor request, got {attempts}"
    assert len(keyings) == 2, f"{len(keyings)} keying(s) -- FLOOR_REQ should have provoked a retransmit"
    assert elapsed < 1.0, f"took {elapsed:.2f}s -- looks like it waited out data_ack_timeout"
    print("test_floor_req_while_data_unacked_retransmits_without_waiting_out_the_timeout OK")


def test_irs_does_not_request_the_floor_while_reassembly_is_in_progress():
    """The IRS half of the same rule: while _partial_rx_buf is non-empty the
    peer is still mid-message, so nothing asks for the floor -- it would key
    over the peer's next chunk. Once the message completes the buffer empties
    and the request goes out normally. See Link._acquire_floor."""
    a = link.Link(_FakeTransport(), "STA1")
    a.role = "IRS"
    a.control_ack_timeout = 1.0
    a._rx_expect_seq = 5
    a._partial_rx_buf = bytearray(b"half a message")

    tx_types = []
    a._tx_packet = lambda ptype, body: tx_types.append(ptype)
    a._await_turnaround = lambda: None

    # First wait answers with the peer's still-in-flight final (EOF) chunk,
    # which completes reassembly and empties _partial_rx_buf. Only then may
    # a FLOOR_REQ go out; the second wait grants it.
    queue_ = [(link.PT_DATA, bytes([5 | link.EOF_BIT]) + b"tail"),
             (link.PT_FLOOR_GRANT, b"")]
    a._wait_packet = lambda types, timeout: queue_.pop(0) if queue_ else None

    assert a._acquire_floor() is True
    assert tx_types == [link.PT_DATA_ACK, link.PT_FLOOR_REQ], \
        f"floor was requested before reassembly finished: {tx_types}"
    assert a.role == "ISS", a.role
    assert not a._partial_rx_buf
    print("test_irs_does_not_request_the_floor_while_reassembly_is_in_progress OK")


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


def test_floor_request_after_a_lost_final_ack_does_not_stall_either_link():
    """End-to-end reproduction of the bench bug: A (ISS) sends a multi-chunk
    message; B decodes and ACKs the final chunk but the ACK is lost. B's
    app has a reply ready the instant recv_message() returns and asks for
    the floor immediately -- while A is still waiting on that very ACK.
    data_ack_timeout is set far larger than the whole test's time budget, so
    if recovery only happened via A's timeout this test would time out
    rather than fail fast."""
    a, b, ta, tb = _connected_pair()
    try:
        a.data_ack_timeout = 60.0
        chunk_size = a.tx_profile.chunk_size
        data_ab = bytes((i * 19 + 4) % 256 for i in range(chunk_size * 2 + 50))
        data_ba = bytes((i * 23 + 6) % 256 for i in range(40))
        chunks = -(-len(data_ab) // chunk_size)  # ceil division
        assert chunks >= 3, f"test needs a genuinely multi-chunk message, got {chunks}"
        _drop_next(b, "DATA_ACK", occurrences=(chunks,))  # B's ack for the final chunk

        a_result, b_result = {}, {}

        def run_b():
            b_result["msg"] = b.recv_message(timeout=90)
            if b_result["msg"] is not None:
                b.send_message(data_ba)  # queued the instant the message is in

        def run_a():
            a.send_message(data_ab)
            a_result["msg"] = a.recv_message(timeout=90)

        thread_b = threading.Thread(target=run_b)
        thread_a = threading.Thread(target=run_a)
        start = time.monotonic()
        thread_b.start()
        thread_a.start()
        thread_a.join(timeout=90)
        thread_b.join(timeout=90)
        elapsed = time.monotonic() - start

        assert b_result.get("msg") == data_ab, "B never reassembled A's message intact"
        assert a_result.get("msg") == data_ba, "A never received B's reply intact"
        assert a.state == "CONNECTED" and b.state == "CONNECTED", (a.state, b.state)
        assert elapsed < 20.0, \
            f"took {elapsed:.1f}s -- looks like recovery waited out data_ack_timeout"
        print("test_floor_request_after_a_lost_final_ack_does_not_stall_either_link OK "
              f"({elapsed:.2f}s)")
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
    TX rate to B need not match B's TX rate to A (this rig's two directions
    can measure different SNR, which is what motivates this)."""
    link.TX_TURNAROUND_DELAY = 0.05  # keep the test fast; real hardware needs the settling time, this doesn't

    ta, tb = _FakeTransport(), _FakeTransport()
    ta.peer, tb.peer = tb, ta
    history = {}
    a = link.Link(ta, "STA1", mode_history_store=history)
    b = link.Link(tb, "STA2", mode_history_store=history)
    a.start()
    b.start()
    try:
        # History says STA1->STA2 last spoke at VF13 (one rung above the
        # control mode), but STA2->STA1 has no history at all -- connect
        # should bring up an asymmetric link: A's tx (and B's rx) at VF13,
        # B's tx (and A's rx) still at the control mode since B has nothing
        # to go on yet.
        mode_history.record_good_mode(history, "STA1", "STA2", VF13.mode_id)

        listen_result = {}

        def do_listen():
            listen_result["peer"] = b.listen_once(timeout=20)

        t = threading.Thread(target=do_listen)
        t.start()
        ok = a.connect("STA2", retries=3)
        t.join(timeout=20)

        assert ok, "connect() failed"
        assert listen_result["peer"] == "STA1", listen_result
        assert a.tx_profile.mode_id == VF13.mode_id, a.tx_profile
        assert a.rx_profile is a.modes.control, a.rx_profile
        assert b.rx_profile.mode_id == VF13.mode_id, b.rx_profile
        assert b.tx_profile is b.modes.control, b.tx_profile
        # The whole default ladder: what each end advertises is its
        # registry, and stations run whale.modes.default_registry().
        expected = set(modes.default_registry().supported_ids)
        assert a.peer_supported_modes == expected
        assert b.peer_supported_modes == expected

        # A changes its own transmit mode locally. B discovers it from the
        # next DATA and confirms that mode in DATA_ACK.
        def do_recv():
            b.recv_message(timeout=20)

        t = threading.Thread(target=do_recv)
        t.start()
        a._step_tx_mode(-1)
        # A downward mode change starts the adaptation cooldown, so the ACK
        # below cannot immediately probe the old mode again.
        a.send_message(b"mode confirmation")
        t.join(timeout=20)

        assert a.tx_profile.mode_id == VF14_4.mode_id, a.tx_profile
        assert b.rx_profile.mode_id == VF14_4.mode_id, b.rx_profile
        assert b.tx_profile is b.modes.control, b.tx_profile
        print("test_link_negotiation_and_mode_step OK")
    finally:
        a.stop()
        b.stop()


if __name__ == "__main__":
    test_connect_body_roundtrip()
    test_connect_ack_body_roundtrip()
    test_negotiate_mode()
    test_data_ack_carries_received_mode()
    test_seq_ahead_wraps()
    test_floor_req_while_data_unacked_retransmits_without_waiting_out_the_timeout()
    test_irs_does_not_request_the_floor_while_reassembly_is_in_progress()
    test_roles_assigned_at_connect()
    test_irs_can_request_and_use_the_floor()
    test_floor_request_after_a_lost_final_ack_does_not_stall_either_link()
    test_concurrent_send_attempts_do_not_collide()
    test_spare_ack_for_an_earlier_chunk_does_not_provoke_a_retransmit()
    test_ack_for_a_duplicate_still_advances_the_sender()
    test_unanswered_chunk_gives_up_after_max_retries()
    test_link_negotiation_and_mode_step()
    test_link_multi_chunk_message_roundtrip()
    test_link_recovers_from_lost_data_frame()
    test_link_survives_lost_ack_without_duplicating_data()
    print("all tests OK")
