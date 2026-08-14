"""Pure-software checks for framing + afsk -- no hardware, no radios.

Run: python tests/test_afsk_loopback.py
"""

import sys
import threading
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


class _FakeTransport:
    """In-memory stand-in for whale.transport.RadioTransport: send() writes
    straight into the paired transport's buffer instead of playing audio."""

    def __init__(self):
        self._buf = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self.peer = None

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

    def send(self, tx_audio):
        with self.peer._lock:
            self.peer._buf = np.concatenate([self.peer._buf, tx_audio])


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
    test_link_packet_roundtrip()
    test_connect_body_roundtrip()
    test_connect_ack_body_roundtrip()
    test_negotiate_mode()
    test_mode_change_packet_roundtrip()
    test_link_negotiation_and_mode_step()
    print("all tests OK")
