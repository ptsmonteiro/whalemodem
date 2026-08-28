"""Two whale.link.Link instances driven against each other in one process,
over fake transports that hand audio straight to one another.

Not a mock of the protocol: the real afsk.modulate/demodulate run on every
frame, so the decode-candidate logic, the sync search and the CRC are all
genuinely exercised. What the fake replaces is only the radio -- no sound
card, no PTT, no real time spent playing audio -- which is what makes a
whole ARQ exchange take a second or two instead of a minute or two.

That distinction matters for mode recovery: the IRS must genuinely search
all negotiated waveforms and decode the ISS after an unannounced speed
change. A harness that passed packets around as tuples could not prove it.

Frame loss is injected two ways, at two levels:

  - FakeTransport.corrupt replaces one transmission's audio. This is the
    channel losing a frame, and it is what the older ARQ tests use.
  - drop_next() sets the Link's own WHALE_DROP_PTYPE suppressor, so a
    chosen packet *type* never reaches the transport. This is the same
    hook the hardware runs use, driven through the same parser -- see the
    "test affordances" note in whale/link.py for why loss on the bench has
    to be reproduced by suppressing the transmission rather than by
    corrupting the channel.
"""

import threading
import time

import numpy as np

from whale import afsk, link


class FakeTransport:
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


def silence_once(transport):
    """Wipes the next transmission from this transport and then gets out of
    the way -- one frame or ACK lost to the channel."""
    def corrupt(audio):
        transport.corrupt = None
        return np.zeros(len(audio), dtype=np.float32)
    return corrupt


def drop_next(a_link, *ptype_names, occurrences=(1,)):
    """Suppresses transmissions of the named packet types on `a_link`.

    Goes through _TxSuppressor.from_env with a synthetic environment rather
    than setting the fields directly, so the tests and the bench runs share
    not just the mechanism but the spelling of it: a typo in
    WHALE_DROP_PTYPE that a hardware run would silently ignore fails here.

    `occurrences=None` suppresses every occurrence, as WHALE_DROP_NTH=all
    does."""
    env = {"WHALE_DROP_PTYPE": ",".join(ptype_names),
           "WHALE_DROP_NTH": "all" if occurrences is None
           else ",".join(str(n) for n in occurrences)}
    a_link.tx_suppress = link._TxSuppressor.from_env(env)


def speed_up():
    """Winds the settling delays down. They exist for real radios swapping
    T/R; nothing in this harness needs them, and they otherwise dominate
    the runtime of every test here."""
    link.TX_TURNAROUND_DELAY = 0.02
    link.DECODE_POLL_INTERVAL = 0.01


def make_pair(history=None, a_call="STA1", b_call="STA2"):
    """Two started-but-unconnected Links over paired fake transports, so a
    test can set profiles, history or suppression before the handshake.
    Returns (a, b, transport_a, transport_b)."""
    speed_up()
    ta, tb = FakeTransport(), FakeTransport()
    ta.peer, tb.peer = tb, ta
    history = {} if history is None else history
    a = link.Link(ta, a_call, mode_history_store=history)
    b = link.Link(tb, b_call, mode_history_store=history)
    a.start()
    b.start()
    return a, b, ta, tb


def handshake(a, b, retries=3, timeout=30, service_b=False):
    """a.connect(b.mycall) with b listening concurrently, the way two
    stations actually meet. Returns (connect_returned, peer_b_saw).

    With service_b, b goes on calling service_while_idle() after
    listen_once() returns, which is what vara_server does while it waits
    for its local client to open the data port. That window is where a lost
    CONNECT_ACK used to strand the listener, so a test of it has to model
    the window rather than let b fall silent the moment it thinks it is
    connected."""
    seen = {}
    done = threading.Event()

    def listen():
        seen["peer"] = b.listen_once(timeout=timeout)
        while service_b and not done.is_set():
            if not b.service_while_idle():
                return
            time.sleep(0.02)

    t = threading.Thread(target=listen)
    t.start()
    try:
        ok = a.connect(b.mycall, retries=retries)
    finally:
        done.set()
        t.join(timeout=timeout)
    return ok, seen.get("peer")


def connected_pair(history=None):
    """Two Links, connected. Asserts the handshake worked."""
    a, b, ta, tb = make_pair(history=history)
    ok, peer = handshake(a, b)
    assert ok, "connect() failed"
    assert peer == "STA1", peer
    return a, b, ta, tb


def transfer(a, b, data, timeout=90):
    """a.send_message(data) with b receiving concurrently, as vara_server's
    pump does. Returns what b reassembled."""
    got = {}
    t = threading.Thread(target=lambda: got.update(msg=b.recv_message(timeout=timeout)))
    t.start()
    a.send_message(data)
    t.join(timeout=timeout)
    return got.get("msg")
