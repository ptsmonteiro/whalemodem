"""Half-duplex point-to-point data link: connect / send / receive / disconnect
over one radio, built as simple stop-and-wait ARQ on top of whale.afsk.

Not optimized: one frame in flight at a time, fixed small chunk size, fixed
timeouts. Correctness first.
"""

import logging
import queue
import threading
import time

from whale import afsk, mode_history

logger = logging.getLogger(__name__)

PT_CONNECT = 0x01
PT_CONNECT_ACK = 0x02
PT_DISC = 0x03
PT_DISC_ACK = 0x04
PT_DATA = 0x05
PT_DATA_ACK = 0x06
PT_MODE_REQ = 0x07         # body: [proposed_mode_id] -- mid-session speed step, see _request_mode_step
PT_MODE_ACK = 0x08         # body: [accepted_mode_id] (may differ from proposed if rejected)

# Packet types whose bodies are small and must survive even when the
# negotiated data profile is struggling -- these always go out on
# afsk.CONTROL_PROFILE (see _tx_packet), never on self.tx_profile. Only bulk
# data (PT_DATA/PT_DATA_ACK) ever rides the negotiated speed.
_CONTROL_PLANE_TYPES = {PT_CONNECT, PT_CONNECT_ACK, PT_DISC, PT_DISC_ACK, PT_MODE_REQ, PT_MODE_ACK}

EOF_BIT = 0x80             # top bit of the seq byte marks the last chunk of a message

# CHUNK_SIZE (payload bytes per DATA frame -- kept small so a single
# real-hardware bit error, observed near the tail of longer frames, only
# costs a short retransmit instead of derailing a large chunk) and the
# frame-airtime-derived ACK timeout both depend on the active afsk.Profile
# (baud, in particular) -- see Link._apply_tx_profile/_apply_rx_profile,
# which compute them per instance instead of as module constants.
MAX_RETRIES = 6
DECODE_POLL_INTERVAL = 0.15
TX_TURNAROUND_DELAY = 1.0  # settling time before keying up, see _tx_packet

# Mid-session speed adaptation thresholds -- deliberately just ARQ-outcome
# based (no SNR estimate, no throughput math). React fast to trouble, be
# conservative about speeding up.
STEP_DOWN_AFTER_ATTEMPTS = 3   # a chunk needing this many tries triggers an immediate step down
STEP_UP_AFTER_CLEAN_STREAK = 3  # this many first-try chunks in a row triggers a step up

# Rough control-frame payload size used to size the control-plane ACK
# timeout (callsigns + mode list all comfortably fit) -- not a hard limit.
_CONTROL_FRAME_LEN_ESTIMATE = 32

_PTYPE_NAMES = {
    PT_CONNECT: "CONNECT", PT_CONNECT_ACK: "CONNECT_ACK",
    PT_DISC: "DISC", PT_DISC_ACK: "DISC_ACK",
    PT_DATA: "DATA", PT_DATA_ACK: "DATA_ACK",
    PT_MODE_REQ: "MODE_REQ", PT_MODE_ACK: "MODE_ACK",
}


def _ptype_name(ptype):
    return _PTYPE_NAMES.get(ptype, f"0x{ptype:02x}")


def _encode_call_pair(src, dst):
    return src.encode("ascii") + b"\x00" + dst.encode("ascii")


def _decode_call_pair(payload):
    src, _, dst = payload.partition(b"\x00")
    return src.decode("ascii", "replace"), dst.decode("ascii", "replace")


def _encode_call_and_modes(a, b, supported_ids, extra_id):
    """CONNECT body: "a\\x00b\\x00" + one byte per supported mode_id + one
    trailing byte (the sender's proposed TX mode_id for the a->b direction)."""
    return (a.encode("ascii") + b"\x00" + b.encode("ascii") + b"\x00" +
            bytes(sorted(supported_ids)) + bytes([extra_id]))


def _decode_call_and_modes(payload):
    a, _, rest = payload.partition(b"\x00")
    b, _, mode_section = rest.partition(b"\x00")
    a = a.decode("ascii", "replace")
    b = b.decode("ascii", "replace")
    if not mode_section:
        return a, b, [], afsk.CONTROL_PROFILE.mode_id
    return a, b, list(mode_section[:-1]), mode_section[-1]


