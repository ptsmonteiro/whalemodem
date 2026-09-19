"""Half-duplex point-to-point data link: connect / send / receive / disconnect
over one radio, built as stop-and-wait ARQ over a negotiable physical-layer
mode.

One frame in flight at a time, acknowledged before the next goes out.
Correctness first.

Throughput on a half-duplex link is dominated by turnaround, not by baud.
Timing the acceptance run frame by frame (both stations' logs, PTT-on
recovered as logged_time - keyed_seconds) put a steady-state 100-byte
exchange on the fast historical AFSK profile at 3.91s, of which:

    2.00s  turnaround dead air, two 1.0s fixed sleeps
    0.85s  PTT lead + output-stream startup + PTT tail, two transmissions
    0.67s  the payload bits
    0.20s  the ACK frame, carrying 8 bits of information
    0.19s  the DATA frame's own sync word, length, CRC and pads

So 17% of the link was moving user data. The decode loop prunes audio it has
already searched, so a poll costs a bounded amount of time rather than growing
with the idle stretch before it. See _prune_stale.

The third and largest item -- several DATA frames per keying under one
cumulative ACK, go-back-N -- was built and then rolled back. It never
worked on the bench: the ic705->ht leg recovered exactly one frame from 32
of its 34 two-frame bursts, the second frame syncing cleanly and then
failing its CRC every time, which is the same "sync locks, frame does not
verify" signature as the per-frame size ceilings seen sweeping the frame
budget. That is not
understood, and bursting is parked until it is. What survives from the
attempt is the sequence numbering (below) and the decoder fixes it forced,
which were real bugs in their own right.

Losing a control frame
----------------------

ARQ covers a lost DATA frame and a lost DATA_ACK, because both ends keep
agreeing about what they are doing while the retransmits happen. A lost
*control* frame is different: control frames are the things that change
what each end is doing, so losing one leaves the two ends disagreeing, and
a disagreement is not something a retransmit repairs. Two of those used to
be unrecoverable.

  - Mode changes use no control exchange. The receiver searches every
    mutually advertised DATA mode, and DATA_ACK echoes the mode actually
    decoded. This lets a sender step down after silence and retry the same
    chunk without first delivering a request at the failing speed.

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

ON-AIR FORMAT CHANGE for connection version 5: DATA no longer carries a
head-duration byte and DATA_ACK no longer carries a head request.
Every waveform supplies its own fixed-duration native preamble. The checked air
header version also advances so older peers are rejected explicitly.
"""

import logging
import os
import queue
import random
import time

from whale import mode_history
from whale import link_protocol as protocol
from whale.link_adaptation import _AdaptationMixin
from whale.link_receiver import (
    DECODE_POLL_BUDGET_FRACTION,
    DECODE_POLL_INTERVAL,
    _ReceiverMixin,
    _decode_snr_summary,
    net_bits_per_second,
)
from whale.policy import FM

# Public compatibility aliases.  The wire-format implementation lives in
# link_protocol; callers that historically imported these from whale.link
# continue to see the same API.
PT_CONNECT = protocol.PT_CONNECT
PT_CONNECT_ACK = protocol.PT_CONNECT_ACK
PT_DISC = protocol.PT_DISC
PT_DISC_ACK = protocol.PT_DISC_ACK
PT_DATA = protocol.PT_DATA
PT_DATA_ACK = protocol.PT_DATA_ACK
PT_FLOOR_REQ = protocol.PT_FLOOR_REQ
PT_FLOOR_GRANT = protocol.PT_FLOOR_GRANT
EOF_BIT = protocol.EOF_BIT
SEQ_MASK = protocol.SEQ_MASK
SEQ_MODULO = protocol.SEQ_MODULO
SESSION_ID_NONE = protocol.SESSION_ID_NONE
CONNECT_FORMAT_MAGIC = protocol.CONNECT_FORMAT_MAGIC
CONNECT_FORMAT_VERSION = protocol.CONNECT_FORMAT_VERSION
_CONTROL_PLANE_TYPES = protocol.CONTROL_PLANE_TYPES
_DATA_PLANE_TYPES = protocol.DATA_PLANE_TYPES
_AIR_HEADER_LEN = protocol.AIR_HEADER_LEN
_PTYPE_NAMES = protocol.PTYPE_NAMES
_PTYPES_BY_NAME = {name: ptype for ptype, name in _PTYPE_NAMES.items()}
_air_inline_length = protocol.air_inline_length
_encode_air_header = protocol.encode_air_header
_decode_air_header = protocol.decode_air_header
_valid_air_shape = protocol.valid_air_shape
_seq_ahead = protocol.seq_ahead
_ptype_name = protocol.ptype_name
_connection_envelope = protocol.connection_envelope
_decode_connection_envelope = protocol.decode_connection_envelope
_call_bytes = protocol.call_bytes
_take_call = protocol.take_call
_decode_call_pair = protocol.decode_call_pair
_encode_call_and_modes = protocol.encode_call_and_modes
_decode_call_and_modes = protocol.decode_call_and_modes
_encode_connect_ack = protocol.encode_connect_ack
_decode_connect_ack = protocol.decode_connect_ack
_negotiate_mode = protocol.negotiate_mode

