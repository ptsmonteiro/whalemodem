"""Half-duplex point-to-point data link: connect / send / receive / disconnect
over one radio, built as stop-and-wait ARQ on top of whale.afsk.

One frame in flight at a time, acknowledged before the next goes out.
Correctness first.

Throughput on a half-duplex link is dominated by turnaround, not by baud.
Timing the acceptance run frame by frame (both stations' logs, PTT-on
recovered as logged_time - keyed_seconds) put a steady-state 100-byte
exchange at 1200 baud at 3.91s, of which:

    2.00s  turnaround dead air, two 1.0s fixed sleeps
    0.85s  PTT lead + output-stream startup + PTT tail, two transmissions
    0.67s  the payload bits
    0.20s  the ACK frame, carrying 8 bits of information
    0.19s  the DATA frame's own sync word, length, CRC and pads

So 17% of the link was moving user data. Two things are done about it here,
neither of which changes how many frames are in flight:

  - The turnaround wait is anchored on *when the peer stopped
    transmitting*, worked out from where in the RX buffer its last frame
    ended, rather than started when we get round to replying -- so decode
    latency is absorbed by the wait instead of added to it. See
    _await_turnaround and TX_TURNAROUND_DELAY.
  - The decode loop prunes audio it has already searched, so a poll costs
    a bounded amount of time rather than growing with the idle stretch
    before it. See _prune_stale. This matters to turnaround specifically:
    the reply cannot go out until the poll that decoded the frame returns.

The third and largest item -- several DATA frames per keying under one
cumulative ACK, go-back-N -- was built and then rolled back. It never
worked on the bench: the ic705->ht leg recovered exactly one frame from 32
of its 34 two-frame bursts, the second frame syncing cleanly and then
failing its CRC every time, which is the same "sync locks, frame does not
verify" signature as the per-frame size ceilings in
scripts/sweep_payload_1200_2200.py and the 600 baud sweep. That is not
understood, and bursting is parked until it is. What survives from the
attempt is the sequence numbering (below) and the decoder fixes it forced
in whale/afsk.py, which were real bugs in their own right.

Losing a control frame
----------------------

ARQ covers a lost DATA frame and a lost DATA_ACK, because both ends keep
agreeing about what they are doing while the retransmits happen. A lost
*control* frame is different: control frames are the things that change
what each end is doing, so losing one leaves the two ends disagreeing, and
a disagreement is not something a retransmit repairs. Two of those used to
be unrecoverable.

  - A lost PT_MODE_ACK ended the session. _handle_mode_req moves rx_profile
    when it *sends* the ack; _request_mode_step moves the requester's
    tx_profile only when it *receives* one. Lose that frame and the peer
    goes on transmitting at a profile this station has stopped listening
    for -- and the only way to notice is to decode a frame, which is
    exactly what has become impossible. Every DATA frame then failed, ARQ
    exhausted its retries, and send_message raised.

    rx_profile is now a hint rather than an assertion. The pre-step profile
    stays a decode candidate until a data frame settles the question (see
    _apply_rx_profile), and a decoded PT_DATA/PT_DATA_ACK is taken as
    ground truth about what the peer is really transmitting (see
    _confirm_rx_profile). That is self-healing whichever end lost the
    frame, and it costs one extra candidate profile for one frame.

  - A lost PT_CONNECT_ACK left the session half open. The caller retried
    PT_CONNECT into a listener that had already returned from listen_once,
    and nothing anywhere handled a PT_CONNECT afterwards -- _wait_packet
    discarded them. The caller exhausted its retries and went IDLE while
    the listener sat CONNECTED with no keepalive, no timeout, and nothing
    left that could ever wake it.

    The handshake is now idempotent: a retry of the session we are already
    in is re-answered with the same CONNECT_ACK, byte for byte (see
    _answer_duplicate_connect). A caller that gives up anyway sends one
    PT_DISC on its way out, so the listener converges in seconds rather
    than waiting out INACTIVITY_TIMEOUT -- which remains as the backstop
    for everything idempotency cannot reach, including a peer that simply
    vanished.

ON-AIR FORMAT CHANGE, made deliberately for the second of those: PT_CONNECT
and PT_CONNECT_ACK each carry one extra trailing byte, a session identifier
the caller picks and the listener echoes. It is what makes "a retry of the
session I am already in" distinguishable from "a genuinely new session";
without it those are the same bytes, the listener has to guess, and
guessing "new session" resets the sequence state of a transfer that may
have chunks in flight. One byte of airtime buys an unambiguous answer.
Stations running builds from either side of this change will not
interoperate.
"""

import logging
import os
import queue
import random
import threading
import time

import numpy as np

from whale import afsk, mode_history

logger = logging.getLogger(__name__)

PT_CONNECT = 0x01
PT_CONNECT_ACK = 0x02
PT_DISC = 0x03
PT_DISC_ACK = 0x04
PT_DATA = 0x05
PT_DATA_ACK = 0x06         # body: [answered_seq, next_expected_seq] -- see _send_chunk_with_arq
PT_MODE_REQ = 0x07         # body: [proposed_mode_id] -- mid-session speed step, see _request_mode_step
PT_MODE_ACK = 0x08         # body: [accepted_mode_id] (may differ from proposed if rejected)

# Packet types whose bodies are small and must survive even when the
# negotiated data profile is struggling -- these always go out on
# afsk.CONTROL_PROFILE (see _tx_packet), never on self.tx_profile. Only bulk
# data (PT_DATA/PT_DATA_ACK) ever rides the negotiated speed.
_CONTROL_PLANE_TYPES = {PT_CONNECT, PT_CONNECT_ACK, PT_DISC, PT_DISC_ACK, PT_MODE_REQ, PT_MODE_ACK}

# The complement: the only two types that ever ride self.tx_profile. A
# decoded frame of one of these is therefore direct evidence of the profile
# the peer is actually transmitting at, which is what makes rx_profile
# self-correcting -- see _confirm_rx_profile. A decoded control-plane frame
# says nothing of the sort, since it would have gone out at
# afsk.CONTROL_PROFILE whatever either station had negotiated.
_DATA_PLANE_TYPES = {PT_DATA, PT_DATA_ACK}

# The seq byte of a DATA frame: one flag bit and a seven-bit sequence
# number.
#
# Stop-and-wait only needs one bit of sequence, and this used to be an
# alternating toggle reset at the start of each message. That cannot tell a
# retransmitted final chunk -- which arrives after its message has already
# been delivered and acked -- from the first chunk of the next message, so
# a lost ACK at a message boundary silently duplicated data. A counter that
# runs for the whole session has no such boundary to trip over. See
# _reset_sequence_state.
EOF_BIT = 0x80             # last chunk of the message
SEQ_MASK = 0x7F
SEQ_MODULO = SEQ_MASK + 1

