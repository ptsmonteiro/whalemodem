"""Half-duplex point-to-point data link: connect / send / receive / disconnect
over one radio, built as simple stop-and-wait ARQ on top of whale.afsk.

Not optimized: one frame in flight at a time, fixed small chunk size, fixed
timeouts. Correctness first.
"""

import logging
import queue
import threading
import time

from whale import afsk

logger = logging.getLogger(__name__)

PT_CONNECT = 0x01
PT_CONNECT_ACK = 0x02
PT_DISC = 0x03
PT_DISC_ACK = 0x04
PT_DATA = 0x05
PT_DATA_ACK = 0x06

CHUNK_SIZE = 40           # payload bytes per DATA frame -- kept small so a single
                          # real-hardware bit error (observed near the tail of
                          # longer frames) only costs a short retransmit instead
                          # of derailing a large chunk
EOF_BIT = 0x80             # top bit of the seq byte marks the last chunk of a message

FRAME_AIRTIME = afsk.frame_seconds(CHUNK_SIZE + 2)  # ~ worst case for a DATA frame
ACK_TIMEOUT = FRAME_AIRTIME + 3.0      # time to wait for a reply after tx
MAX_RETRIES = 6
DECODE_POLL_INTERVAL = 0.15
TX_TURNAROUND_DELAY = 1.0  # settling time before keying up, see _tx_packet


def _encode_call_pair(src, dst):
    return src.encode("ascii") + b"\x00" + dst.encode("ascii")


def _decode_call_pair(payload):
    src, _, dst = payload.partition(b"\x00")
    return src.decode("ascii", "replace"), dst.decode("ascii", "replace")


class LinkError(Exception):
    pass