logger = logging.getLogger(__name__)


# Which end may originate PT_DATA right now. Real half-duplex ARQ modems
# (PACTOR, VARA, WINMOR/Ardop) call these roles ISS (Information Sending
# Station) and IRS (Information Receiving Station) and bake the distinction
# into connection state rather than leaving it to the application layer:
# without it, nothing stops both ends of this stop-and-wait link from
# deciding to key up DATA at the same moment whenever both happen to have
# outbound bytes queued at once -- on real RF neither transmission is heard
# cleanly, and with no jitter between ARQ retries the collision tends to
# repeat on the next attempt too.
#
# Assigned once at connect time -- the caller starts as ISS, the listener as
# IRS (see connect()/listen_once()) -- and handed over on request via
# PT_FLOOR_REQ/PT_FLOOR_GRANT (see _acquire_floor/_handle_floor_req).
# send_message() requires ISS and acquires it first if this station is IRS;
# recv_message() is where an IRS's request for it is answered, since that is
# the only place a station that currently holds the floor is listening for
# anything while it has nothing of its own in flight.

# The complement: the only two types that ever ride self.tx_profile. A
# decoded frame of one of these is therefore direct evidence of the profile
# the peer is actually transmitting at, which is what makes rx_profile
# self-correcting -- see _confirm_rx_profile. A decoded control-plane frame
# says nothing of the sort, since it would have gone out at the registry's
# control mode whatever either station had negotiated.
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
# Carrying both costs one byte of airtime and makes every ACK say exactly
# which frame it answers and what it accomplished.

# CHUNK_SIZE (payload bytes per DATA frame -- kept small so a single
# real-hardware bit error, observed near the tail of longer frames, only
# costs a short retransmit instead of derailing a large chunk) and the
# frame-airtime-derived ACK timeout both depend on the active mode -- see
# Link._apply_tx_profile/_apply_rx_profile, which compute them per instance
# instead of as module constants.
#
# The retry budget itself moved to whale/policy.py: how many times to try
# again is a bet about the channel, not a protocol fact. See ChannelPolicy
# and _channel_value below for what this name still does here.
MAX_RETRIES = FM.max_retries

#: `_send_chunk_with_arq` returning this means "no ACK, and the step-down it
#: just took landed on a mode whose chunk_size cannot carry the chunk in
#: hand". A lower rung can have a smaller chunk size. The chunk was never
#: ACKed, so nothing on the
#: receiver depends on its size: the sender re-cuts it at the new mode's
#: chunk_size and retries under the same sequence number.
_RESIZE = object()
# The one byte of session identity in PT_CONNECT/PT_CONNECT_ACK. See the
# module docstring for why it is on air at all. 0 is reserved for "not
# stated" so a body that decoded short reads as unknown rather than as
# session zero; _new_session_id never returns it.


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
# from its peer before tearing the session down. Moved, with the bench
# measurement that produced it, to ChannelPolicy.inactivity_timeout -- see
# _channel_value below for what this name still does here.
INACTIVITY_TIMEOUT = FM.inactivity_timeout

# Dead air before a reply, measured from the end of the peer's frame. Now
# ChannelPolicy.tx_turnaround_delay -- see _channel_value below.
TX_TURNAROUND_DELAY = FM.tx_turnaround_delay

# How much older than the turnaround itself an anchor may be and still be
# believed. Beyond that it is not evidence about when the peer stopped
# talking -- it only says when we last managed to follow it, which is a
# different claim. A retransmit after an ACK timeout is the case that
# matters: the anchor left over from some earlier frame would otherwise
# report that the channel went quiet long ago and let us key straight over
# a peer that is still talking.
ANCHOR_AGE_SLACK = DECODE_POLL_INTERVAL


# Mid-session emergency fallback threshold. Statistical mode selection lives
# on ChannelPolicy; this name remains as the VHF value for diagnostics and
# bench scripts.
STEP_DOWN_AFTER_ATTEMPTS = FM.step_down_after_attempts