# A DATA_ACK carries two sequence numbers, and needs both.
#
#   answered_seq       the frame this ACK is a response to
#   next_expected_seq  where the receiver's sequence now stands
#
# The second alone is what a cumulative ACK would carry, and it is
# ambiguous: an ACK reading "send me S next" is equally "your chunk S-1
# landed" and "I still want S". The receiver acks every DATA it decodes,
# duplicates included, so one lost ACK leaves a spare copy queued at the
# sender -- which, read as an answer to the frame now in flight, says
# "that did not arrive" and provokes an immediate pointless retransmit.
# That retransmit is itself a duplicate, so it draws another spare ACK, and
# the link settles into two keyings per chunk for the rest of the session.
#
# The first alone is what the pre-session-sequence code carried, and it is
# unambiguous but says nothing about where the peer got to.
#
# Carrying both costs one byte of airtime (~27ms at 300 baud) and makes
# every ACK say exactly which frame it answers and what it accomplished.

# CHUNK_SIZE (payload bytes per DATA frame -- kept small so a single
# real-hardware bit error, observed near the tail of longer frames, only
# costs a short retransmit instead of derailing a large chunk) and the
# frame-airtime-derived ACK timeout both depend on the active afsk.Profile
# (baud, in particular) -- see Link._apply_tx_profile/_apply_rx_profile,
# which compute them per instance instead of as module constants.
MAX_RETRIES = 6
DECODE_POLL_INTERVAL = 0.15

# The one byte of session identity in PT_CONNECT/PT_CONNECT_ACK. See the
# module docstring for why it is on air at all. 0 is reserved for "not
# stated" so a body that decoded short reads as unknown rather than as
# session zero; _new_session_id never returns it.
SESSION_ID_NONE = 0


def _new_session_id():
    """A fresh session identifier for one connect() attempt sequence.

    Random rather than a counter: a counter restarts at the same value
    every time the process does, and the case this has to distinguish is
    precisely "the peer restarted and is calling again" from "the peer is
    retrying the call I already answered". 255 values is plenty -- the only
    collision that matters is with the session this station is in *right
    now*, and a 1-in-255 chance of a restarted caller having to wait out
    INACTIVITY_TIMEOUT is a far smaller cost than the extra bytes of a
    wider field.
    """
    return random.randint(1, 255)


# How long a CONNECTED station will go without decoding anything at all
# from its peer before tearing the session down.
#
# MEASURED on the bench, both stations logged, by
# scripts/measure_peer_gap.py -- which reads the worst gap between frames
# decoded off the air out of each station's log. Three runs, because the
# silences that matter are not the ones a clean run produces:
#
#   clean acceptance run, 1 KB each way          5.2s  (ht->ic705 leg;
#                                                       4.0s the other way)
#   a full MAX_RETRIES cycle at 300 baud, forced
#     by suppressing the first five DATA_ACKs
#     (WHALE_DROP_PTYPE=DATA_ACK
#      WHALE_DROP_NTH=1,2,3,4,5)                44.4s
#   a mode step whose MODE_ACK never arrives:
#     control_ack_timeout                         4.3s
#
# So ~48.7s is the worst silence a *healthy* session can legitimately
# present, and this is a little over 3x that.
#
# Note the retry cycle measured 44.4s where the arithmetic says 34.8s (six
# data_ack_timeouts). The formula counts only the waiting; the five
# retransmissions in between are each a keying of their own, and their
# airtime, PTT lead and turnaround land inside the same silence. That gap
# between the computed and the measured figure is the reason this is
# measured at all -- a reasoned constant here would have been ~20% short of
# a case that occurs in normal operation.
#
# The margin on top is deliberately wide, because the cost of being wrong
# is asymmetric: too long only delays a teardown that something else
# usually beats to it (the peer's DISC, or the caller's parting DISC in
# connect()), while too short kills a session that was about to recover.
#
# What this deliberately does NOT do is keep an idle session alive. A
# station with a connection up and no user data to send transmits nothing,
# so its peer decodes nothing, and after this long the session is torn
# down. That is the accepted trade for not adding keepalive frames -- every
# keepalive is a keying, on a link where a keying costs seconds of air time
# and PTT wear. If sessions that idle longer than this ever need to
# survive, the answer is a keepalive probe (send one, retry it, tear down
# only when the probe itself goes unanswered), not a bigger number here.
INACTIVITY_TIMEOUT = 150.0

# Dead time between the end of the peer's transmission and our keying up.
#
# This was 1.0s and started when the link layer got round to replying. It
# is now anchored on the end of the peer's audio, which the receiver can
# measure -- demodulate() reports where in the RX buffer the frame ended,
# and everything captured after that is time already spent. See
# _await_turnaround. The old constant was therefore paying for the poll
# interval, the decode, and the peer's PTT tail all over again on top of
# whatever the radios actually needed.
#
# What is left for this to cover, from the peer's last CRC bit:
#
#   framing.TAIL_PAD_SECONDS   0.03   peer still transmitting pad
#   transport.PTT_TAIL         0.05   peer still keyed, carrier only
#   peer's T/R switch back to receive, and ours to transmit
#
# Getting this wrong is not a correctness problem -- too short and our
# frame lands on the tail of the peer's transmission, the ACK does not
# come, and ARQ retransmits -- but it is a throughput problem in both
# directions. scripts/sweep_turnaround.py measures the floor; it has not
# been run on the bench yet, so this is still a reasoned figure rather
# than a measured one.
#
# 0.25 was the first such figure and it was too small. The bench
# acceptance run keyed 24 of its 51 replies within 150ms of the peer's own
# PTT OFF -- 2ms in the worst case, i.e. on top of a radio that had not
# begun to swap T/R -- and lost 21 ACKs to it. Two things were wrong:
# the anchor stopped at the peer's last CRC bit and ignored the pad and
# carrier the peer still had to send after it (now PEER_TRAILING_TRANSMISSION,
# added at the anchor), and what remained was under-provisioned for the
# switch itself. 0.4s of T/R allowance on top of the peer's own tail is
# ~2.5x the largest turnaround loss seen; the sweep should replace it.
TX_TURNAROUND_DELAY = 0.4