class Link:
    """Owns one radio transport and one session's worth of protocol state.

    All of connect()/send()/disconnect() are blocking and meant to be called
    from a single worker thread per station (see vara_server.py) -- the
    protocol is stop-and-wait, so there is never more than one thing in
    flight and nothing here needs to be reentrant.
    """

    def __init__(self, transport, mycall, on_event=None):
        self.transport = transport
        self.mycall = mycall
        self.peer_call = None
        self.state = "IDLE"
        self.on_event = on_event or (lambda name, **kw: None)

        self._rx_packets = queue.Queue()
        self._partial_rx_buf = None  # in-progress recv_message() reassembly, see recv_message()
        self._partial_rx_last_seq = None
        self._stop = threading.Event()
        self._decode_thread = threading.Thread(target=self._decode_loop, daemon=True)

    def start(self):
        self.transport.start_receiving()
        self._decode_thread.start()

    def stop(self):
        self._stop.set()
        self.transport.stop_receiving()

    # -- decode loop (background) ---------------------------------------

    def _decode_loop(self):
        while not self._stop.is_set():
            snap = self.transport.snapshot_rx()
            if len(snap) > 0:
                result = afsk.demodulate(snap)
                if result.get("payload") is not None:
                    end = result.get("end_index", len(snap))
                    self.transport.consume_rx(end)
                    self._handle_raw(result["payload"])
                    continue  # try again immediately in case another frame follows
                elif "end_index" in result:
                    # Sync was found (confidence cleared the threshold) but
                    # the frame itself didn't check out (corrupt bits, e.g.
                    # a garbled self-echo of our own last TX). If we don't
                    # advance past it, this same strong-but-bad match stays
                    # the global correlation peak on every future poll --
                    # snapshot_rx() keeps accumulating, argmax keeps landing
                    # right back here, and a later, weaker, genuine frame
                    # elsewhere in the buffer never gets a look in. Consume
                    # up to its end estimate so the search moves on.
                    logger.debug("[%s] near-miss decode: confidence=%.1f len(snap)=%d",
                                 self.mycall, result.get("confidence", 0), len(snap))
                    self.transport.consume_rx(result["end_index"])
                    continue
            time.sleep(DECODE_POLL_INTERVAL)

    def _handle_raw(self, raw: bytes):
        if len(raw) < 1:
            return
        ptype, body = raw[0], raw[1:]
        if ptype in (PT_CONNECT, PT_CONNECT_ACK):
            # A station can pick up the tail of its own just-finished TX as
            # a decodable frame (RF self-reception on this rig -- the two
            # radios sit close together). CONNECT/CONNECT_ACK carry the
            # sender's callsign, so a frame whose "source" is us is
            # unambiguously our own echo, not something a peer sent -- drop
            # it before it can be mistaken for the peer's reply.
            src, _ = _decode_call_pair(body)
            if src == self.mycall:
                logger.debug("[%s] dropping self-echoed type=0x%02x", self.mycall, ptype)
                return
        logger.debug("[%s] rx packet type=0x%02x len=%d", self.mycall, ptype, len(body))
        self._rx_packets.put((ptype, body))

    def _tx_packet(self, ptype: int, body: bytes):
        # A reply sent essentially back-to-back with the frame it's replying
        # to (e.g. this station's decode loop hands off a CONNECT and
        # listen_once() keys up within milliseconds) reaches the peer
        # garbled or not at all on this rig -- the radio doesn't seem to be
        # fully settled from RX back to TX yet. A short fixed pause before
        # every transmission gives it that settling time.
        time.sleep(TX_TURNAROUND_DELAY)
        payload = bytes([ptype]) + body
        audio = afsk.modulate(payload)
        self.transport.send(audio)

    def _wait_packet(self, want_types, timeout):
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                ptype, body = self._rx_packets.get(timeout=remaining)
            except queue.Empty:
                return None
            if ptype in want_types:
                return ptype, body
            # Not what we're waiting for right now (e.g. a stray DISC from a
            # previous session) -- drop it and keep waiting.
            logger.debug("[%s] dropping unexpected packet type=0x%02x while waiting for %s", self.mycall,
                         ptype, want_types)

    def _drain_packets(self):
        while True:
            try:
                self._rx_packets.get_nowait()
            except queue.Empty:
                return

    # -- connection setup -------------------------------------------------

    def connect(self, dst_call, timeout_per_try=ACK_TIMEOUT, retries=MAX_RETRIES):
        self._drain_packets()
        self.state = "CONNECTING"
        body = _encode_call_pair(self.mycall, dst_call)
        for attempt in range(1, retries + 1):
            logger.info("[%s] CONNECT attempt %d/%d to %s", self.mycall, attempt, retries, dst_call)
            self.on_event("PTT", on=True)
            self._tx_packet(PT_CONNECT, body)
            self.on_event("PTT", off=True)
            got = self._wait_packet({PT_CONNECT_ACK}, timeout_per_try)
            if got is not None:
                _, ack_body = got
                src, dst = _decode_call_pair(ack_body)
                if dst == self.mycall:
                    self.peer_call = src
                    self.state = "CONNECTED"
                    self._partial_rx_buf = None
                    self._partial_rx_last_seq = None
                    self.on_event("CONNECTED", mycall=self.mycall, peer=self.peer_call)
                    return True
        self.state = "IDLE"
        self.on_event("CONNECT_FAILED")
        return False

    def listen_once(self, timeout=None):
        """Blocks until an incoming CONNECT addressed to us arrives, replies,
        and transitions to CONNECTED. Returns the peer callsign, or None on
        timeout."""
        self._drain_packets()
        self.state = "LISTENING"
        got = self._wait_packet({PT_CONNECT}, timeout or 1e9)
        if got is None:
            return None
        _, body = got
        src, dst = _decode_call_pair(body)
        if dst != self.mycall:
            return None
        self.peer_call = src
        ack_body = _encode_call_pair(self.mycall, src)
        self.on_event("PTT", on=True)
        self._tx_packet(PT_CONNECT_ACK, ack_body)
        self.on_event("PTT", off=True)
        self.state = "CONNECTED"
        self._partial_rx_buf = None
        self._partial_rx_last_seq = None
        self.on_event("CONNECTED", mycall=self.mycall, peer=self.peer_call)
        return self.peer_call

    # -- data transfer ------------------------------------------------------

    def send_message(self, data: bytes):
        """Sends `data` as one or more ARQ'd DATA frames. Blocks until every
        chunk is acknowledged or raises LinkError."""
        if self.state != "CONNECTED":
            raise LinkError("not connected")
        chunks = [data[i:i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)] or [b""]
        toggle = 0
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            seq = toggle | (EOF_BIT if is_last else 0)
            ok = self._send_chunk_with_arq(seq, chunk)
            if not ok:
                raise LinkError(f"no ACK for chunk {i+1}/{len(chunks)} after {MAX_RETRIES} tries")
            toggle ^= 1
        logger.info("send_message: %d bytes in %d chunk(s) acked", len(data), len(chunks))

    def _send_chunk_with_arq(self, seq, chunk):
        body = bytes([seq]) + chunk
        for attempt in range(1, MAX_RETRIES + 1):
            self.on_event("PTT", on=True)
            self._tx_packet(PT_DATA, body)
            self.on_event("PTT", off=True)
            got = self._wait_packet({PT_DATA_ACK, PT_DISC}, ACK_TIMEOUT)
            if got is None:
                logger.warning("DATA seq=0x%02x: no ACK, retry %d/%d", seq, attempt, MAX_RETRIES)
                continue
            ptype, body_in = got
            if ptype == PT_DISC:
                self._handle_peer_disc()
                raise LinkError("peer disconnected mid-transfer")
            if len(body_in) >= 1 and body_in[0] == seq:
                return True
            # ACK for a different seq (stale retransmit) -- keep waiting.
        return False

    def recv_message(self, timeout=None):
        """Blocks for the chunks of one message (as delimited by the EOF bit)
        and returns the reassembled bytes, or None on timeout / disconnect.

        Reassembly progress lives on self (_partial_rx_buf), not a local
        variable: a caller polling with a short timeout (vara_server.py's
        pump loop calls this with 0.5s so it can also check for outbound
        work) will see this return None most of the time simply because a
        real over-the-air frame takes several seconds -- if the chunks
        already ACKed while waiting were only held in a local buffer, each
        such timeout would silently drop them even though the sender
        correctly believes they were delivered. Persisting the buffer means
        a timeout just pauses reassembly; the next call picks up where it
        left off.
        """
        if self.state != "CONNECTED":
            raise LinkError("not connected")
        if self._partial_rx_buf is None:
            self._partial_rx_buf = bytearray()
        deadline = None if timeout is None else time.time() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.time())
            if deadline is not None and remaining <= 0:
                return None
            got = self._wait_packet({PT_DATA, PT_DISC}, remaining if remaining is not None else 1e9)
            if got is None:
                return None
            ptype, body = got
            if ptype == PT_DISC:
                self._handle_peer_disc()
                return None
            seq, chunk = body[0], body[1:]
            is_eof = bool(seq & EOF_BIT)
            # Ack every DATA we see, even a duplicate -- the sender is
            # waiting on it and may have missed our first ack.
            self.on_event("PTT", on=True)
            self._tx_packet(PT_DATA_ACK, bytes([seq]))
            self.on_event("PTT", off=True)
            if seq == self._partial_rx_last_seq:
                continue  # duplicate retransmit, already delivered
            self._partial_rx_last_seq = seq
            self._partial_rx_buf += chunk
            if is_eof:
                result = bytes(self._partial_rx_buf)
                self._partial_rx_buf = None
                self._partial_rx_last_seq = None
                return result

    # -- teardown ------------------------------------------------------

    def _handle_peer_disc(self):
        self.on_event("PTT", on=True)
        self._tx_packet(PT_DISC_ACK, b"")
        self.on_event("PTT", off=True)
        self.state = "IDLE"
        self.peer_call = None
        self.on_event("DISCONNECTED")

    def disconnect(self, timeout=ACK_TIMEOUT, retries=3):
        if self.state != "CONNECTED":
            self.state = "IDLE"
            return
        for attempt in range(1, retries + 1):
            self.on_event("PTT", on=True)
            self._tx_packet(PT_DISC, b"")
            self.on_event("PTT", off=True)
            got = self._wait_packet({PT_DISC_ACK, PT_DISC}, timeout)
            if got is not None:
                break
        self.state = "IDLE"
        self.peer_call = None
        self.on_event("DISCONNECTED")

    def poll_peer_disconnect(self):
        """Non-blocking: if the peer has sent DISC, tear down and return
        True. Used while idling in a connected state waiting on the user."""
        try:
            ptype, _ = self._rx_packets.get_nowait()
        except queue.Empty:
            return False
        if ptype == PT_DISC:
            self._handle_peer_disc()
            return True
        return False