# The module names above are no longer what the Link reads -- it reads
# self.policy -- but they are not vestigial either. The test suite and the
# bench scripts reach in and reassign them on a *live* station to make a
# policy number bite in seconds instead of minutes (link.INACTIVITY_TIMEOUT
# = 0.3 in test_link_recovery, link.TX_TURNAROUND_DELAY = 0.02 in the test
# harness), which is the only way to exercise a timeout whose real value is
# measured in minutes of air time. Constructing a whole policy for that
# would work, but it cannot reach a station that already exists.
#
# So each name stays as a live override: reassigning it to something other
# than the VHF value wins over whatever policy the Link holds. Leaving it
# alone -- the normal case, and the only case in production -- means the
# policy decides, so a station running HF is not silently given VHF
# numbers.
_CHANNEL_OVERRIDES = {
    "tx_turnaround_delay": "TX_TURNAROUND_DELAY",
    "inactivity_timeout": "INACTIVITY_TIMEOUT",
    "max_retries": "MAX_RETRIES",
    "step_down_after_attempts": "STEP_DOWN_AFTER_ATTEMPTS",
}


def _channel_value(policy, field):
    """`policy.<field>`, unless the module-level alias has been reassigned."""
    override = globals()[_CHANNEL_OVERRIDES[field]]
    if override != getattr(FM, field):
        return override
    return getattr(policy, field)


# Rough control-frame payload size used to size the control-plane ACK
# timeout (callsigns + mode list all comfortably fit) -- not a hard limit.
_CONTROL_FRAME_LEN_ESTIMATE = 32

# -- test affordances --------------------------------------------------
#
# Three environment-gated hooks, all off by default, all no-ops unless the
# variable is set. They exist because the failures this module now handles
# cannot otherwise be produced on demand over a real radio link.
#
#   WHALE_DROP_PTYPE   comma-separated packet type names (DATA_ACK,
#                      CONNECT_ACK, DATA_ACK, ...) or numeric ids, whose
#                      transmission is suppressed.
#   WHALE_DROP_NTH     which occurrences of each to suppress: comma-
#                      separated 1-based ordinals, or "all". Default "1".
#   WHALE_FORCE_MODE   mode_id this station proposes at connect time (as
#                      caller) or picks for its own TX (as listener),
#                      overriding whatever mode_history remembers. Any id
#                      in this station's own registry, extra waveforms
#                      included; an unsupported id is ignored with a warning.
#   WHALE_MODE_STEP_SCRIPT
#                      comma-separated "<n>:<up|down>": after the nth
#                      ACKed chunk of a session, take that mode step
#                      instead of whatever _maybe_adapt would have decided.
#
# Why suppression rather than a dropped frame. A real channel cannot be
# told to lose a chosen frame, and waiting for it to lose the right one is
# not a test. From the peer's side a frame that was never sent is
# indistinguishable from one that was sent and lost, so suppressing the
# transmission reproduces the failure. The software recovery tests drive
# the same hook, so the bench and the suite exercise one mechanism rather
# than two that have to be kept in agreement.
#
# WHALE_FORCE_MODE picks the starting profile; WHALE_MODE_STEP_SCRIPT makes
# a local DATA-mode step occur at a repeatable chunk boundary.


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


def _forced_mode_id(env=None, supported_ids=None):
    """The mode_id WHALE_FORCE_MODE pins this station's own TX to, or None.

    supported_ids is the station's own mode registry, not the built-in AFSK
    profile table: a station carrying an extra waveform must be
    able to pin itself to it. An id this station cannot transmit is ignored,
    but loudly -- a silently dropped override looks exactly like a bench run
    that never set the variable.
    """
    env = os.environ if env is None else env
    raw = (env.get("WHALE_FORCE_MODE") or "").strip()
    if not raw:
        return None
    mode_id = int(raw, 0)
    if supported_ids is not None and mode_id not in supported_ids:
        logger.warning("WHALE_FORCE_MODE=%s is not a mode this station supports (%s) -- ignored",
                       raw, ", ".join(str(i) for i in sorted(supported_ids)))
        return None
    return mode_id


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


class LinkError(Exception):
    pass