def _encode_connect_ack(a, b, supported_ids, accepted_tx_id, own_tx_id):
    """CONNECT_ACK body: same shape as CONNECT's, but with *two* trailing
    bytes instead of one -- the two directions of the link are negotiated
    independently (one station's TX quality to its peer is not the same as
    the reverse leg, see whale/afsk.py's measured per-direction SNR), so the
    listener must report back both: the mode_id it's accepting for the
    caller's proposed (a->b) direction, and the mode_id it has separately
    chosen for its own (b->a) transmissions."""
    return (a.encode("ascii") + b"\x00" + b.encode("ascii") + b"\x00" +
            bytes(sorted(supported_ids)) + bytes([accepted_tx_id, own_tx_id]))


def _decode_connect_ack(payload):
    a, _, rest = payload.partition(b"\x00")
    b, _, mode_section = rest.partition(b"\x00")
    a = a.decode("ascii", "replace")
    b = b.decode("ascii", "replace")
    if len(mode_section) < 2:
        return a, b, [], afsk.CONTROL_PROFILE.mode_id, afsk.CONTROL_PROFILE.mode_id
    return a, b, list(mode_section[:-2]), mode_section[-2], mode_section[-1]


def _negotiate_mode(own_supported_ids, proposed_id):
    """Listener's rule for picking a starting data profile: accept the
    caller's proposal if we can decode it, else fall back to the always-
    supported control profile."""
    if proposed_id in own_supported_ids:
        return proposed_id
    return afsk.CONTROL_PROFILE.mode_id


class LinkError(Exception):
    pass