# What the peer is still transmitting after the last bit we decoded:
# framing.TAIL_PAD_SECONDS of pad, then its own transport.PTT_TAIL of bare
# carrier. Both are the *peer's* settings and nothing on air tells us what
# they are, so this is a nominal figure for a bench where both stations run
# the same build -- which is exactly why it is stated here as an assumption
# rather than read out of our own transport module.
PEER_TRAILING_TRANSMISSION = 0.08

# How much older than the turnaround itself an anchor may be and still be
# believed. Beyond that it is not evidence about when the peer stopped
# talking -- it only says when we last managed to follow it, which is a
# different claim. A retransmit after an ACK timeout is the case that
# matters: the anchor left over from some earlier frame would otherwise
# report that the channel went quiet long ago and let us key straight over
# a peer that is still talking.
ANCHOR_AGE_SLACK = DECODE_POLL_INTERVAL


def _seq_ahead(a, b):
    """How far sequence number `a` is ahead of `b`, in a space that wraps
    at SEQ_MODULO. Only meaningful for distances shorter than half the
    space; stop-and-wait never has more than one frame outstanding, so the
    only answers that arise here are 0 and 1."""
    return (a - b) % SEQ_MODULO

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


_PTYPES_BY_NAME = {name: ptype for ptype, name in _PTYPE_NAMES.items()}


# -- test affordances --------------------------------------------------
#
# Three environment-gated hooks, all off by default, all no-ops unless the
# variable is set. They exist because the failures this module now handles
# cannot otherwise be produced on demand over a real radio link.
#
#   WHALE_DROP_PTYPE   comma-separated packet type names (MODE_ACK,
#                      CONNECT_ACK, DATA_ACK, ...) or numeric ids, whose
#                      transmission is suppressed.
#   WHALE_DROP_NTH     which occurrences of each to suppress: comma-
#                      separated 1-based ordinals, or "all". Default "1".
#   WHALE_FORCE_MODE   mode_id this station proposes at connect time (as
#                      caller) or picks for its own TX (as listener),
#                      overriding whatever mode_history remembers.
#   WHALE_MODE_STEP_SCRIPT
#                      comma-separated "<n>:<up|down>": after the nth
#                      ACKed chunk of a session, take that mode step
#                      instead of whatever _maybe_adapt would have decided.
#
# Why suppression rather than a dropped frame. A real channel cannot be
# told to lose a chosen frame, and waiting for it to lose the right one is
# not a test. But from the peer's side a MODE_ACK that was never sent is
# indistinguishable from one that was sent and lost -- the peer sees
# silence either way -- so suppressing the transmission reproduces the
# failure exactly. The software tests in tests/test_link_recovery.py drive
# the same hook, so the bench and the suite exercise one mechanism rather
# than two that have to be kept in agreement.
#
# Why the other two. The lost-MODE_ACK cases that used to be fatal are
# specific transitions -- 600<->1200, and any step down to the control
# profile -- and reaching them by waiting for _maybe_adapt to adapt its way
# there over a real channel is slow and unreliable. WHALE_FORCE_MODE picks
# the profile a session starts at; WHALE_MODE_STEP_SCRIPT picks which step
# it then takes. Neither is visible on air: the first only changes which
# mode_id a station proposes through the negotiation that already exists,
# and the second only changes when an ordinary PT_MODE_REQ goes out.


class _TxSuppressor:
    """Drops selected frames on the way out of _tx_packet, as if the
    channel had eaten them. See the note above."""

    def __init__(self, ptypes=(), occurrences=None):
        self.ptypes = set(ptypes)
        self.occurrences = occurrences  # None means every occurrence
        self._seen = {}

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        spec = (env.get("WHALE_DROP_PTYPE") or "").strip()
        if not spec:
            return cls()
        ptypes = set()
        for token in spec.split(","):
            token = token.strip().upper()
            if not token:
                continue
            ptypes.add(_PTYPES_BY_NAME[token] if token in _PTYPES_BY_NAME else int(token, 0))
        nth = (env.get("WHALE_DROP_NTH") or "1").strip().lower()
        occurrences = None if nth == "all" else {int(t) for t in nth.split(",") if t.strip()}
        return cls(ptypes, occurrences)

    def should_drop(self, ptype):
        if ptype not in self.ptypes:
            return False
        seen = self._seen.get(ptype, 0) + 1
        self._seen[ptype] = seen
        return self.occurrences is None or seen in self.occurrences


def _forced_mode_id(env=None):
    """The mode_id WHALE_FORCE_MODE pins this station's own TX to, or None."""
    env = os.environ if env is None else env
    raw = (env.get("WHALE_FORCE_MODE") or "").strip()
    if not raw:
        return None
    mode_id = int(raw, 0)
    return mode_id if mode_id in afsk.PROFILES_BY_ID else None


def _mode_step_script(env=None):
    """WHALE_MODE_STEP_SCRIPT parsed to {chunk_number: +1 | -1}."""
    env = os.environ if env is None else env
    script = {}
    for token in (env.get("WHALE_MODE_STEP_SCRIPT") or "").split(","):
        token = token.strip()
        if not token:
            continue
        nth, _, direction = token.partition(":")
        script[int(nth)] = +1 if direction.strip().lower().startswith("u") else -1
    return script


def _encode_call_pair(src, dst):
    return src.encode("ascii") + b"\x00" + dst.encode("ascii")


def _decode_call_pair(payload):
    src, _, dst = payload.partition(b"\x00")
    return src.decode("ascii", "replace"), dst.decode("ascii", "replace")


def _encode_call_and_modes(a, b, supported_ids, extra_id, session_id=SESSION_ID_NONE):
    """CONNECT body: "a\\x00b\\x00" + one byte per supported mode_id + two
    trailing bytes -- the sender's proposed TX mode_id for the a->b
    direction, and the session identifier it has picked for this call (see
    the module docstring; SESSION_ID_NONE if it has none)."""
    return (a.encode("ascii") + b"\x00" + b.encode("ascii") + b"\x00" +
            bytes(sorted(supported_ids)) + bytes([extra_id, session_id]))


def _decode_call_and_modes(payload):
    a, _, rest = payload.partition(b"\x00")
    b, _, mode_section = rest.partition(b"\x00")
    a = a.decode("ascii", "replace")
    b = b.decode("ascii", "replace")
    if len(mode_section) < 2:
        return a, b, [], afsk.CONTROL_PROFILE.mode_id, SESSION_ID_NONE
    return a, b, list(mode_section[:-2]), mode_section[-2], mode_section[-1]