class Link(_ReceiverMixin, _AdaptationMixin):
    """Owns one radio transport and one session's worth of protocol state.

    All of connect()/send()/disconnect() are blocking and meant to be called
    from a single worker thread per station (see vara_server.py) -- the
    protocol is stop-and-wait, so there is never more than one thing in
    flight and nothing here needs to be reentrant.
    """

    def __init__(self, transport, mycall, on_event=None, mode_history_store=None,
                 mode_registry=None, policy=FM):
        self.transport = transport
        # What this station assumes about the channel it is on -- retry
        # budget, timeouts, keying length, how eagerly it speeds up. See
        # whale/policy.py. Nothing here is negotiated or on air, so two
        # stations running different policies interoperate.
        self.policy = policy
        if mode_registry is None:
            # Which waveforms suit this channel is the policy's call (see
            # whale/policy.py's mode_ladder), and the useful-frame budget
            # sizes every CPFSK rung's chunk, so a policy that keys longer
            # has to be threaded into the ladder rather than merely
            # remembered.
            mode_registry = self.policy.mode_ladder(
                self.policy.max_useful_frame_seconds)
        self.modes = mode_registry
        self.mycall = mycall
        self.peer_call = None
        self.peer_supported_modes = set()
        self.state = "IDLE"
        self.role = None  # "ISS" or "IRS" once CONNECTED -- see the constants above
        self.on_event = on_event or (lambda name, **kw: None)
        self.mode_history = {} if mode_history_store is None else mode_history_store

        # Control-plane frames always use the registry's control mode (see
        # _tx_packet), so this timeout is fixed for the life of the Link.
        self.control_ack_timeout = (self.modes.control.airtime(_CONTROL_FRAME_LEN_ESTIMATE)
                                    + self.policy.ack_timeout_slack)

        # self.tx_profile / self.rx_profile are the *negotiated data*
        # profiles for each direction -- only meaningful once CONNECTED.
        # They're independent: this station's TX quality to its peer and
        # the reverse leg can and do differ on real hardware, so each side
        # is negotiated and adapted separately instead of sharing one
        # profile. Both start at the control mode as a harmless default.
        self.tx_profile = self.modes.control
        self.rx_profile = self.modes.control
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
        # A station asking for the floor may receive the current ISS's
        # message before its request can be granted.  send_message() has to
        # service and ACK those frames to let the ISS finish; retain any
        # completed message here for the application's next recv_message().
        self._pending_messages = queue.Queue()
        self._partial_rx_buf = None  # in-progress recv_message() reassembly, see recv_message()
        self._tx_seq = 0
        self._rx_expect_seq = 0
        self._init_adaptation()
        # Qualification counters are observational and do not affect protocol state.
        self.qualification_metrics = {
            "data_attempts": 0, "retransmissions": 0,
            "ack_timeouts": 0, "duplicate_data": 0, "mode_changes": []}
        # Session identity, and the ack that established it. Both are what
        # make a retried PT_CONNECT answerable after listen_once has
        # returned -- see _answer_duplicate_connect.
        self._session_id = SESSION_ID_NONE
        self._connect_ack_body = None
        self._init_receiver()

    def start(self):
        self._start_receiver()

    def stop(self):
        """End the link and put the radio down.

        Stopping the receiver is only half of going off the air: the link
        owns the transport, so it is the link that closes it, and closing is
        what un-keys. Before, every shutdown path stopped the decode loop and
        left PTT exactly as it found it -- which, on a teardown that happened
        mid-transmission, was keyed.
        """
        self._stop_receiver()
        self.transport.close()

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
        while not self._pending_messages.empty():
            try:
                self._pending_messages.get_nowait()
            except queue.Empty:
                break
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
        -- they always use the registry's control mode regardless of this."""
        old = self.tx_profile
        self.tx_profile = profile
        if old is not profile and self.state == "CONNECTED":
            self.qualification_metrics["mode_changes"].append({
                "direction": "tx", "from": old.name, "to": profile.name})
        self._recompute_timings()

    def _apply_rx_profile(self, profile, fallback=None):
        """Sets the profile this station expects the *peer's* transmissions
        at -- i.e. the peer's own tx_profile, as far as this station knows
        it. Used by the decode loop and (indirectly) by the ACK timeout.

        `fallback` is retained for callers predating all-mode receive; normal
        connected operation now searches every mutually supported mode."""
        self.rx_profile = profile
        self._rx_profile_fallback = fallback if fallback is not profile else None
        self._recompute_timings()

    def _confirm_rx_profile(self, profile):
        """Takes a decoded data-plane frame as ground truth about what the
        peer is transmitting at, correcting rx_profile if they disagree.

        rx_profile is only a belief about the peer's tx_profile; a frame that
        actually decoded is not a belief.

        Only PT_DATA counts (_DATA_PLANE_TYPES). DATA_ACK and all other
        controls are robust-header transmissions, so they say nothing about
        the peer's negotiated DATA body mode."""
        if profile is self.rx_profile:
            self._rx_profile_fallback = None
            return
        logger.info("[%s] peer is transmitting at %s, not %s -- adopting what decoded",
                    self.mycall, profile.name, self.rx_profile.name)
        self._apply_rx_profile(profile)

    def _channel(self, field):
        """This station's value for one ChannelPolicy field.

        Goes through the module-level override hook rather than reading
        self.policy directly -- see _CHANNEL_OVERRIDES.
        """
        return _channel_value(self.policy, field)

    def _recompute_timings(self):
        # Everything here is a function of the two negotiated profiles, and
        # the two legs can run at different baud, so each is accounted for
        # separately rather than doubling one.
        #
        # Worst-case round trip for one DATA/DATA_ACK exchange: our DATA
        # frame out at tx_profile, then the peer's (tiny) ACK back at
        # rx_profile, with a turnaround at each end.
        tx_airtime = self.tx_profile.airtime(_AIR_HEADER_LEN + self.tx_profile.chunk_size)
        # The two sequence bytes are inline; the decoded mode is the ACK's
        # one-byte control-mode body.
        ack_airtime = self.modes.control.airtime(_AIR_HEADER_LEN + 1)
        self.data_ack_timeout = (tx_airtime + ack_airtime
                                 + 2 * self._channel("tx_turnaround_delay")
                                 + self.policy.ack_timeout_slack)

        # How much recent audio a poll that found nothing must leave alone
        # (see _prune_stale) -- enough that the longest frame either
        # candidate profile could be part-way through is never cut in half.
        self._rx_keep_seconds = (max(
            p.airtime(_AIR_HEADER_LEN + p.chunk_size) for p in self.modes.modes) + 1.0)

    def _await_turnaround(self):
        """Blocks until it is safe to key up over the peer.

        A reply sent essentially back-to-back with the frame it's replying
        to reaches the peer garbled or not at all on this rig: the peer is
        still transmitting or holding its carrier, and
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
        delay = self._channel("tx_turnaround_delay")
        anchor = self._peer_unkeyed_at
        observed_at = self._peer_unkeyed_observed_at
        self._peer_unkeyed_at = None
        self._peer_unkeyed_observed_at = None
        if delay <= 0:
            return
        now = time.monotonic()
        observation_is_fresh = (observed_at is not None
                                and now - observed_at <= delay + ANCHOR_AGE_SLACK)
        if anchor is None or (now - anchor > delay + ANCHOR_AGE_SLACK
                              and not observation_is_fresh):
            time.sleep(delay)
            return
        remaining = anchor + delay - now
        if remaining > 0:
            time.sleep(remaining)
        logger.debug("[%s] keying %.0f ms after the peer's frame ended", self.mycall,
                     (time.monotonic() - anchor) * 1000.0)

    def _tx_packet(self, ptype: int, body: bytes):
        """Keys one complete packet in its control or negotiated waveform."""
        self._await_turnaround()
        profile = self.modes.control if ptype in _CONTROL_PLANE_TYPES else self.tx_profile
        if self.tx_suppress.should_drop(ptype):
            # Test affordance only (WHALE_DROP_PTYPE) -- see _TxSuppressor.
            # Everything up to this line has already happened, turnaround
            # included, so the caller's own timing is exactly what it would
            # have been; the frame simply never reaches the air.
            logger.warning("[%s] SUPPRESSING TX %s at %s (%d body byte(s)) -- WHALE_DROP_PTYPE",
                           self.mycall, _ptype_name(ptype), profile.name, len(body))
            return
        header, remainder = _encode_air_header(ptype, profile.mode_id, body)
        if not _valid_air_shape(ptype, profile, len(remainder),
                                body[:_air_inline_length(ptype)],
                                self.modes.control.mode_id):
            raise ValueError(f"invalid {_ptype_name(ptype)} body/mode for air header")
        audio = profile.encode(header + remainder)
        keyed = self.transport.send(audio)
        # Both numbers, because the gap between them is the PTT/settling
        # overhead this frame actually paid -- the thing to watch if air
        # time regresses. See scripts/sweep_ptt_timing.py.
        logger.info("[%s] TX %s at %s (%d body byte(s), %.2fs audio, %.2fs keyed)",
                    self.mycall, _ptype_name(ptype), profile.name, len(body),
                    len(audio) / profile.tx_sample_rate, keyed)

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
        if self.state not in ("LISTENING", "CONNECTED") or self._connect_ack_body is None:
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
        return (time.monotonic() - self._last_peer_frame_at
                > self._channel("inactivity_timeout"))

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
                       self.mycall, self.peer_call, self._channel("inactivity_timeout"))
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

    def connect(self, dst_call, timeout_per_try=None, retries=None):
        # Defaults resolved here rather than in the signature: both come
        # from this station's channel policy, which the class does not have
        # at def time.
        timeout_per_try = self.control_ack_timeout if timeout_per_try is None else timeout_per_try
        retries = self._channel("max_retries") if retries is None else retries
        self._drain_packets()
        self._rx_frequency_hint_hz = None
        self.state = "CONNECTING"
        own_supported = list(self.modes.supported_ids)
        # Not `forced or history`: a forced mode_id can be falsy (0) and is
        # still a real, deliberate override, not "no override" -- so this
        # has to check `is None`, not truthiness.
        proposed_id = _forced_mode_id(supported_ids=own_supported)
        if proposed_id is None:
            proposed_id = mode_history.last_good_mode(self.mode_history, self.mycall, dst_call)
        if proposed_id is None or proposed_id not in own_supported:
            proposed_id = self.modes.control.mode_id  # no history with this peer -- start slow
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
                self._apply_tx_profile(self.modes.resolve(accepted_id))
                self._apply_rx_profile(self.modes.resolve(peer_tx_id))
                self.state = "CONNECTED"
                self.role = "ISS"  # the caller starts holding the floor -- see PT_FLOOR_REQ above
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
        self._rx_frequency_hint_hz = None
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
        own_supported = list(self.modes.supported_ids)
        # negotiated_id: whether we accept the caller's proposed rate for
        # its (src->mycall) direction -- becomes our rx expectation.
        negotiated_id = _negotiate_mode(
            own_supported, proposed_id, fallback_id=self.modes.control.mode_id)
        # own_tx_id: independently, what rate *we* should use transmitting
        # back (mycall->src) -- our own history for this peer, downgraded
        # to the control mode if the caller hasn't told us it supports that
        # mode. The two legs need not match: this rig's two directions can
        # measure different SNR.
        own_tx_id = _forced_mode_id(supported_ids=own_supported)  # mode_id 0 is falsy, so not `or`
        if own_tx_id is None:
            own_tx_id = mode_history.last_good_mode(self.mode_history, self.mycall, src)
        if (own_tx_id is None or own_tx_id not in own_supported
                or own_tx_id not in peer_supported):
            own_tx_id = self.modes.control.mode_id
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
        self._apply_rx_profile(self.modes.resolve(negotiated_id))
        self._apply_tx_profile(self.modes.resolve(own_tx_id))
        self.state = "CONNECTED"
        self.role = "IRS"  # the listener starts waiting for the floor -- see PT_FLOOR_REQ above
        self._reset_sequence_state()
        self.on_event("CONNECTED", mycall=self.mycall, peer=self.peer_call)
        logger.info("[%s] accepted connection from %s: tx=%s rx=%s", self.mycall, src,
                    self.tx_profile.name, self.rx_profile.name)
        return self.peer_call

    # -- floor (ISS/IRS) ---------------------------------------------------

    def _acquire_floor(self, retries=None):
        """Blocks until this station holds ISS -- the right to originate
        PT_DATA -- by asking whichever end currently holds it to hand it
        over. A no-op if we already have it.

        Retried the same way a DATA chunk is: the peer only answers
        PT_FLOOR_REQ from inside recv_message(), i.e. while it isn't itself
        mid-send (see recv_message and _handle_floor_req), so a request that
        lands while the peer is busy sending its own message is silently
        dropped by its _wait_packet.
        The retry after this attempt's timeout is what gets through once the
        peer goes back to polling for incoming work.

        Two rules keep the request from keying over the peer. While a
        message is part-way in, nothing is requested: the ISS would drop it
        and would be keying its next chunk at the same moment. And each
        request waits long enough to hear a whole DATA retransmission, since
        that is how the ISS answers a request that arrives while one of its
        chunks is unacknowledged (see _send_chunk_with_arq). Any DATA from
        the peer restarts the attempt count: it is still sending, not gone."""
        if self.role == "ISS":
            return True
        retries = self._channel("max_retries") if retries is None else retries
        attempt = 0
        while attempt < retries:
            if self._partial_rx_buf:
                if self._peer_is_stale():
                    self._abandon_stale_session()
                    raise LinkError("peer went silent mid-message while we waited for the floor")
                if self._take_peer_data_for_floor(self.control_ack_timeout) is True:
                    return True
                continue
            attempt += 1
            logger.info("[%s] requesting the floor (attempt %d/%d)", self.mycall, attempt, retries)
            self.on_event("PTT", on=True)
            self._tx_packet(PT_FLOOR_REQ, b"")
            self.on_event("PTT", off=True)
            # Jitter so a request cannot lock step with the peer's own retry
            # period and land on every one of its retransmissions.
            wait = self._floor_grant_timeout() + random.uniform(
                0.0, self.modes.control.airtime(_AIR_HEADER_LEN))
            granted = self._take_peer_data_for_floor(wait)
            if granted is True:
                return True
            if granted == "data":
                attempt = 0
        return False

    def _take_peer_data_for_floor(self, timeout):
        """Waits up to `timeout` for FLOOR_GRANT, DATA or DISC while asking
        for the floor. Returns True once granted, "data" after answering one
        DATA frame, or None on timeout."""
        got = self._wait_packet({PT_FLOOR_GRANT, PT_DATA, PT_DISC}, timeout)
        if got is None:
            return None
        ptype, body = got
        if ptype == PT_DISC:
            self._handle_peer_disc()
            raise LinkError("peer disconnected while we were requesting the floor")
        if ptype == PT_DATA:
            # The peer still owns the floor and was already sending.
            # Keep its ARQ moving instead of deadlocking with both
            # application pumps blocked in send_message().
            message = self._handle_data(body)
            if message is not None:
                self._pending_messages.put(message)
            return "data"
        self.role = "ISS"
        logger.info("[%s] floor granted, now ISS", self.mycall)
        return True

    def _floor_grant_timeout(self):
        """How long one FLOOR_REQ waits: a grant, or the longest DATA frame
        the ISS may retransmit in answer, plus the usual turnaround and
        slack."""
        longest_data = max((p.airtime(_AIR_HEADER_LEN + p.chunk_size)
                            for p in self.modes.modes
                            if p.mode_id in self.peer_supported_modes), default=0.0)
        return max(self.control_ack_timeout,
                   longest_data + self._channel("tx_turnaround_delay")
                   + self.policy.ack_timeout_slack)

    def _handle_floor_req(self):
        """The peer (currently IRS) wants to become ISS. We only ever see
        this from inside recv_message(), i.e. while nothing of our own is in
        flight, so there is nothing to finish first -- hand the floor over
        unconditionally. Granting again when we're already IRS is harmless:
        it is exactly what happens when our own PT_FLOOR_GRANT to an earlier
        request was lost and the peer retried it (see _acquire_floor)."""
        self.role = "IRS"
        self.on_event("PTT", on=True)
        self._tx_packet(PT_FLOOR_GRANT, b"")
        self.on_event("PTT", off=True)
        logger.info("[%s] granted floor to peer, now IRS", self.mycall)

    # -- data transfer ------------------------------------------------------

    def send_message(self, data: bytes):
        """Sends `data` as one or more ARQ'd DATA frames. Blocks until every
        chunk is acknowledged or raises LinkError.

        Chunks are cut one at a time, immediately before each is sent, rather
        than pre-split up front: _maybe_adapt() can step tx_profile mid-message
        and different modes have different chunk_size, so a message
        pre-split at the starting profile would keep sending undersized
        frames for the rest of the transfer even after stepping up."""
        if self.state != "CONNECTED":
            raise LinkError("not connected")
        if self.role != "ISS" and not self._acquire_floor():
            raise LinkError("could not acquire the floor to send")
        sent = 0
        offset = 0
        while True:
            chunk = data[offset:offset + self.tx_profile.chunk_size]
            is_last = offset + len(chunk) >= len(data)
            starting_profile = self.tx_profile
            attempts = self._send_chunk_with_arq(self._tx_seq, chunk, is_last)
            if attempts is _RESIZE:
                # A step-down mid-chunk left this chunk too big for the mode
                # now in force. It was never ACKed and the sequence number
                # has not advanced, so re-cutting it smaller and sending it
                # again under the same seq is indistinguishable, to the
                # receiver, from a chunk that was always that size.
                continue
            offset += len(chunk)
            sent += 1
            if attempts is None:
                raise LinkError(f"no ACK for chunk {sent} ({offset}/{len(data)} bytes) "
                                f"after {self._channel('max_retries')} tries")
            self._tx_seq = (self._tx_seq + 1) % SEQ_MODULO
            # Always retain the ACK as evidence for the mode that ultimately
            # delivered the chunk. If the retry loop already stepped down,
            # suppress another decision at this same chunk boundary.
            self._maybe_adapt(attempts, allow_change=self.tx_profile is starting_profile)
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

        DATA_ACK confirms both the sequence and the mode the IRS decoded.
        After repeated silence the sender steps down and retries this same
        chunk; receivers search every mutually supported mode."""
        max_retries = self._channel("max_retries")
        for attempt in range(1, max_retries + 1):
            self.qualification_metrics["data_attempts"] += 1
            if attempt > 1:
                self.qualification_metrics["retransmissions"] += 1
            body = bytes([seq | (EOF_BIT if is_eof else 0)]) + chunk
            self.on_event("BITRATE", direction="TX",
                          mode_id=self.tx_profile.mode_id,
                          bits_per_second=net_bits_per_second(self.tx_profile))
            self.on_event("PTT", on=True)
            self._tx_packet(PT_DATA, body)
            self.on_event("PTT", off=True)
            deadline = time.monotonic() + self.data_ack_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                got = self._wait_packet({PT_DATA_ACK, PT_DISC, PT_FLOOR_REQ}, remaining)
                if got is None:
                    break
                ptype, body_in = got
                if ptype == PT_DISC:
                    self._handle_peer_disc()
                    raise LinkError("peer disconnected mid-transfer")
                if ptype == PT_FLOOR_REQ:
                    # The IRS asks only when no message of ours is part-way
                    # in and it is listening now, so this frame or its ACK was
                    # lost. Waiting out the timeout would only let its next
                    # request key over our retransmission.
                    logger.info("[%s] floor requested while DATA seq=0x%02x is unacknowledged "
                                "-- retransmitting now", self.mycall, seq)
                    break
                if len(body_in) != 3:
                    logger.info("[%s] ignoring malformed DATA_ACK for seq=0x%02x (%d bytes)",
                                self.mycall, seq, len(body_in))
                    continue
                answered, expects = body_in[0] & SEQ_MASK, body_in[1] & SEQ_MASK
                received_mode_id = body_in[2]
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
                    if received_mode_id != self.tx_profile.mode_id:
                        logger.warning("[%s] ignoring ACK reporting mode %d; transmitting at %s",
                                       self.mycall, received_mode_id, self.tx_profile.name)
                        continue
                    logger.info("[%s] DATA seq=0x%02x acked after %d attempt(s) at %s",
                                self.mycall, seq, attempt, self.tx_profile.name)
                    return attempt
                # The peer decoded this very frame and still did not advance
                # past it, so the two ends disagree about where the sequence
                # stands. Retransmitting cannot repair that; let the attempts
                # run out and surface it as a LinkError instead of looping.
                logger.warning("[%s] peer answered seq 0x%02x but expects 0x%02x -- sequence desync",
                               self.mycall, answered, expects)
            logger.warning("DATA seq=0x%02x: no ACK, retry %d/%d", seq, attempt, max_retries)
            self.qualification_metrics["ack_timeouts"] += 1
            self._record_mode_attempt(self.tx_profile, False)
            self._consecutive_tx_failures += 1
            if self._consecutive_tx_failures >= self._channel("step_down_after_attempts"):
                self._adaptive_step(-1, "consecutive DATA_ACK timeouts")
                if len(chunk) > self.tx_profile.chunk_size:
                    logger.info("[%s] stepped down to %s mid-chunk; re-cutting seq=0x%02x "
                                "(%d bytes > %d chunk_size)", self.mycall,
                                self.tx_profile.name, seq, len(chunk),
                                self.tx_profile.chunk_size)
                    return _RESIZE
        return None

    # -- mid-session speed adaptation ---------------------------------------

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
        try:
            return self._pending_messages.get_nowait()
        except queue.Empty:
            pass
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
            got = self._wait_packet({PT_DATA, PT_DISC, PT_FLOOR_REQ},
                                     remaining if remaining is not None else 1e9)
            if got is None:
                return None
            ptype, body = got
            if ptype == PT_FLOOR_REQ:
                self._handle_floor_req()
                continue  # we're IRS now; wait for the actual DATA from the new ISS
            if ptype == PT_DISC:
                self._handle_peer_disc()
                return None
            message = self._handle_data(body)
            if message is not None:
                return message

    def _handle_data(self, body):
        """Consumes and acknowledges one DATA body, returning a completed
        message or None.  Shared by recv_message() and floor acquisition so
        an IRS can continue receiving while its application wants to send."""
        if len(body) < 1:
            logger.info("[%s] ignoring empty DATA body", self.mycall)
            return None
        flags, chunk = body[0], body[1:]
        seq = flags & SEQ_MASK
        message = None
        if self._partial_rx_buf is None:
            self._partial_rx_buf = bytearray()
        if seq == self._rx_expect_seq:
            self._partial_rx_buf += chunk
            self._rx_expect_seq = (seq + 1) % SEQ_MODULO
            if flags & EOF_BIT:
                message = bytes(self._partial_rx_buf)
                self._partial_rx_buf = bytearray()
        else:
            # A duplicate retransmit: the sender missed our ACK. Already
            self.qualification_metrics["duplicate_data"] += 1
            # delivered, so drop the payload -- but still ack below.
            logger.info("[%s] DATA seq=0x%02x already have (expecting 0x%02x) -- dropping",
                        self.mycall, seq, self._rx_expect_seq)
        # ACK every DATA we see, duplicates included. The ACK names both the
        # answered frame and the sequence wanted next so a stale duplicate
        # cannot be mistaken for an answer to a later frame.
        self.on_event("PTT", on=True)
        if message is not None:
            # Hand the reassembled message up now rather than after the ACK
            # keying finishes: it is already decoded and the peer is already
            # owed it, so holding it for the length of a transmission only
            # adds latency. Callers that consume the return value still get
            # it -- see recv_message.
            self.on_event("RX_MESSAGE", data=message)
        self._tx_packet(PT_DATA_ACK,
                        bytes([seq, self._rx_expect_seq, self.rx_profile.mode_id]))
        self.on_event("PTT", off=True)
        return message

    # -- teardown ------------------------------------------------------

    def _forget_session(self):
        """Drops the identity of the session that has just ended, so a
        PT_CONNECT arriving afterwards is treated as a new call rather than
        re-answered as a retry of a session that no longer exists."""
        self._session_id = SESSION_ID_NONE
        self._rx_frequency_hint_hz = None
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
            return True
        if self.peer_call is not None:
            mode_history.record_good_mode(self.mode_history, self.mycall, self.peer_call, self.tx_profile.mode_id)
        acknowledged = False
        for attempt in range(1, retries + 1):
            self.on_event("PTT", on=True)
            self._tx_packet(PT_DISC, b"")
            self.on_event("PTT", off=True)
            got = self._wait_packet({PT_DISC_ACK, PT_DISC}, timeout)
            if got is not None:
                acknowledged = True
                break
        self.state = "IDLE"
        self.peer_call = None
        self._forget_session()
        self.on_event("DISCONNECTED")
        return acknowledged
