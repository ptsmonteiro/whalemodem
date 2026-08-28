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

  - The fixed turnaround sleep is removed. The calibration handshake measures
    effective clipping on replies after a real direction change, and the
    resulting per-session head pad absorbs that loss.
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
"""

import logging
import os
import queue
import random
import threading
import time

import numpy as np

from whale import afsk, framing, mode_history

logger = logging.getLogger(__name__)

PT_CONNECT = 0x01
PT_CONNECT_ACK = 0x02
PT_DISC = 0x03
PT_DISC_ACK = 0x04
PT_DATA = 0x05
PT_DATA_ACK = 0x06         # body: [answered_seq, next_expected_seq, received_mode_id]
# 0x07 and 0x08 were MODE_REQ/MODE_ACK in version 1. They are reserved so an
# old control exchange is rejected rather than reinterpreted as another type.
PT_FLOOR_REQ = 0x09        # IRS asking to become ISS -- see _acquire_floor
PT_FLOOR_GRANT = 0x0A      # ISS handing the floor to the peer -- see _handle_floor_req
PT_TIMING_ACK = 0x0B
PT_TIMING_CONFIRM = 0x0C

# Packet types whose bodies are small and must survive even when the
# negotiated data profile is struggling -- these always go out on
# afsk.CONTROL_PROFILE (see _tx_packet), never on self.tx_profile. Only bulk
# only the DATA body ever rides the negotiated speed.
_CONTROL_PLANE_TYPES = {PT_CONNECT, PT_CONNECT_ACK, PT_DISC, PT_DISC_ACK, PT_DATA_ACK,
                         PT_FLOOR_REQ, PT_FLOOR_GRANT, PT_TIMING_ACK,
                         PT_TIMING_CONFIRM}

CONNECT_FORMAT_MAGIC = b"\xffWHL"
CONNECT_FORMAT_VERSION = 2
CALIBRATION_SECONDS = 1.0
HEAD_MIN_GUARD_SECONDS = 0.05
TAIL_MIN_GUARD_SECONDS = 0.03
TIMING_MARGIN = 0.20

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
# says nothing of the sort, since it would have gone out at
# afsk.CONTROL_PROFILE whatever either station had negotiated.
_DATA_PLANE_TYPES = {PT_DATA}

# Every keying starts with this fixed-size packet in the registry's robust
# control mode.  Its ordinary frame CRC is therefore a CRC over every field
# below, including the length and mode used to configure the following body
# decoder.  Up to two bytes live inline, making the common ACK and one-byte
# control packets header-only transmissions.
_AIR_HEADER_MAGIC = b"WH"
_AIR_HEADER_VERSION = 1
_AIR_HEADER_INLINE_BYTES = 2
_AIR_HEADER_LEN = framing.BOOTSTRAP_HEADER_BYTES


def _air_inline_length(ptype):
    if ptype == PT_DATA:
        return 1
    if ptype in (PT_CONNECT, PT_CONNECT_ACK, PT_TIMING_ACK,
                 PT_TIMING_CONFIRM):
        return 2
    if ptype == PT_DATA_ACK:
        return 2
    return 0


def _encode_air_header(ptype: int, body_mode_id: int, body: bytes):
    inline_count = _air_inline_length(ptype)
    inline = body[:inline_count]
    remainder = body[len(inline):]
    if len(remainder) > framing.MAX_PAYLOAD_BYTES:
        raise ValueError("packet body is too long")
    header = (_AIR_HEADER_MAGIC + bytes([_AIR_HEADER_VERSION, ptype, body_mode_id,
                                         len(inline)])
              + len(remainder).to_bytes(2, "big")
              + inline.ljust(_AIR_HEADER_INLINE_BYTES, b"\x00"))
    assert len(header) == _AIR_HEADER_LEN
    return header, remainder


def _decode_air_header(raw: bytes):
    if len(raw) != _AIR_HEADER_LEN or raw[:2] != _AIR_HEADER_MAGIC:
        return None
    if raw[2] != _AIR_HEADER_VERSION:
        return None
    inline_len = raw[5]
    if inline_len > _AIR_HEADER_INLINE_BYTES:
        return None
    if any(raw[8 + inline_len:10]):
        return None
    return raw[3], raw[4], int.from_bytes(raw[6:8], "big"), raw[8:8 + inline_len]


def _valid_air_shape(ptype, profile, body_len, inline, control_mode_id):
    """Semantic checks applied only after the header CRC has passed."""
    if ptype == PT_DATA:
        return len(inline) == 1 and body_len <= profile.chunk_size
    if profile.mode_id != control_mode_id:
        return False
    if ptype in (PT_CONNECT, PT_CONNECT_ACK):
        return len(inline) == 2 and 0 < body_len <= 128
    if ptype == PT_DATA_ACK:
        return len(inline) == 2 and body_len == 1
    if ptype == PT_TIMING_ACK:
        return len(inline) == 2 and body_len == 1
    if ptype == PT_TIMING_CONFIRM:
        return len(inline) == 2 and body_len == 1
    if ptype in (PT_DISC, PT_DISC_ACK, PT_FLOOR_REQ, PT_FLOOR_GRANT):
        return len(inline) == 0 and body_len == 0
    return False

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
# The full retry cycle remains the longest active-session silence; 150s is
# a little over 3x the measured 44.4s.
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

# Kept as a compatibility name for diagnostics/tests. No fixed dead-air delay
# is applied: replies begin after the decoder has observed the expected tail,
# and calibrated head audio absorbs the effective direction-change loss.
TX_TURNAROUND_DELAY = 0.0

# What the peer is still transmitting after the last bit we decoded:
# framing.TAIL_PAD_SECONDS of pad. This is the peer's setting and nothing on
# air tells us what it is, so this is a nominal figure for peers running the
# same build.
PEER_TRAILING_TRANSMISSION = framing.TAIL_PAD_SECONDS

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
    PT_FLOOR_REQ: "FLOOR_REQ", PT_FLOOR_GRANT: "FLOOR_GRANT",
    PT_TIMING_ACK: "TIMING_ACK", PT_TIMING_CONFIRM: "TIMING_CONFIRM",
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
#   WHALE_DROP_PTYPE   comma-separated packet type names (DATA_ACK,
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


def _connection_envelope(content):
    return CONNECT_FORMAT_MAGIC + bytes([CONNECT_FORMAT_VERSION]) + len(content).to_bytes(2, "big") + content


def _decode_connection_envelope(payload):
    if (len(payload) < 7 or payload[:4] != CONNECT_FORMAT_MAGIC
            or payload[4] != CONNECT_FORMAT_VERSION
            or int.from_bytes(payload[5:7], "big") != len(payload) - 7):
        raise ValueError("invalid connection body envelope")
    return memoryview(payload)[7:]


def _call_bytes(call):
    raw = call.encode("ascii")
    if not 1 <= len(raw) <= 15 or any(chr(c) not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in raw):
        raise ValueError("invalid callsign")
    return bytes([len(raw)]) + raw


def _take_call(content, offset):
    if offset >= len(content):
        raise ValueError("missing callsign")
    size = content[offset]
    end = offset + 1 + size
    if not 1 <= size <= 15 or end > len(content):
        raise ValueError("invalid callsign length")
    call = bytes(content[offset + 1:end]).decode("ascii")
    _call_bytes(call)
    return call, end


def _decode_call_pair(payload):
    try:
        content = _decode_connection_envelope(payload)
        src, offset = _take_call(content, 0)
        dst, _ = _take_call(content, offset)
        return src, dst
    except (ValueError, UnicodeError):
        return "", ""


def _encode_call_and_modes(a, b, supported_ids, extra_id, session_id=SESSION_ID_NONE):
    """CONNECT body: "a\\x00b\\x00" + one byte per supported mode_id + two
    trailing bytes -- the sender's proposed TX mode_id for the a->b
    direction, and the session identifier it has picked for this call (see
    the module docstring; SESSION_ID_NONE if it has none)."""
    modes = bytes(sorted(set(supported_ids)))
    content = (_call_bytes(a) + _call_bytes(b) + bytes([session_id, len(modes)])
               + modes + bytes([extra_id]))
    return _connection_envelope(content)


def _decode_call_and_modes(payload):
    content = _decode_connection_envelope(payload)
    a, offset = _take_call(content, 0)
    b, offset = _take_call(content, offset)
    if offset + 3 > len(content):
        raise ValueError("truncated CONNECT")
    session_id, count = content[offset], content[offset + 1]
    offset += 2
    if offset + count + 1 != len(content):
        raise ValueError("invalid CONNECT mode count")
    modes = list(content[offset:offset + count])
    return a, b, modes, content[-1], session_id


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
    modes = bytes(sorted(set(supported_ids)))
    content = (_call_bytes(a) + _call_bytes(b) + bytes([session_id, len(modes)]) + modes
               + bytes([accepted_tx_id, own_tx_id]))
    return _connection_envelope(content)


def _decode_connect_ack(payload):
    content = _decode_connection_envelope(payload)
    a, offset = _take_call(content, 0)
    b, offset = _take_call(content, offset)
    if offset + 4 > len(content):
        raise ValueError("truncated CONNECT_ACK")
    session_id, count = content[offset], content[offset + 1]
    offset += 2
    if offset + count + 2 != len(content):
        raise ValueError("invalid CONNECT_ACK mode count")
    modes = list(content[offset:offset + count])
    offset += count
    return a, b, modes, content[offset], content[offset + 1], session_id


def _encode_timing(session_id, head_received, tail_received):
    total = int(np.ceil(CALIBRATION_SECONDS * afsk.CONTROL_PROFILE.baud))

    def duration_byte(received):
        if not 0 <= received <= total:
            raise ValueError("invalid calibration measurement")
        return min(255, int(np.ceil(received * 255 / total)))

    return bytes([session_id, duration_byte(head_received), duration_byte(tail_received)])


def _decode_timing(body):
    if len(body) != 3:
        raise ValueError("invalid TIMING_ACK")
    return body[0], body[1], body[2]


def _derive_timing(head_duration_byte, tail_duration_byte, baud=None):
    if not (0 < head_duration_byte <= 255 and 0 < tail_duration_byte <= 255):
        raise ValueError("invalid calibration measurement")
    head_loss = CALIBRATION_SECONDS * (255 - head_duration_byte) / 255
    tail_loss = CALIBRATION_SECONDS * (255 - tail_duration_byte) / 255
    return (min(CALIBRATION_SECONDS, head_loss + max(HEAD_MIN_GUARD_SECONDS, head_loss * TIMING_MARGIN)),
            min(CALIBRATION_SECONDS, tail_loss + max(TAIL_MIN_GUARD_SECONDS, tail_loss * TIMING_MARGIN)))


def _negotiate_mode(own_supported_ids, proposed_id, fallback_id=None):
    """Listener's rule for picking a starting data profile: accept the
    caller's proposal if we can decode it, else fall back to the always-
    supported control profile."""
    if proposed_id in own_supported_ids:
        return proposed_id
    return afsk.CONTROL_PROFILE.mode_id if fallback_id is None else fallback_id


class LinkError(Exception):
    pass


class Link:
    """Owns one radio transport and one session's worth of protocol state.

    All of connect()/send()/disconnect() are blocking and meant to be called
    from a single worker thread per station (see vara_server.py) -- the
    protocol is stop-and-wait, so there is never more than one thing in
    flight and nothing here needs to be reentrant.
    """

    def __init__(self, transport, mycall, on_event=None, mode_history_store=None,
                 mode_registry=None):
        self.transport = transport
        self.modes = mode_registry or afsk.default_registry()
        self.mycall = mycall
        self.peer_call = None
        self.peer_supported_modes = set()
        self.state = "IDLE"
        self.role = None  # "ISS" or "IRS" once CONNECTED -- see the constants above
        self.on_event = on_event or (lambda name, **kw: None)
        self.mode_history = {} if mode_history_store is None else mode_history_store
        self._clean_streak = 0

        # Control-plane frames always use afsk.CONTROL_PROFILE (see
        # _tx_packet), so this timeout is fixed for the life of the Link.
        self.control_ack_timeout = self.modes.control.airtime(_CONTROL_FRAME_LEN_ESTIMATE) + 3.0

        # self.tx_profile / self.rx_profile are the *negotiated data*
        # profiles for each direction -- only meaningful once CONNECTED.
        # They're independent: this station's TX quality to its peer and
        # the reverse leg can and do differ on real hardware (see
        # whale/afsk.py's measured per-direction SNR numbers), so each side
        # is negotiated and adapted separately instead of sharing one
        # profile. Both start at CONTROL_PROFILE as a harmless default.
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
        self._rx_measurements = {}
        self._tx_head_seconds = CALIBRATION_SECONDS
        self._tx_tail_seconds = CALIBRATION_SECONDS
        self._rx_head_seconds = CALIBRATION_SECONDS
        self._rx_tail_seconds = CALIBRATION_SECONDS
        # A station asking for the floor may receive the current ISS's
        # message before its request can be granted.  send_message() has to
        # service and ACK those frames to let the ISS finish; retain any
        # completed message here for the application's next recv_message().
        self._pending_messages = queue.Queue()
        self._partial_rx_buf = None  # in-progress recv_message() reassembly, see recv_message()
        self._tx_seq = 0
        self._rx_expect_seq = 0
        self._acked_chunks = 0
        # Session identity, and the ack that established it. Both are what
        # make a retried PT_CONNECT answerable after listen_once has
        # returned -- see _answer_duplicate_connect.
        self._session_id = SESSION_ID_NONE
        self._connect_ack_body = None
        self._timing_confirm_body = None
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
        -- they always use afsk.CONTROL_PROFILE regardless of this."""
        self.tx_profile = profile
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

    def _recompute_timings(self):
        # Everything here is a function of the two negotiated profiles, and
        # the two legs can run at different baud, so each is accounted for
        # separately rather than doubling one.
        #
        # Worst-case round trip for one DATA/DATA_ACK exchange: our DATA
        # frame out at tx_profile, then the peer's (tiny) ACK back at
        # rx_profile, with a turnaround at each end.
        tx_airtime = self.tx_profile.airtime(_AIR_HEADER_LEN + self.tx_profile.chunk_size)
        # The two sequence bytes are inline; received_mode_id is the ACK's
        # one-byte control-mode body.
        ack_airtime = self.modes.control.airtime(_AIR_HEADER_LEN + 1)
        self.data_ack_timeout = (tx_airtime + ack_airtime
                                 + 2 * TX_TURNAROUND_DELAY + 3.0)

        # How much recent audio a poll that found nothing must leave alone
        # (see _prune_stale) -- enough that the longest frame either
        # candidate profile could be part-way through is never cut in half.
        self._rx_keep_seconds = (max(
            p.airtime(_AIR_HEADER_LEN + p.chunk_size) for p in self.modes.modes) + 1.0)

    def _candidate_decode_profiles(self):
        """Which afsk.Profile(s) an incoming frame might be using right
        now: control-plane traffic always uses CONTROL_PROFILE, and DATA
        traffic uses whatever self.rx_profile currently is (the peer's own
        tx_profile) -- try both since the decode loop can't otherwise tell
        which is arriving next.

        A sender may step down after silence, when it cannot notify us first.
        Therefore every mutually advertised data mode remains a candidate;
        the DATA frame itself is authoritative notification of a change."""
        candidates = [self.modes.control]
        data_profiles = [p for p in self.modes.modes
                         if p.mode_id in self.peer_supported_modes]
        for profile in (self.rx_profile, *data_profiles):
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
        reads far enough to hit a garbage length field, report a near-miss
        end_index of its own. Consuming on that would truncate the real
        frame before the other candidate ever gets the full thing to look
        at. So: if any candidate has a genuine sync lock (confidence over
        its own threshold) but hasn't seen enough samples yet for a verdict,
        this poll holds off consuming anything and just waits for more
        audio, rather than letting a different candidate's near-miss win."""
        results = [(profile, profile.decode(snap, head_seconds=self._rx_head_seconds,
                                             tail_seconds=self._rx_tail_seconds))
                   for profile in self._candidate_decode_profiles()]
        for profile, result in results:
            payload = result.get("payload")
            if payload is None:
                continue
            decoded = _decode_air_header(payload[:_AIR_HEADER_LEN])
            if decoded is None:
                continue
            ptype, mode_id, body_len, inline = decoded
            body_profile = self.modes.by_id.get(mode_id)
            remainder = payload[_AIR_HEADER_LEN:]
            if (body_profile is not profile or len(remainder) != body_len
                    or not _valid_air_shape(ptype, body_profile, body_len, inline,
                                            self.modes.control.mode_id)):
                continue
            end = result.get("end_index", len(snap))
            self.transport.consume_rx(end)
            self._finish_air_packet(ptype, inline + remainder, profile, snap, end,
                                    result)
            return True

        pending = [result for candidate, result in results
                   if result.get("confidence", 0) >= candidate.confidence_threshold
                   and "end_index" not in result]
        if pending:
            return False
        near_misses = [result for _, result in results if "end_index" in result]
        if near_misses:
            result = min(near_misses,
                         key=lambda item: item.get("sync_end_index", item["end_index"]))
            skip = result.get("sync_end_index", result["end_index"])
            self._capture_near_miss(snap, result.get("confidence", 0))
            self.transport.consume_rx(skip)
            return True
        if all(result.get("confidence", 0) < profile.confidence_threshold
               for profile, result in results):
            self._prune_stale(len(snap))
        return False

    def _finish_air_packet(self, ptype, body, profile, snap, end, decode_result):
        trailing = max(0, len(snap) - end)
        # CPFSK outer-pad measurement advances `end` through the observed
        # tail, so that boundary is already the peer's nominal unkeying time.
        # Other waveform codecs still end at the checked frame and retain the
        # legacy trailing-duration estimate.
        tail_already_observed = "tail_symbols_received" in decode_result
        remaining_tail = 0.0 if tail_already_observed else PEER_TRAILING_TRANSMISSION
        self._peer_unkeyed_at = (time.monotonic() - trailing / profile.sample_rate
                                 + remaining_tail)
        logger.info("[%s] decoded %s body at profile %s", self.mycall,
                    _ptype_name(ptype), profile.name)
        for side in ("head", "tail"):
            received = decode_result.get(f"{side}_symbols_received")
            if received is None:
                continue
            logger.info(
                "[%s] RX outer %s: observed %d adjacent symbols (%.1f ms)",
                self.mycall, side, received, received * 1000.0 / profile.baud,
            )
        self._rx_measurements[(ptype, body)] = {
            "head": decode_result.get("head_symbols_received"),
            "tail": decode_result.get("tail_symbols_received"),
        }
        self._handle_raw(bytes([ptype]) + body, profile)

    def _capture_near_miss(self, snap, confidence):
        """Saves the audio a near-miss gave up on, if WHALE_CAPTURE_DIR is
        set in the environment. Off by default.

        For the failure that is hardest to reason about from logs alone: a
        frame that syncs strongly and then fails CRC anyway. This is the only
        way to see what the radio actually received rather than what the
        decoder made of it, and that distinction is what such a case turns
        on -- a high confidence score reads the first 5% of the frame, so it
        says nothing about the 95% that failed.

        Kept on the strength of a past investigation that took a long time
        precisely because nothing about it reproduced in software:
        progressive arrival, truncation at every offset from 0 to 800ms,
        AWGN down to 15 dB and real off-air noise beds all decode fine. If a
        near-miss shows up that software cannot reproduce, these captures
        are the input to the analysis.
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
        keep = int(self._rx_keep_seconds * max(p.sample_rate for p in self._candidate_decode_profiles()))
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
        self._peer_unkeyed_at = None

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
        audio = profile.encode(header + remainder, head_seconds=self._tx_head_seconds,
                               tail_seconds=self._tx_tail_seconds)
        keyed = self.transport.send(audio)
        # Both numbers, because the gap between them is the PTT/settling
        # overhead this frame actually paid -- the thing to watch if air
        # time regresses. See scripts/sweep_ptt_timing.py.
        logger.info("[%s] TX %s at %s (%d body byte(s), %.2fs audio, %.2fs keyed)",
                    self.mycall, _ptype_name(ptype), profile.name, len(body),
                    len(audio) / profile.sample_rate, keyed)

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

    def _answer_duplicate_timing(self, body):
        if self.state != "CONNECTED" or self._timing_confirm_body is None:
            return False
        try:
            session_id, _, _ = _decode_timing(body)
        except ValueError:
            return False
        if session_id != self._session_id:
            return False
        self.on_event("PTT", on=True)
        self._tx_packet(PT_TIMING_CONFIRM, self._timing_confirm_body)
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
            elif ptype == PT_TIMING_ACK:
                self._answer_duplicate_timing(body)
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
        self._tx_head_seconds = self._tx_tail_seconds = CALIBRATION_SECONDS
        self._rx_head_seconds = self._rx_tail_seconds = CALIBRATION_SECONDS
        own_supported = list(self.modes.supported_ids)
        # Not `forced or history`: mode_id 0 is a real profile (300 baud)
        # and a perfectly reasonable thing to pin a bench run to.
        proposed_id = _forced_mode_id()
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
                measurement = self._rx_measurements.get((PT_CONNECT_ACK, ack_body), {})
                head = measurement.get("head")
                tail = measurement.get("tail")
                try:
                    timing_body = _encode_timing(self._session_id, head, tail)
                    _, head_duration, tail_duration = _decode_timing(timing_body)
                    self._rx_head_seconds, self._rx_tail_seconds = _derive_timing(
                        head_duration, tail_duration)
                except (TypeError, ValueError):
                    logger.warning("[%s] invalid CONNECT_ACK timing measurement", self.mycall)
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
                confirmed = None
                for _ in range(retries):
                    self.on_event("PTT", on=True)
                    self._tx_packet(PT_TIMING_ACK, timing_body)
                    self.on_event("PTT", off=True)
                    confirmed = self._wait_packet({PT_TIMING_CONFIRM}, timeout_per_try)
                    if confirmed is not None:
                        break
                if confirmed is None:
                    continue
                _, confirm_body = confirmed
                try:
                    confirm_session, own_head, own_tail = _decode_timing(confirm_body)
                    if confirm_session != self._session_id:
                        continue
                    self._tx_head_seconds, self._tx_tail_seconds = _derive_timing(
                        own_head, own_tail, self.modes.control.baud)
                except ValueError:
                    continue
                self._clean_streak = 0
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
        self.state = "LISTENING"
        self._tx_head_seconds = self._tx_tail_seconds = CALIBRATION_SECONDS
        self._rx_head_seconds = self._rx_tail_seconds = CALIBRATION_SECONDS
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
        # to CONTROL_PROFILE if the caller hasn't told us it supports that
        # mode. The two legs need not match: this rig's two directions
        # measure different SNR (see whale/afsk.py's module docstring).
        own_tx_id = _forced_mode_id()   # mode_id 0 is falsy, so not `or`
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
        # Once CONNECT has been accepted this is no longer an idle-listen
        # poll. Give the rest of the handshake its full control-frame
        # timeout even when the service called listen_once with a short
        # polling timeout.
        got_timing = self._wait_packet({PT_TIMING_ACK}, self.control_ack_timeout)
        if got_timing is None:
            self.state = "IDLE"
            return None
        _, timing_body = got_timing
        try:
            timing_session, own_head, own_tail = _decode_timing(timing_body)
            if timing_session != session_id:
                raise ValueError("wrong timing session")
            self._tx_head_seconds, self._tx_tail_seconds = _derive_timing(
                own_head, own_tail)
            measurement = self._rx_measurements[(PT_TIMING_ACK, timing_body)]
            peer_head, peer_tail = measurement["head"], measurement["tail"]
            confirm_body = _encode_timing(session_id, peer_head, peer_tail)
            _, peer_head_duration, peer_tail_duration = _decode_timing(confirm_body)
            self._rx_head_seconds, self._rx_tail_seconds = _derive_timing(
                peer_head_duration, peer_tail_duration)
        except (KeyError, TypeError, ValueError):
            self.state = "IDLE"
            return None
        self._timing_confirm_body = confirm_body
        self.on_event("PTT", on=True)
        self._tx_packet(PT_TIMING_CONFIRM, confirm_body)
        self.on_event("PTT", off=True)
        self._apply_rx_profile(self.modes.resolve(negotiated_id))
        self._apply_tx_profile(self.modes.resolve(own_tx_id))
        self._clean_streak = 0
        self.state = "CONNECTED"
        self.role = "IRS"  # the listener starts waiting for the floor -- see PT_FLOOR_REQ above
        self._reset_sequence_state()
        self.on_event("CONNECTED", mycall=self.mycall, peer=self.peer_call)
        logger.info("[%s] accepted connection from %s: tx=%s rx=%s", self.mycall, src,
                    self.tx_profile.name, self.rx_profile.name)
        return self.peer_call

    # -- floor (ISS/IRS) ---------------------------------------------------

    def _acquire_floor(self, retries=MAX_RETRIES):
        """Blocks until this station holds ISS -- the right to originate
        PT_DATA -- by asking whichever end currently holds it to hand it
        over. A no-op if we already have it.

        Retried the same way a DATA chunk is: the peer only answers
        PT_FLOOR_REQ from inside recv_message(), i.e. while it isn't itself
        mid-send (see recv_message and _handle_floor_req), so a request that
        lands while the peer is busy sending its own message is silently
        dropped by its _wait_packet.
        The retry after this attempt's timeout is what gets through once the
        peer goes back to polling for incoming work."""
        if self.role == "ISS":
            return True
        for attempt in range(1, retries + 1):
            logger.info("[%s] requesting the floor (attempt %d/%d)", self.mycall, attempt, retries)
            self.on_event("PTT", on=True)
            self._tx_packet(PT_FLOOR_REQ, b"")
            self.on_event("PTT", off=True)
            deadline = time.monotonic() + self.control_ack_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                got = self._wait_packet(
                    {PT_FLOOR_GRANT, PT_DATA, PT_DISC}, remaining)
                if got is None:
                    break
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
                    continue
                self.role = "ISS"
                logger.info("[%s] floor granted, now ISS", self.mycall)
                return True
        return False

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
        and the profiles have different chunk_size (78 at 300 baud, 170 at 600,
        each is whatever afsk.MAX_USEFUL_FRAME_SECONDS allows at that
        baud), so a message pre-split at the starting profile would keep
        sending undersized frames for the rest of the transfer even after
        stepping up."""
        if self.state != "CONNECTED":
            raise LinkError("not connected")
        if self.role != "ISS" and not self._acquire_floor():
            raise LinkError("could not acquire the floor to send")
        sent = 0
        offset = 0
        while True:
            chunk = data[offset:offset + self.tx_profile.chunk_size]
            offset += len(chunk)
            is_last = offset >= len(data)
            starting_profile = self.tx_profile
            attempts = self._send_chunk_with_arq(self._tx_seq, chunk, is_last)
            sent += 1
            if attempts is None:
                raise LinkError(f"no ACK for chunk {sent} ({offset}/{len(data)} bytes) "
                                f"after {MAX_RETRIES} tries")
            self._tx_seq = (self._tx_seq + 1) % SEQ_MODULO
            if self.tx_profile is starting_profile:
                self._maybe_adapt(attempts)
            else:
                # The retry loop already reacted to silence; do not take a
                # second step for the same chunk after its eventual ACK.
                self._clean_streak = 0
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
                got = self._wait_packet({PT_DATA_ACK, PT_DISC}, remaining)
                if got is None:
                    break
                ptype, body_in = got
                if ptype == PT_DISC:
                    self._handle_peer_disc()
                    raise LinkError("peer disconnected mid-transfer")
                if len(body_in) != 3:
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
            logger.warning("DATA seq=0x%02x: no ACK, retry %d/%d", seq, attempt, MAX_RETRIES)
            if attempt == STEP_DOWN_AFTER_ATTEMPTS:
                self._step_tx_mode(-1)
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
            self._step_tx_mode(scripted)
            return
        if attempts >= STEP_DOWN_AFTER_ATTEMPTS:
            self._clean_streak = 0
            self._step_tx_mode(-1)
            return
        self._clean_streak += 1
        if self._clean_streak >= STEP_UP_AFTER_CLEAN_STREAK:
            self._clean_streak = 0
            self._step_tx_mode(+1)

    def _step_tx_mode(self, direction):
        """Change this station's DATA mode without a control exchange.

        The next DATA frame announces the change. Its DATA_ACK echoes the
        mode actually decoded, while the reverse direction remains wholly
        independent."""
        candidate = self.modes.step(self.tx_profile, direction)
        if candidate is None:
            return
        if candidate.mode_id not in self.peer_supported_modes:
            return
        self._apply_tx_profile(candidate)
        logger.info("[%s] switched tx profile to %s; awaiting DATA_ACK confirmation",
                    self.mycall, self.tx_profile.name)

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
        if not body:
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
            # delivered, so drop the payload -- but still ack below.
            logger.info("[%s] DATA seq=0x%02x already have (expecting 0x%02x) -- dropping",
                        self.mycall, seq, self._rx_expect_seq)
        # ACK every DATA we see, duplicates included. The ACK names both the
        # answered frame and the sequence wanted next so a stale duplicate
        # cannot be mistaken for an answer to a later frame.
        self.on_event("PTT", on=True)
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
        self._connect_ack_body = None
        self._timing_confirm_body = None
        self._tx_head_seconds = self._tx_tail_seconds = CALIBRATION_SECONDS
        self._rx_head_seconds = self._rx_tail_seconds = CALIBRATION_SECONDS
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