class Link:
    """Owns one radio transport and one session's worth of protocol state.

    All of connect()/send()/disconnect() are blocking and meant to be called
    from a single worker thread per station (see vara_server.py) -- the
    protocol is stop-and-wait, so there is never more than one thing in
    flight and nothing here needs to be reentrant.
    """

    def __init__(self, transport, mycall, on_event=None, mode_history_store=None):
        self.transport = transport
        self.mycall = mycall
        self.peer_call = None
        self.peer_supported_modes = set()
        self.state = "IDLE"
        self.on_event = on_event or (lambda name, **kw: None)
        self.mode_history = {} if mode_history_store is None else mode_history_store
        self._clean_streak = 0

        # Control-plane frames always use afsk.CONTROL_PROFILE (see
        # _tx_packet), so this timeout is fixed for the life of the Link.
        self.control_ack_timeout = afsk.frame_seconds(_CONTROL_FRAME_LEN_ESTIMATE, afsk.CONTROL_PROFILE) + 3.0

        # self.tx_profile / self.rx_profile are the *negotiated data*
        # profiles for each direction -- only meaningful once CONNECTED.
        # They're independent: this station's TX quality to its peer and
        # the reverse leg can and do differ on real hardware (see
        # whale/afsk.py's measured per-direction SNR numbers), so each side
        # is negotiated and adapted separately instead of sharing one
        # profile. Both start at CONTROL_PROFILE as a harmless default.
        self.tx_profile = afsk.CONTROL_PROFILE
        self.rx_profile = afsk.CONTROL_PROFILE
        self._recompute_data_ack_timeout()

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

    # -- profile management -----------------------------------------------

    def _apply_tx_profile(self, profile):
        """Sets the profile this station uses to transmit (DATA when it's
        the sender, DATA_ACK when it's replying -- both reflect the same
        outbound RF path to the peer). Control-plane frames are unaffected
        -- they always use afsk.CONTROL_PROFILE regardless of this."""
        self.tx_profile = profile
        self._recompute_data_ack_timeout()

    def _apply_rx_profile(self, profile):
        """Sets the profile this station expects the *peer's* transmissions
        at -- i.e. the peer's own tx_profile, as far as this station knows
        it. Used by the decode loop and (indirectly) by the ACK timeout."""
        self.rx_profile = profile
        self._recompute_data_ack_timeout()

    def _recompute_data_ack_timeout(self):
        # Worst-case round trip for one DATA/DATA_ACK exchange: our DATA
        # frame out at tx_profile, then the peer's (tiny) ACK back at
        # rx_profile -- the two legs can run at different baud, so both
        # have to be accounted for separately rather than doubling one.
        tx_airtime = afsk.frame_seconds(self.tx_profile.chunk_size + 2, self.tx_profile)
        ack_airtime = afsk.frame_seconds(3, self.rx_profile)
        self.data_ack_timeout = tx_airtime + ack_airtime + 3.0

    def _candidate_decode_profiles(self):
        """Which afsk.Profile(s) an incoming frame might be using right
        now: control-plane traffic always uses CONTROL_PROFILE, and DATA
        traffic uses whatever self.rx_profile currently is (the peer's own
        tx_profile) -- try both since the decode loop can't otherwise tell
        which is arriving next."""
        if self.rx_profile is afsk.CONTROL_PROFILE:
            return (afsk.CONTROL_PROFILE,)
        return (afsk.CONTROL_PROFILE, self.rx_profile)

    # -- decode loop (background) ---------------------------------------

    def _decode_loop(self):
        while not self._stop.is_set():
            if self.transport.is_transmitting():
                # Don't touch the RX buffer mid-TX: transport.send() clears
                # it before and after keying up specifically so our own
                # leaked audio is never handed to the decoder, but this loop
                # polls independently on its own timer, so without this check
                # it can grab a snapshot *during* the TX and decode our own
                # frame before send()'s post-TX clear ever runs.
                time.sleep(DECODE_POLL_INTERVAL)
                continue
            snap = self.transport.snapshot_rx()
            if len(snap) > 0:
                if self._decode_one(snap):
                    continue  # try again immediately in case another frame follows
            time.sleep(DECODE_POLL_INTERVAL)

    def _decode_one(self, snap) -> bool:
        """Tries every candidate profile against `snap`; handles/consumes
        the first usable result. Returns True if it made progress (decoded
        a frame or skipped a near-miss) so the caller should retry the
        buffer immediately instead of sleeping.

        With two candidate profiles (control + a faster negotiated data
        profile), a lower-baud profile's correlator can pick up a spurious
        sync lock on audio that's actually a still-arriving higher-baud
        frame -- their tones can overlap enough for that -- and, once it
        reads far enough to hit a garbage length byte, report a near-miss
        end_index of its own. Consuming on that would truncate the real
        frame before the other candidate ever gets the full thing to look
        at. So: if any candidate has a genuine sync lock (confidence over
        its own threshold) but hasn't seen enough samples yet for a verdict,
        this poll holds off consuming anything and just waits for more
        audio, rather than letting a different candidate's near-miss win."""
        near_miss = None  # (end_index, confidence) of the best non-decoding sync, if any
        still_arriving = False
        for profile in self._candidate_decode_profiles():
            result = afsk.demodulate(snap, profile=profile)
            if result.get("payload") is not None:
                end = result.get("end_index", len(snap))
                self.transport.consume_rx(end)
                logger.info("[%s] decoded frame at profile %s (confidence=%.1f)",
                            self.mycall, profile.name, result.get("confidence", 0.0))
                self._handle_raw(result["payload"], profile)
                return True
            if "end_index" not in result and result.get("confidence", 0) >= profile.confidence_threshold:
                still_arriving = True
            if "end_index" in result:
                # Sync was found (confidence cleared the threshold) but the
                # frame itself didn't check out -- most often a genuine
                # frame at the *other* candidate profile, or a garbled
                # self-echo of our own last TX. If we don't advance past
                # it, this same strong-but-bad match stays the correlation
                # peak on every future poll and a later, weaker, genuine
                # frame elsewhere in the buffer never gets a look in.
                if near_miss is None or result["end_index"] > near_miss[0]:
                    near_miss = (result["end_index"], result.get("confidence", 0))
        if near_miss is not None and not still_arriving:
            logger.info("[%s] near-miss decode: sync found but frame invalid (confidence=%.1f len(snap)=%d)",
                        self.mycall, near_miss[1], len(snap))
            self.transport.consume_rx(near_miss[0])
            return True
        return False

    def _handle_raw(self, raw: bytes, profile):
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
                logger.info("[%s] dropping self-echoed %s", self.mycall, _ptype_name(ptype))
                return
        logger.info("[%s] RX %s at %s (%d body byte(s))", self.mycall, _ptype_name(ptype), profile.name, len(body))
        self._rx_packets.put((ptype, body))

    def _tx_packet(self, ptype: int, body: bytes):
        # A reply sent essentially back-to-back with the frame it's replying
        # to (e.g. this station's decode loop hands off a CONNECT and
        # listen_once() keys up within milliseconds) reaches the peer
        # garbled or not at all on this rig -- the radio doesn't seem to be
        # fully settled from RX back to TX yet. A short fixed pause before
        # every transmission gives it that settling time.
        time.sleep(TX_TURNAROUND_DELAY)
        profile = afsk.CONTROL_PROFILE if ptype in _CONTROL_PLANE_TYPES else self.tx_profile
        payload = bytes([ptype]) + body
        audio = afsk.modulate(payload, profile=profile)
        keyed = self.transport.send(audio)
        # Both numbers, because the gap between them is the PTT/settling
        # overhead this frame actually paid -- the thing to watch if air
        # time regresses. See scripts/sweep_ptt_timing.py.
        logger.info("[%s] TX %s at %s (%d body byte(s), %.2fs audio, %.2fs keyed)",
                    self.mycall, _ptype_name(ptype), profile.name, len(body),
                    len(audio) / afsk.SAMPLE_RATE, keyed)

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
            logger.info("[%s] dropping unexpected %s while waiting for %s", self.mycall,
                        _ptype_name(ptype), {_ptype_name(t) for t in want_types})

    def _drain_packets(self):
        while True:
            try:
                self._rx_packets.get_nowait()
            except queue.Empty:
                return

    # -- connection setup -------------------------------------------------

    def connect(self, dst_call, timeout_per_try=None, retries=MAX_RETRIES):
        timeout_per_try = self.control_ack_timeout if timeout_per_try is None else timeout_per_try
        self._drain_packets()
        self.state = "CONNECTING"
        own_supported = [p.mode_id for p in afsk.PROFILES]
        proposed_id = mode_history.last_good_mode(self.mode_history, self.mycall, dst_call)
        if proposed_id is None or proposed_id not in own_supported:
            proposed_id = afsk.CONTROL_PROFILE.mode_id  # no history with this peer -- start slow
        body = _encode_call_and_modes(self.mycall, dst_call, own_supported, proposed_id)
        for attempt in range(1, retries + 1):
            logger.info("[%s] CONNECT attempt %d/%d to %s (proposing mode %d)",
                        self.mycall, attempt, retries, dst_call, proposed_id)
            self.on_event("PTT", on=True)
            self._tx_packet(PT_CONNECT, body)
            self.on_event("PTT", off=True)
            got = self._wait_packet({PT_CONNECT_ACK}, timeout_per_try)
            if got is not None:
                _, ack_body = got
                src, dst, peer_supported, accepted_id, peer_tx_id = _decode_connect_ack(ack_body)
                if dst == self.mycall:
                    self.peer_call = src
                    self.peer_supported_modes = set(peer_supported)
                    # accepted_id: what the listener accepted of our proposal --
                    # that's our TX rate for this (mycall->peer) direction.
                    # peer_tx_id: the listener's own, independently chosen TX
                    # rate for the reverse (peer->mycall) direction -- that's
                    # what we should expect its frames at.
                    self._apply_tx_profile(afsk.PROFILES_BY_ID.get(accepted_id, afsk.CONTROL_PROFILE))
                    self._apply_rx_profile(afsk.PROFILES_BY_ID.get(peer_tx_id, afsk.CONTROL_PROFILE))
                    self._clean_streak = 0
                    self.state = "CONNECTED"
                    self._partial_rx_buf = None
                    self._partial_rx_last_seq = None
                    self.on_event("CONNECTED", mycall=self.mycall, peer=self.peer_call)
                    logger.info("[%s] connected to %s: tx=%s rx=%s", self.mycall, self.peer_call,
                                self.tx_profile.name, self.rx_profile.name)
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
        src, dst, peer_supported, proposed_id = _decode_call_and_modes(body)
        if dst != self.mycall:
            return None
        self.peer_call = src
        self.peer_supported_modes = set(peer_supported)
        own_supported = [p.mode_id for p in afsk.PROFILES]
        # negotiated_id: whether we accept the caller's proposed rate for
        # its (src->mycall) direction -- becomes our rx expectation.
        negotiated_id = _negotiate_mode(own_supported, proposed_id)
        # own_tx_id: independently, what rate *we* should use transmitting
        # back (mycall->src) -- our own history for this peer, downgraded
        # to CONTROL_PROFILE if the caller hasn't told us it supports that
        # mode. The two legs need not match: this rig's two directions
        # measure different SNR (see whale/afsk.py's module docstring).
        own_tx_id = mode_history.last_good_mode(self.mode_history, self.mycall, src)
        if (own_tx_id is None or own_tx_id not in own_supported
                or own_tx_id not in peer_supported):
            own_tx_id = afsk.CONTROL_PROFILE.mode_id
        ack_body = _encode_connect_ack(self.mycall, src, own_supported, negotiated_id, own_tx_id)
        self.on_event("PTT", on=True)
        self._tx_packet(PT_CONNECT_ACK, ack_body)
        self.on_event("PTT", off=True)
        self._apply_rx_profile(afsk.PROFILES_BY_ID.get(negotiated_id, afsk.CONTROL_PROFILE))
        self._apply_tx_profile(afsk.PROFILES_BY_ID.get(own_tx_id, afsk.CONTROL_PROFILE))
        self._clean_streak = 0
        self.state = "CONNECTED"
        self._partial_rx_buf = None
        self._partial_rx_last_seq = None
        self.on_event("CONNECTED", mycall=self.mycall, peer=self.peer_call)
        logger.info("[%s] accepted connection from %s: tx=%s rx=%s", self.mycall, src,
                    self.tx_profile.name, self.rx_profile.name)
        return self.peer_call

    # -- data transfer ------------------------------------------------------

    def send_message(self, data: bytes):
        """Sends `data` as one or more ARQ'd DATA frames. Blocks until every
        chunk is acknowledged or raises LinkError.

        Chunks are cut one at a time, immediately before each is sent, rather
        than pre-split up front: _maybe_adapt() can step tx_profile mid-message
        and the profiles have different chunk_size (40 at 300baud, 100 at
        600/1200), so a message pre-split at the starting profile would keep
        sending undersized frames for the rest of the transfer even after
        stepping up."""
        if self.state != "CONNECTED":
            raise LinkError("not connected")
        toggle = 0
        sent = 0
        offset = 0
        while True:
            chunk = data[offset:offset + self.tx_profile.chunk_size]
            offset += len(chunk)
            is_last = offset >= len(data)
            seq = toggle | (EOF_BIT if is_last else 0)
            attempts = self._send_chunk_with_arq(seq, chunk)
            sent += 1
            if attempts is None:
                raise LinkError(f"no ACK for chunk {sent} ({offset}/{len(data)} bytes) "
                                f"after {MAX_RETRIES} tries")
            self._maybe_adapt(attempts)
            toggle ^= 1
            if is_last:
                break
        logger.info("send_message: %d bytes in %d chunk(s) acked", len(data), sent)

    def _send_chunk_with_arq(self, seq, chunk):
        """Returns the number of attempts it took to get ACKed, or None if
        it never got ACKed after MAX_RETRIES."""
        body = bytes([seq]) + chunk
        for attempt in range(1, MAX_RETRIES + 1):
            self.on_event("PTT", on=True)
            self._tx_packet(PT_DATA, body)
            self.on_event("PTT", off=True)
            got = self._wait_packet({PT_DATA_ACK, PT_DISC}, self.data_ack_timeout)
            if got is None:
                logger.warning("DATA seq=0x%02x: no ACK, retry %d/%d", seq, attempt, MAX_RETRIES)
                continue
            ptype, body_in = got
            if ptype == PT_DISC:
                self._handle_peer_disc()
                raise LinkError("peer disconnected mid-transfer")
            if len(body_in) >= 1 and body_in[0] == seq:
                logger.info("[%s] DATA seq=0x%02x acked after %d attempt(s)", self.mycall, seq, attempt)
                return attempt
            # ACK for a different seq (stale retransmit) -- keep waiting.
        return None

    # -- mid-session speed adaptation ---------------------------------------

    def _maybe_adapt(self, attempts):
        """Called after each ACKed chunk with how many tries it took.
        Purely ARQ-outcome based: no SNR estimate, just react to trouble
        fast and only speed up after a solid run of clean chunks."""
        if attempts >= STEP_DOWN_AFTER_ATTEMPTS:
            self._clean_streak = 0
            self._request_mode_step(-1)
            return
        self._clean_streak += 1
        if self._clean_streak >= STEP_UP_AFTER_CLEAN_STREAK:
            self._clean_streak = 0
            self._request_mode_step(+1)

    def _request_mode_step(self, direction):
        """Steps *this station's own* TX rate (the direction it's actually
        been having trouble/success with in _maybe_adapt) up or down. Never
        touches the reverse leg -- that's the peer's own tx_profile, and
        gets adapted independently by the peer's own _maybe_adapt calls
        when it's the one sending."""
        idx = afsk.PROFILES.index(self.tx_profile)
        new_idx = idx + direction
        if not (0 <= new_idx < len(afsk.PROFILES)):
            return
        candidate = afsk.PROFILES[new_idx]
        if candidate.mode_id not in self.peer_supported_modes:
            return
        logger.info("[%s] requesting mode step to %s", self.mycall, candidate.name)
        self.on_event("PTT", on=True)
        self._tx_packet(PT_MODE_REQ, bytes([candidate.mode_id]))
        self.on_event("PTT", off=True)
        got = self._wait_packet({PT_MODE_ACK}, self.control_ack_timeout)
        if got is None:
            logger.warning("[%s] MODE_REQ to %s: no ack, staying at %s", self.mycall, candidate.name,
                            self.tx_profile.name)
            return
        _, body = got
        accepted_id = body[0] if body else afsk.CONTROL_PROFILE.mode_id
        self._apply_tx_profile(afsk.PROFILES_BY_ID.get(accepted_id, afsk.CONTROL_PROFILE))
        logger.info("[%s] switched tx profile to %s", self.mycall, self.tx_profile.name)

    def _handle_mode_req(self, body):
        """The peer is telling us it's stepping *its own* TX rate -- i.e.
        our rx expectation. Accept/reject based on whether we can decode
        that rate, and update only self.rx_profile; our own tx_profile
        (the reverse leg) is unrelated and stays put."""
        proposed_id = body[0] if body else afsk.CONTROL_PROFILE.mode_id
        own_supported = {p.mode_id for p in afsk.PROFILES}
        accepted_id = proposed_id if proposed_id in own_supported else afsk.CONTROL_PROFILE.mode_id
        self.on_event("PTT", on=True)
        self._tx_packet(PT_MODE_ACK, bytes([accepted_id]))
        self.on_event("PTT", off=True)
        self._apply_rx_profile(afsk.PROFILES_BY_ID.get(accepted_id, afsk.CONTROL_PROFILE))
        logger.info("[%s] accepted peer mode step, now expecting rx at %s", self.mycall, self.rx_profile.name)

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
            got = self._wait_packet({PT_DATA, PT_DISC, PT_MODE_REQ}, remaining if remaining is not None else 1e9)
            if got is None:
                return None
            ptype, body = got
            if ptype == PT_MODE_REQ:
                self._handle_mode_req(body)
                continue  # still waiting on the actual DATA
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
        if self.peer_call is not None:
            mode_history.record_good_mode(self.mode_history, self.mycall, self.peer_call, self.tx_profile.mode_id)
        self.on_event("PTT", on=True)
        self._tx_packet(PT_DISC_ACK, b"")
        self.on_event("PTT", off=True)
        self.state = "IDLE"
        self.peer_call = None
        self.on_event("DISCONNECTED")

    def disconnect(self, timeout=None, retries=3):
        timeout = self.control_ack_timeout if timeout is None else timeout
        if self.state != "CONNECTED":
            self.state = "IDLE"
            return
        if self.peer_call is not None:
            mode_history.record_good_mode(self.mode_history, self.mycall, self.peer_call, self.tx_profile.mode_id)
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