def _encode_connect_ack(a, b, supported_ids, accepted_tx_id, own_tx_id,
                        session_id=SESSION_ID_NONE):
    """CONNECT_ACK body: same shape as CONNECT's, but with *three* trailing
    bytes instead of two -- the two directions of the link are negotiated
    independently (one station's TX quality to its peer is not the same as
    the reverse leg, see whale/afsk.py's measured per-direction SNR), so the
    listener must report back both: the mode_id it's accepting for the
    caller's proposed (a->b) direction, and the mode_id it has separately
    chosen for its own (b->a) transmissions. The third is the caller's own
    session identifier echoed back unchanged, which is what lets the caller
    tell this ack from a leftover ack for some earlier session."""
    return (a.encode("ascii") + b"\x00" + b.encode("ascii") + b"\x00" +
            bytes(sorted(supported_ids)) + bytes([accepted_tx_id, own_tx_id, session_id]))


def _decode_connect_ack(payload):
    a, _, rest = payload.partition(b"\x00")
    b, _, mode_section = rest.partition(b"\x00")
    a = a.decode("ascii", "replace")
    b = b.decode("ascii", "replace")
    if len(mode_section) < 3:
        return (a, b, [], afsk.CONTROL_PROFILE.mode_id, afsk.CONTROL_PROFILE.mode_id,
                SESSION_ID_NONE)
    return (a, b, list(mode_section[:-3]), mode_section[-3], mode_section[-2],
            mode_section[-1])


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
        # A second profile the decoder keeps trying while it is not yet
        # settled which of the two the peer is transmitting at. Only ever
        # set across a mode step -- see _apply_rx_profile.
        self._rx_profile_fallback = None
        self._recompute_timings()

        # Env-gated, off by default, and no-ops unless the corresponding
        # variable is set -- see the "test affordances" note above.
        self.tx_suppress = _TxSuppressor.from_env()
        self._mode_step_script = _mode_step_script()

        self._rx_packets = queue.Queue()
        self._partial_rx_buf = None  # in-progress recv_message() reassembly, see recv_message()
        self._tx_seq = 0
        self._rx_expect_seq = 0
        self._acked_chunks = 0
        # Session identity, and the ack that established it. Both are what
        # make a retried PT_CONNECT answerable after listen_once has
        # returned -- see _answer_duplicate_connect.
        self._session_id = SESSION_ID_NONE
        self._connect_ack_body = None
        # When we last decoded anything from the peer, in time.monotonic()
        # terms. Written by the decode thread, read by _peer_is_stale.
        self._last_peer_frame_at = None
        # Set by the decode thread to when the peer's audio ended, in
        # time.monotonic() terms; consumed by _await_turnaround.
        self._peer_unkeyed_at = None
        self._stop = threading.Event()
        self._decode_thread = threading.Thread(target=self._decode_loop, daemon=True)

    def start(self):
        self.transport.start_receiving()
        self._decode_thread.start()

    def stop(self):
        self._stop.set()
        self.transport.stop_receiving()

    def _reset_sequence_state(self):
        """Clears everything that is scoped to one session, at the moment a
        session begins.

        Sequence numbers run for the life of the session rather than
        restarting per message: a retransmitted final chunk arrives after
        its message has already been delivered, and a counter that restarts
        at zero each message cannot tell that duplicate from the first
        chunk of the next one. Both stations start a session at zero, so
        both ends reset here and nowhere else."""
        self._partial_rx_buf = None
        self._tx_seq = 0
        self._rx_expect_seq = 0
        self._acked_chunks = 0
        # Arm the inactivity backstop from the handshake rather than from
        # the first frame after it: a listener whose CONNECT_ACK was lost
        # may never decode anything from its peer at all, and that is
        # precisely the session that has to time out.
        self._last_peer_frame_at = time.monotonic()

    # -- profile management -----------------------------------------------

    def _apply_tx_profile(self, profile):
        """Sets the profile this station uses to transmit (DATA when it's
        the sender, DATA_ACK when it's replying -- both reflect the same
        outbound RF path to the peer). Control-plane frames are unaffected
        -- they always use afsk.CONTROL_PROFILE regardless of this."""
        self.tx_profile = profile
        self._recompute_timings()

    def _apply_rx_profile(self, profile, fallback=None):
        """Sets the profile this station expects the *peer's* transmissions
        at -- i.e. the peer's own tx_profile, as far as this station knows
        it. Used by the decode loop and (indirectly) by the ACK timeout.

        `fallback` is a second profile to go on trying until a data frame
        settles which of the two the peer is really using. It exists for
        one case, and the case is unrecoverable without it: we have just
        accepted the peer's PT_MODE_REQ and sent a PT_MODE_ACK, but the
        peer only moves its tx_profile on *receiving* that ack. If it never
        arrives, the peer stays where it was and this station is the only
        one that moved -- and with only the new profile as a candidate, its
        frames stop decoding entirely. Nothing then recovers, because the
        only evidence that could correct the mistake is a decoded frame.

        Keeping the old profile as a candidate makes that frame decodable;
        _confirm_rx_profile makes it authoritative. The cost is one extra
        demodulate() pass per poll, for as long as it takes one data frame
        to arrive."""
        self.rx_profile = profile
        self._rx_profile_fallback = fallback if fallback is not profile else None
        self._recompute_timings()

    def _confirm_rx_profile(self, profile):
        """Takes a decoded data-plane frame as ground truth about what the
        peer is transmitting at, correcting rx_profile if they disagree.

        This is the other half of _apply_rx_profile's fallback, and it is
        what makes the two ends converge again after a lost PT_MODE_ACK --
        in one frame, and whichever end lost it. rx_profile is only ever a
        belief about the peer's tx_profile; a frame that actually decoded
        is not a belief.

        Only PT_DATA/PT_DATA_ACK count (_DATA_PLANE_TYPES). Control-plane
        frames always ride afsk.CONTROL_PROFILE regardless of what either
        station negotiated, so treating a decoded MODE_REQ or DISC as
        evidence would drag rx_profile back down to the control profile
        every time the peer stepped up."""
        if profile is self.rx_profile:
            self._rx_profile_fallback = None
            return
        logger.info("[%s] peer is transmitting at %s, not %s -- adopting what decoded",
                    self.mycall, profile.name, self.rx_profile.name)
        self._apply_rx_profile(profile)

    def _recompute_timings(self):
        # Everything here is a function of the two negotiated profiles, and
        # the two legs can run at different baud, so each is accounted for
        # separately rather than doubling one.
        #
        # Worst-case round trip for one DATA/DATA_ACK exchange: our DATA
        # frame out at tx_profile, then the peer's (tiny) ACK back at
        # rx_profile, with a turnaround at each end.
        tx_airtime = afsk.frame_seconds(self.tx_profile.chunk_size + 2, self.tx_profile)
        ack_airtime = afsk.frame_seconds(3, self.rx_profile)
        self.data_ack_timeout = (tx_airtime + ack_airtime
                                 + 2 * TX_TURNAROUND_DELAY + 3.0)

        # How much recent audio a poll that found nothing must leave alone
        # (see _prune_stale) -- enough that the longest frame either
        # candidate profile could be part-way through is never cut in half.
        self._rx_keep_seconds = max(
            afsk.frame_seconds(p.chunk_size + 2, p) for p in self._candidate_decode_profiles()) + 1.0

    def _candidate_decode_profiles(self):
        """Which afsk.Profile(s) an incoming frame might be using right
        now: control-plane traffic always uses CONTROL_PROFILE, and DATA
        traffic uses whatever self.rx_profile currently is (the peer's own
        tx_profile) -- try both since the decode loop can't otherwise tell
        which is arriving next.

        A third appears transiently, while a mode step is still unconfirmed
        and the peer may not have taken it: see _apply_rx_profile. It is
        dropped again as soon as one data frame says which of the two the
        peer is really using."""
        candidates = [afsk.CONTROL_PROFILE]
        for profile in (self.rx_profile, self._rx_profile_fallback):
            if profile is not None and profile not in candidates:
                candidates.append(profile)
        return tuple(candidates)

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
        near_miss = None  # (end_index, confidence) of the earliest non-decoding sync, if any
        still_arriving = False
        for profile in self._candidate_decode_profiles():
            result = afsk.demodulate(snap, profile=profile)
            if result.get("payload") is not None:
                end = result.get("end_index", len(snap))
                # Anchor the turnaround on when the peer's audio actually
                # ended rather than on now: everything captured after this
                # frame -- the rest of the poll interval, this decode, the
                # peer's own PTT tail -- is settling time already spent.
                # See _await_turnaround.
                trailing = max(0, len(snap) - end)
                self._peer_unkeyed_at = (time.monotonic() - trailing / afsk.SAMPLE_RATE
                                             + PEER_TRAILING_TRANSMISSION)
                self.transport.consume_rx(end)
                logger.info("[%s] decoded frame at profile %s (confidence=%.2f)",
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
                # it, this same match stays the strongest peak on every
                # future poll and a later, weaker, genuine frame elsewhere
                # in the buffer never gets a look in.
                #
                # Advance past the *sync word only*, not to the end of the
                # frame this position claims. A frame that failed to decode
                # is by definition one whose length byte we have no reason
                # to trust: a garbage length of 255 at 300 baud would skip
                # seven seconds of audio, stepping straight over a real
                # frame arriving behind it. Stepping past the sync is all
                # that is needed to guarantee forward progress.
                #
                # Earliest wins, for the same reason -- whatever else is in
                # the buffer is behind this one.
                skip = result.get("sync_end_index", result["end_index"])
                if near_miss is None or skip < near_miss[0]:
                    near_miss = (skip, result.get("confidence", 0))
        if near_miss is not None and not still_arriving:
            logger.info("[%s] near-miss decode: sync found but frame invalid (confidence=%.2f len(snap)=%d)",
                        self.mycall, near_miss[1], len(snap))
            self._capture_near_miss(snap, near_miss[1])
            self.transport.consume_rx(near_miss[0])
            return True
        if not still_arriving:
            self._prune_stale(len(snap))
        return False

    def _capture_near_miss(self, snap, confidence):
        """Saves the audio a near-miss gave up on, if WHALE_CAPTURE_DIR is
        set in the environment. Off by default.

        This was added for one open question -- frames that sync strongly
        and then fail CRC anyway, on the ic705->ht leg only, which is the
        signature behind the per-frame size ceilings in
        scripts/sweep_payload_1200_2200.py and behind the burst attempt
        being rolled back (see the module docstring). That question is
        answered: priority scan was enabled on the HT, muting its receiver
        for ~280ms every ~3s, so any keying still running at 3.0s lost a
        chunk out of its middle. With the scan off the ceiling is gone
        entirely -- 255-byte payloads pass 100% both directions. See
        scripts/probe_tx_duration_dropout.py, which separated keying
        duration from payload size by holding the payload constant.

        Worth keeping anyway. It is the only way to see what the radio
        actually received rather than what the decoder made of it, and the
        reason the original question took so long was that nothing about it
        reproduced in software -- progressive arrival, truncation at every
        offset from 0 to 800ms, AWGN down to 15 dB and real off-air noise
        beds all decode fine. If a near-miss shows up that the dropout does
        not explain, these captures are again the input to the analysis.
        """
        directory = os.environ.get("WHALE_CAPTURE_DIR")
        if not directory:
            return
        try:
            os.makedirs(directory, exist_ok=True)
            name = f"nearmiss_{self.mycall}_{time.time():.3f}_c{confidence:.2f}_rx{self.rx_profile.name}.npy"
            np.save(os.path.join(directory, name), np.asarray(snap, dtype=np.float32))
        except Exception:
            # A diagnostic must never be able to take the link down.
            logger.exception("[%s] near-miss capture failed", self.mycall)

    def _prune_stale(self, snap_len):
        """Drops audio this poll searched and found nothing whatsoever in.

        Without it the buffer grows to transport.RX_BUFFER_SECONDS through
        any idle stretch and every later poll re-searches all of it.
        demodulate() costs roughly 14ms per second of buffer per candidate
        profile, so a full 10s buffer turns a 40ms poll into a 280ms one --
        and that lands directly on the turnaround, since the reply cannot
        be sent until the poll that decodes the frame finishes. Keeping the
        most recent _rx_keep_seconds bounds the cost at about one frame's
        worth while leaving any part-arrived frame intact."""
        keep = int(self._rx_keep_seconds * afsk.SAMPLE_RATE)
        if snap_len > keep:
            self.transport.consume_rx(snap_len - keep)

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
        # Anything that got this far came off the air from the peer, so it
        # is proof of life whether or not whatever is waiting wants it.
        self._last_peer_frame_at = time.monotonic()
        if ptype in _DATA_PLANE_TYPES:
            self._confirm_rx_profile(profile)
        logger.info("[%s] RX %s at %s (%d body byte(s))", self.mycall, _ptype_name(ptype), profile.name, len(body))
        self._rx_packets.put((ptype, body))

    def _await_turnaround(self):
        """Blocks until it is safe to key up over the peer.

        A reply sent essentially back-to-back with the frame it's replying
        to reaches the peer garbled or not at all on this rig: the peer is
        still transmitting its tail pad and holding its carrier, and
        neither radio has finished swapping T/R. What that costs is a fixed
        span of time *after the peer's audio ends*, so that -- not the
        moment we happen to reach this line -- is what it is measured from.
        _decode_one records the anchor when it reads a frame out of the RX
        buffer, and by then the poll interval, the decode, and the peer's
        PTT tail have usually consumed most of the wait already.

        With no anchor (we're opening the exchange, or we're retransmitting
        after a timeout and nothing came back) there is nothing to measure
        from, so wait the whole allowance. A *stale* anchor is treated the
        same way and for a stronger reason: it does not mean "the peer
        finished long ago", it means we stopped being able to follow what
        the peer was saying, which is the worst moment to assume the
        channel is free. See ANCHOR_AGE_SLACK."""
        anchor = self._peer_unkeyed_at
        self._peer_unkeyed_at = None
        now = time.monotonic()
        if anchor is None or now - anchor > TX_TURNAROUND_DELAY + ANCHOR_AGE_SLACK:
            time.sleep(TX_TURNAROUND_DELAY)
            return
        remaining = (anchor + TX_TURNAROUND_DELAY) - now
        if remaining > 0:
            time.sleep(remaining)

    def _tx_packet(self, ptype: int, body: bytes):
        """Waits out the turnaround, then keys up for one frame."""
        self._await_turnaround()
        profile = afsk.CONTROL_PROFILE if ptype in _CONTROL_PLANE_TYPES else self.tx_profile
        if self.tx_suppress.should_drop(ptype):
            # Test affordance only (WHALE_DROP_PTYPE) -- see _TxSuppressor.
            # Everything up to this line has already happened, turnaround
            # included, so the caller's own timing is exactly what it would
            # have been; the frame simply never reaches the air.
            logger.warning("[%s] SUPPRESSING TX %s at %s (%d body byte(s)) -- WHALE_DROP_PTYPE",
                           self.mycall, _ptype_name(ptype), profile.name, len(body))
            return
        audio = afsk.modulate(bytes([ptype]) + body, profile=profile)
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
            if ptype == PT_CONNECT and self._answer_duplicate_connect(body):
                continue
            # Not what we're waiting for right now (e.g. a stray DISC from a
            # previous session) -- drop it and keep waiting.
            logger.info("[%s] dropping unexpected %s while waiting for %s", self.mycall,
                        _ptype_name(ptype), {_ptype_name(t) for t in want_types})

    def _answer_duplicate_connect(self, body):
        """Re-answers a PT_CONNECT that is a retry of the session we are
        already in, and reports whether it did.

        This is the one deliberate exception to _wait_packet discarding
        whatever it was not waiting for, and it is the whole fix for a lost
        PT_CONNECT_ACK. The handshake used to be answered exactly once, in
        listen_once; afterwards nothing anywhere handled a PT_CONNECT, so a
        caller retrying because it never heard the ack was retrying into
        silence. It gave up and went IDLE while this station stayed
        CONNECTED -- a half-open session with nothing left to end it.

        Re-answering is the entire response, because the listener's state
        already *is* what the ack describes. The stored body goes back out
        byte for byte rather than being rebuilt, so a retry cannot
        renegotiate anything: the caller ends up with exactly the profiles
        it would have had if the first ack had arrived.

        Narrow on purpose -- only while CONNECTED, only from our own peer,
        and only for the session id we are actually in. A PT_CONNECT
        carrying a *different* session id is a genuinely new call (the peer
        restarted), and adopting it here would reset the sequence state
        underneath a transfer that may have chunks in flight. So it is
        dropped instead, and the caller gets in once INACTIVITY_TIMEOUT has
        cleared this session -- slow, but it cannot corrupt a live one.
        Telling those two cases apart is the entire reason the session id
        is on air; see the module docstring."""
        if self.state != "CONNECTED" or self._connect_ack_body is None:
            return False
        src, dst, _, _, session_id = _decode_call_and_modes(body)
        if dst != self.mycall or src != self.peer_call:
            return False
        if session_id != self._session_id:
            logger.warning("[%s] CONNECT from %s carries session 0x%02x but we are in "
                           "0x%02x -- ignoring rather than resetting a live session",
                           self.mycall, src, session_id, self._session_id)
            return False
        logger.info("[%s] re-answering a duplicate CONNECT from %s (session 0x%02x)",
                    self.mycall, src, session_id)
        self.on_event("PTT", on=True)
        self._tx_packet(PT_CONNECT_ACK, self._connect_ack_body)
        self.on_event("PTT", off=True)
        return True

    def _peer_is_stale(self):
        """True when this station has been CONNECTED for longer than
        INACTIVITY_TIMEOUT with nothing decoded from its peer at all."""
        if self.state != "CONNECTED" or self._last_peer_frame_at is None:
            return False
        return time.monotonic() - self._last_peer_frame_at > INACTIVITY_TIMEOUT

    def _abandon_stale_session(self):
        """Tears down a session whose peer has stopped saying anything.

        The backstop, not the primary mechanism: a lost CONNECT_ACK is
        normally repaired by _answer_duplicate_connect, and a caller that
        gives up anyway sends a PT_DISC on its way out. This covers what
        neither reaches -- a peer that was switched off, moved out of
        range, or crashed. The DISC that disconnect() sends is best effort
        and quite likely lands on nobody; it costs one keying and, when
        there *is* somebody, ends their side too."""
        logger.warning("[%s] nothing decoded from %s in %.0fs -- abandoning the session",
                       self.mycall, self.peer_call, INACTIVITY_TIMEOUT)
        self.disconnect(retries=1)

    def service_while_idle(self):
        """Housekeeping for a station that is CONNECTED but is not, right
        now, inside send_message or recv_message. Returns False once the
        session is over.

        There is one such window and it is exactly where the half-open
        session used to lodge: between going CONNECTED and vara_server's
        local client opening its data socket, nothing on this station reads
        decoded packets at all. A caller's CONNECT retries pile up in the
        queue unanswered, and a peer that has given up cannot be noticed.
        Calling this from that wait closes it."""
        if self.state != "CONNECTED":
            return False
        while True:
            try:
                ptype, body = self._rx_packets.get_nowait()
            except queue.Empty:
                break
            if ptype == PT_CONNECT:
                self._answer_duplicate_connect(body)
            elif ptype == PT_DISC:
                self._handle_peer_disc()
                return False
            else:
                logger.info("[%s] dropping %s received while idle", self.mycall, _ptype_name(ptype))
        if self._peer_is_stale():
            self._abandon_stale_session()
            return False
        return True

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
        # Not `forced or history`: mode_id 0 is a real profile (300 baud)
        # and a perfectly reasonable thing to pin a bench run to.
        proposed_id = _forced_mode_id()
        if proposed_id is None:
            proposed_id = mode_history.last_good_mode(self.mode_history, self.mycall, dst_call)
        if proposed_id is None or proposed_id not in own_supported:
            proposed_id = afsk.CONTROL_PROFILE.mode_id  # no history with this peer -- start slow
        # One id for the whole retry sequence, not one per attempt: every
        # CONNECT below is the same call, and a listener that answered an
        # earlier one has to recognise the later ones as such.
        self._session_id = _new_session_id()
        body = _encode_call_and_modes(self.mycall, dst_call, own_supported, proposed_id,
                                      self._session_id)
        for attempt in range(1, retries + 1):
            logger.info("[%s] CONNECT attempt %d/%d to %s (proposing mode %d)",
                        self.mycall, attempt, retries, dst_call, proposed_id)
            self.on_event("PTT", on=True)
            self._tx_packet(PT_CONNECT, body)
            self.on_event("PTT", off=True)
            got = self._wait_packet({PT_CONNECT_ACK}, timeout_per_try)
            if got is not None:
                _, ack_body = got
                (src, dst, peer_supported, accepted_id, peer_tx_id,
                 ack_session) = _decode_connect_ack(ack_body)
                if dst != self.mycall:
                    continue
                if ack_session != self._session_id:
                    # An ack for some earlier call of ours, still in the
                    # buffer or still in flight. It describes profiles that
                    # were negotiated for a session that no longer exists.
                    logger.info("[%s] ignoring CONNECT_ACK for session 0x%02x (calling as 0x%02x)",
                                self.mycall, ack_session, self._session_id)
                    continue
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
                self._reset_sequence_state()
                self.on_event("CONNECTED", mycall=self.mycall, peer=self.peer_call)
                logger.info("[%s] connected to %s: tx=%s rx=%s", self.mycall, self.peer_call,
                            self.tx_profile.name, self.rx_profile.name)
                return True
        # Giving up. Somebody may nonetheless have answered one of those
        # CONNECTs and be sitting CONNECTED right now with an ack we never
        # heard -- that is the half-open session, seen from the other side.
        # One PT_DISC converges the two ends in seconds instead of leaving
        # the listener to wait out INACTIVITY_TIMEOUT. Best effort: in the
        # ordinary "nobody home" case it lands on nobody, which costs one
        # keying on a call that has already spent `retries` of them.
        logger.info("[%s] CONNECT to %s gave up after %d attempt(s) -- sending DISC in case "
                    "the far end answered an ack we never heard", self.mycall, dst_call, retries)
        self.on_event("PTT", on=True)
        self._tx_packet(PT_DISC, b"")
        self.on_event("PTT", off=True)
        self.state = "IDLE"
        self._session_id = SESSION_ID_NONE
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
        src, dst, peer_supported, proposed_id, session_id = _decode_call_and_modes(body)
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
        own_tx_id = _forced_mode_id()   # mode_id 0 is falsy, so not `or`
        if own_tx_id is None:
            own_tx_id = mode_history.last_good_mode(self.mode_history, self.mycall, src)
        if (own_tx_id is None or own_tx_id not in own_supported
                or own_tx_id not in peer_supported):
            own_tx_id = afsk.CONTROL_PROFILE.mode_id
        ack_body = _encode_connect_ack(self.mycall, src, own_supported, negotiated_id, own_tx_id,
                                       session_id)
        # Both remembered *before* the ack goes out, not after: this is the
        # frame that may be lost, and if it is, the caller's retry can
        # arrive while we are still inside _tx_packet. Everything needed to
        # re-answer it has to be in place by then. See
        # _answer_duplicate_connect.
        self._session_id = session_id
        self._connect_ack_body = ack_body
        self.on_event("PTT", on=True)
        self._tx_packet(PT_CONNECT_ACK, ack_body)
        self.on_event("PTT", off=True)
        self._apply_rx_profile(afsk.PROFILES_BY_ID.get(negotiated_id, afsk.CONTROL_PROFILE))
        self._apply_tx_profile(afsk.PROFILES_BY_ID.get(own_tx_id, afsk.CONTROL_PROFILE))
        self._clean_streak = 0
        self.state = "CONNECTED"
        self._reset_sequence_state()
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
        sent = 0
        offset = 0
        while True:
            chunk = data[offset:offset + self.tx_profile.chunk_size]
            offset += len(chunk)
            is_last = offset >= len(data)
            attempts = self._send_chunk_with_arq(self._tx_seq, chunk, is_last)
            sent += 1
            if attempts is None:
                raise LinkError(f"no ACK for chunk {sent} ({offset}/{len(data)} bytes) "
                                f"after {MAX_RETRIES} tries")
            self._tx_seq = (self._tx_seq + 1) % SEQ_MODULO
            self._maybe_adapt(attempts)
            if is_last:
                break
        logger.info("send_message: %d bytes in %d chunk(s) acked", len(data), sent)

    def _send_chunk_with_arq(self, seq, chunk, is_eof):
        """Returns the number of attempts it took to get ACKed, or None if
        it never got ACKed after MAX_RETRIES.

        Note there is no "the peer told us it never arrived" shortcut here.
        The receiver only ever transmits in response to a DATA frame it
        decoded, so a chunk that did not decode produces no ACK at all --
        the timeout is the only signal there is. (A shortcut did exist while
        the link was bursting, where a keying's later frames failing CRC
        left the receiver acking an earlier frame and thereby saying
        something useful about the current one. One frame per keying, and
        that channel of information is gone with it.)

        PT_MODE_REQ is answered here as well as in recv_message. It should
        not normally arrive while we are the sender -- the peer only steps
        its own TX rate after a chunk *it* sent got acked, which means we
        were receiving -- but the roles can swap between the ack and the
        step, and until this was handled the request was simply discarded
        and the peer spent a full control_ack_timeout waiting for an answer
        that no longer had anywhere to come from. Accepting it mid-ARQ is
        safe: it moves rx_profile, and _apply_rx_profile's fallback covers
        the DATA_ACK that may already be in flight at the old one."""
        body = bytes([seq | (EOF_BIT if is_eof else 0)]) + chunk
        for attempt in range(1, MAX_RETRIES + 1):
            self.on_event("PTT", on=True)
            self._tx_packet(PT_DATA, body)
            self.on_event("PTT", off=True)
            deadline = time.monotonic() + self.data_ack_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                got = self._wait_packet({PT_DATA_ACK, PT_DISC, PT_MODE_REQ}, remaining)
                if got is None:
                    break
                ptype, body_in = got
                if ptype == PT_DISC:
                    self._handle_peer_disc()
                    raise LinkError("peer disconnected mid-transfer")
                if ptype == PT_MODE_REQ:
                    self._handle_mode_req(body_in)
                    continue
                if len(body_in) < 2:
                    continue
                answered, expects = body_in[0] & SEQ_MASK, body_in[1] & SEQ_MASK
                if answered != seq:
                    # An answer to a frame we have already moved past --
                    # most often the receiver's second ack of a chunk we
                    # retransmitted. It says nothing about the frame in
                    # flight, so keep waiting for one that does rather than
                    # retransmitting on it. See PT_DATA_ACK's format note.
                    logger.info("[%s] ignoring stale ACK (answers 0x%02x, waiting on 0x%02x)",
                                self.mycall, answered, seq)
                    continue
                if _seq_ahead(expects, seq) == 1:
                    # Accepted. True of a duplicate as naturally as of a
                    # fresh frame, which is what makes retransmitting after
                    # a lost ACK safe.
                    logger.info("[%s] DATA seq=0x%02x acked after %d attempt(s)",
                                self.mycall, seq, attempt)
                    return attempt
                # The peer decoded this very frame and still did not advance
                # past it, so the two ends disagree about where the sequence
                # stands. Retransmitting cannot repair that; let the attempts
                # run out and surface it as a LinkError instead of looping.
                logger.warning("[%s] peer answered seq 0x%02x but expects 0x%02x -- sequence desync",
                               self.mycall, answered, expects)
            logger.warning("DATA seq=0x%02x: no ACK, retry %d/%d", seq, attempt, MAX_RETRIES)
        return None

    # -- mid-session speed adaptation ---------------------------------------

    def _maybe_adapt(self, attempts):
        """Called after each ACKed chunk with how many tries it took.
        Purely ARQ-outcome based: no SNR estimate, just react to trouble
        fast and only speed up after a solid run of clean chunks."""
        self._acked_chunks += 1
        scripted = self._mode_step_script.get(self._acked_chunks)
        if scripted is not None:
            # Test affordance only (WHALE_MODE_STEP_SCRIPT) -- see the note
            # above _TxSuppressor for why a bench run needs to choose its
            # own transitions rather than wait for the channel to produce
            # them.
            logger.warning("[%s] taking scripted mode step %+d after chunk %d -- "
                           "WHALE_MODE_STEP_SCRIPT", self.mycall, scripted, self._acked_chunks)
            self._clean_streak = 0
            self._request_mode_step(scripted)
            return
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
        (the reverse leg) is unrelated and stays put.

        Note the asymmetry this has to live with, which is not fixable from
        here: we move on *sending* the ack, the peer moves on *receiving*
        it, and no acknowledgement scheme removes the last hop's
        uncertainty. So the old profile is kept as a decode fallback rather
        than assumed dead -- if the ack is lost, the peer goes on
        transmitting at it, and that frame is the only thing that can tell
        us so. See _apply_rx_profile and _confirm_rx_profile."""
        proposed_id = body[0] if body else afsk.CONTROL_PROFILE.mode_id
        own_supported = {p.mode_id for p in afsk.PROFILES}
        accepted_id = proposed_id if proposed_id in own_supported else afsk.CONTROL_PROFILE.mode_id
        previous = self.rx_profile
        self.on_event("PTT", on=True)
        self._tx_packet(PT_MODE_ACK, bytes([accepted_id]))
        self.on_event("PTT", off=True)
        self._apply_rx_profile(afsk.PROFILES_BY_ID.get(accepted_id, afsk.CONTROL_PROFILE),
                               fallback=previous)
        logger.info("[%s] accepted peer mode step, now expecting rx at %s (still trying %s "
                    "until a data frame settles it)", self.mycall, self.rx_profile.name,
                    previous.name)

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
            if self._peer_is_stale():
                # The pump calls this on a short timeout over and over, so
                # this is where a station that is merely *waiting* spends
                # its time -- and therefore where a peer that has gone away
                # has to be noticed. See INACTIVITY_TIMEOUT.
                self._abandon_stale_session()
                return None
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
            flags, chunk = body[0], body[1:]
            seq = flags & SEQ_MASK
            message = None
            if seq == self._rx_expect_seq:
                self._partial_rx_buf += chunk
                self._rx_expect_seq = (seq + 1) % SEQ_MODULO
                if flags & EOF_BIT:
                    message = bytes(self._partial_rx_buf)
                    self._partial_rx_buf = bytearray()
            else:
                # A duplicate retransmit: the sender missed our ACK. Already
                # delivered, so drop the payload -- but still ack below,
                # because the sender is waiting on exactly that.
                logger.info("[%s] DATA seq=0x%02x already have (expecting 0x%02x) -- dropping",
                            self.mycall, seq, self._rx_expect_seq)
            # Ack every DATA we see, duplicates included -- the sender is
            # waiting on it and may have missed our first ack. The ack names
            # the frame it answers *and* the sequence number we want next,
            # so a spare copy of it arriving later cannot be mistaken for an
            # answer to a different frame (see PT_DATA_ACK's format note).
            # Naming what we want next covers a duplicate as naturally as a
            # fresh frame, including a retransmitted final chunk of a
            # message already handed to the caller -- which is why sequence
            # numbers run for the whole session rather than restarting per
            # message.
            self.on_event("PTT", on=True)
            self._tx_packet(PT_DATA_ACK, bytes([seq, self._rx_expect_seq]))
            self.on_event("PTT", off=True)
            if message is not None:
                return message

    # -- teardown ------------------------------------------------------

    def _forget_session(self):
        """Drops the identity of the session that has just ended, so a
        PT_CONNECT arriving afterwards is treated as a new call rather than
        re-answered as a retry of a session that no longer exists."""
        self._session_id = SESSION_ID_NONE
        self._connect_ack_body = None
        self._last_peer_frame_at = None

    def _handle_peer_disc(self):
        if self.peer_call is not None:
            mode_history.record_good_mode(self.mode_history, self.mycall, self.peer_call, self.tx_profile.mode_id)
        self.on_event("PTT", on=True)
        self._tx_packet(PT_DISC_ACK, b"")
        self.on_event("PTT", off=True)
        self.state = "IDLE"
        self.peer_call = None
        self._forget_session()
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
        self._forget_session()
        self.on_event("DISCONNECTED")
