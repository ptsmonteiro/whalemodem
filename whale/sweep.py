"""Frame sweep: per-mode frame error rate and SNR over a real radio path.

``whale-test`` without ``--sweep`` runs a *session*: it starts the shipped
modem and drives one ARQ scenario through it, and answers "does this path
carry a session". The sweep answers the question that one cannot: **per
mode, does this path carry frames, and how well?**

    whale-test --sweep              listen, indefinitely, to be swept
    whale-test --sweep CALLSIGN     drive a sweep against that station

Both ends need ``--sweep``: the listener needs the raw receive stream and
cannot share the sound card with a running modem.

Nothing here is part of the ARQ protocol. The sweep adds no packet type, no
session state and no wire surface to ``whale/link.py``; it drives
``RadioTransport`` in-process and reuses the receive machinery -- the decode
attempt, the sync search and the near-miss capture -- by mixing in
``whale.link_receiver._ReceiverMixin`` and overriding only the two methods
that are about ARQ (which candidate profiles to search, and what a decoded
payload means). Its frames live inside a mode's payload, behind their own
magic, so an ARQ station that hears one simply fails to decode an air header
and drops it.

One mode's measurement, all of it at the control mode except the burst:

  1. the caller announces the mode, the frame count and the frame size,
     retrying if nothing answers;
  2. the listener answers READY and arms for that one mode -- which also
     measures the control mode in the reverse direction;
  3. the caller keys *once* and transmits N sequence-numbered frames, whose
     payload is derived from (sweep id, sequence) so both ends can generate
     it and a frame is verified byte-for-byte rather than merely CRC-passing;
  4. the listener tallies decoded / synced-but-failed / never-seen, plus the
     SNR of each decoded frame;
  5. the caller asks for the tally and records it;
  6. next mode -- walking *up* from the control mode, stopping after two
     consecutive modes decode nothing -- and then the roles swap, because
     the two directions of a path are not the same measurement.

A lost announcement is cheap and is retried. A lost result is expensive --
the burst it describes already cost minutes of air time -- so the listener
holds its tally until a new sweep addresses it or ``RESULT_HOLD_SECONDS``
passes, and the caller can ask again.

The listener never gets stuck armed: it arms with a deadline of the
announced burst's own duration plus margin, and falls back to idle when the
caller vanishes. One aborted sweep must not brick a station that was meant
to sit there until morning.

Exit status (the caller's; ``whale/test_cli.py`` applies it):

    0   the sweep produced a measurement. A mode that decoded nothing is a
        result, not a failure: the point of the exercise is to find where
        the path stops working, so finding it is success. The idle
        listener's Ctrl-C is also 0 -- stopping it is its only ending.
    1   no measurement: the far end never answered the first announcement
        at the control mode, or the caller was interrupted part way.
    2   the station could not be brought up at all and nothing was ever
        transmitted (``preflight``, in test_cli.py).

The SNR figures are whale's own per-decode estimate. They rank modes on one
path; they are not a lab measurement and are not comparable between
differently configured stations.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import random
import struct
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from whale.framing import AIR_HEADER_BYTES
from whale.link_protocol import call_bytes, take_call
from whale.link_receiver import _ReceiverMixin, _decode_snr

logger = logging.getLogger(__name__)

#: Sweep frames are prefixed with this rather than the ARQ air header's
#: ``WH``: neither layer can mistake the other's frames for its own.
MAGIC = b"WS"

FT_ANNOUNCE = 1
FT_READY = 2
FT_ASK = 3
FT_RESULT = 4
FT_SWAP = 5
FT_DONE = 6
FT_DATA = 7

FT_NAMES = {FT_ANNOUNCE: "ANNOUNCE", FT_READY: "READY", FT_ASK: "ASK",
            FT_RESULT: "RESULT", FT_SWAP: "SWAP", FT_DONE: "DONE",
            FT_DATA: "DATA"}

#: Fixed part of a burst frame: magic, type, sweep id, mode id, sequence.
DATA_HEADER = struct.Struct("!2sBHBB")

#: Frames in one mode's burst. Every one of them is paid for in air time at
#: the mode's own rate, and the whole burst is a single keying, so this is
#: also how far the receiver's decode may fall behind real time: the
#: transport retains ``RX_BUFFER_SECONDS`` and no more.
FRAMES_PER_MODE = 5

#: Announcements, and result requests, are retried this many times before
#: the exchange is given up on.
ANNOUNCE_RETRIES = 3
RESULT_RETRIES = 3

#: Added to a control-frame round trip to bound a reply, and multiplied per
#: retry as a backoff.
REPLY_SLACK = 5.0
RETRY_BACKOFF = 1.5

#: Slack added to an announced burst's own duration before an armed
#: listener gives up on the caller and returns to idle.
ARM_MARGIN_FRACTION = 0.5
ARM_MARGIN_SECONDS = 15.0

#: How long a tally is held for a caller that has not collected it. Long
#: enough to survive several failed result requests, short enough that a
#: station idling overnight is not still holding last night's numbers.
RESULT_HOLD_SECONDS = 900.0

#: A mode "fails" when nothing of its burst checked out at the far end.
#: Two in a row ends the walk up the ladder.
FAILURES_TO_STOP = 2

#: How long the caller waits, after handing over, for the far end to start
#: sweeping back.
SWAP_TIMEOUT = 300.0

#: How often a role loop comes up for air to check its stop flag.
POLL = 0.1

#: Sentinel for "no SNR observed" in a RESULT frame's tenths-of-a-dB fields.
SNR_NONE = -32768

#: How many sequence numbers a RESULT frame carries individually, as a
#: bitmap. *Which* frames of a burst were lost is a different diagnosis from
#: how many -- a lost first frame is an onset problem, a lost last one an
#: end-of-burst problem, and a scattered one is marginal decoding -- so the
#: identities ride back with the counts. The control mode has the room: a
#: RESULT payload is 33 bytes of the 42 it carries.
SEQ_BITMAP_BITS = 32

class SweepError(Exception):
    """The sweep could not produce a measurement. Exit status 1."""


# -- frames ----------------------------------------------------------------

@dataclass(frozen=True)
class SweepFrame:
    """One decoded sweep frame: the control fields, unparsed extras."""

    ftype: int
    from_call: str
    to_call: str
    sweep_id: int
    extra: bytes = b""

    @property
    def name(self) -> str:
        return FT_NAMES.get(self.ftype, f"0x{self.ftype:02x}")


def encode_control(ftype: int, from_call: str, to_call: str, sweep_id: int,
                   extra: bytes = b"") -> bytes:
    """One control-plane sweep frame's payload.

    Deliberately small: the HF control mode carries 42 payload bytes in
    total, so every field here is counted rather than convenient.
    """
    return (MAGIC + bytes([ftype]) + call_bytes(from_call) + call_bytes(to_call)
            + struct.pack("!H", sweep_id) + extra)


def decode_frame(payload: bytes) -> SweepFrame | None:
    """Parse a decoded mode payload, or ``None`` if it is not ours.

    A payload reaching here has already passed the mode codec's own CRC, so
    this is a demultiplexer, not an integrity check: anything that is not a
    sweep frame is somebody else's traffic and is simply not ours.
    """
    if len(payload) < 4 or payload[:2] != MAGIC:
        return None
    ftype = payload[2]
    if ftype == FT_DATA:
        if len(payload) < DATA_HEADER.size:
            return None
        _, _, sweep_id, mode_id, seq = DATA_HEADER.unpack(
            payload[:DATA_HEADER.size])
        return SweepFrame(FT_DATA, "", "", sweep_id,
                          struct.pack("!BB", mode_id, seq))
    if ftype not in FT_NAMES:
        return None
    try:
        from_call, offset = take_call(payload, 3)
        to_call, offset = take_call(payload, offset)
        sweep_id, = struct.unpack("!H", payload[offset:offset + 2])
    except (ValueError, UnicodeError, struct.error):
        return None
    return SweepFrame(ftype, from_call, to_call, sweep_id, payload[offset + 2:])


def announce_fields(frame: SweepFrame):
    """(mode_id, count, frame_size) of an ANNOUNCE, or ``None``."""
    try:
        return struct.unpack("!BBH", frame.extra[:4])
    except struct.error:
        return None


def mode_field(frame: SweepFrame):
    """The mode id a READY or ASK refers to, or ``None``."""
    return frame.extra[0] if frame.extra else None


def data_fields(frame: SweepFrame):
    """(mode_id, seq) of a burst frame."""
    return struct.unpack("!BB", frame.extra[:2])


def frame_payload(sweep_id: int, mode_id: int, seq: int, size: int) -> bytes:
    """The exact bytes of one burst frame, from its identity alone.

    Both ends generate this independently, which is what lets the receiver
    compare a decoded frame byte-for-byte instead of trusting the codec's
    CRC and its own idea of what should have arrived.
    """
    if size < DATA_HEADER.size:
        raise ValueError(f"a burst frame needs at least {DATA_HEADER.size} bytes")
    head = DATA_HEADER.pack(MAGIC, FT_DATA, sweep_id, mode_id, seq)
    return head + _filler(sweep_id, mode_id, seq, size - len(head))


def _filler(sweep_id: int, mode_id: int, seq: int, length: int) -> bytes:
    """A deterministic, platform-independent keystream for the frame body."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(
            struct.pack("!HBBI", sweep_id, mode_id, seq, counter)).digest()
        counter += 1
    return bytes(out[:length])


def full_frame_size(mode) -> int:
    """The payload length of a full-capacity frame in this mode.

    The same shape the ARQ layer's DATA frames have -- air header plus one
    chunk -- so a sweep frame costs what a real frame costs.
    """
    return AIR_HEADER_BYTES + mode.chunk_size


def sweep_modes(modes):
    """The ladder the sweep walks: the control mode, then upwards.

    ``ModeRegistry.modes`` is already in ascending rate order, so this is a
    slice. Starting at the control mode is what makes the first datum
    always obtainable -- and an operator who stops half way still has the
    rungs below whatever failed.
    """
    ids = [mode.mode_id for mode in modes.modes]
    start = ids.index(modes.control.mode_id)
    return tuple(modes.modes[start:])


def new_sweep_id() -> int:
    """A fresh sweep's identity, distinct from the last one at both ends."""
    return random.randrange(1, 1 << 16)


# -- tallies ---------------------------------------------------------------

@dataclass
class Tally:
    """One mode's receive-side result, as the receiving station saw it."""

    mode_id: int
    expected: int = 0
    decoded: set = field(default_factory=set)
    near_missed: int = 0
    snr_db: list = field(default_factory=list)

    @property
    def decoded_count(self) -> int:
        return len(self.decoded)

    @property
    def missed(self) -> int:
        """Frames never seen at all: not decoded, and never even synced."""
        return max(0, self.expected - self.decoded_count - self.near_missed)

    def snr_range(self):
        if not self.snr_db:
            return None
        return (min(self.snr_db), sum(self.snr_db) / len(self.snr_db),
                max(self.snr_db))


@dataclass(frozen=True)
class ModeResult:
    """One mode measured in one direction, as it goes into the report."""

    direction: str            # "TX" (the far end decoded us) or "RX"
    mode_id: int
    mode_name: str
    sent: int
    decoded: int
    near_missed: int
    missed: int
    snr_min: float | None = None
    snr_mean: float | None = None
    snr_max: float | None = None
    #: The sequence numbers that checked out, as far as they fit in a
    #: RESULT frame's bitmap. Empty when the tally never came back.
    seqs: tuple = ()

    @property
    def passed(self) -> bool:
        return self.decoded > 0


def encode_result(mode_id: int, tally: Tally) -> bytes:
    """A tally, small enough to ride in one control-mode frame."""
    span = tally.snr_range()
    values = (SNR_NONE, SNR_NONE, SNR_NONE) if span is None else tuple(
        max(-3000, min(3000, int(round(value * 10.0)))) for value in span)
    bitmap = 0
    for seq in tally.decoded:
        if 0 <= seq < SEQ_BITMAP_BITS:
            bitmap |= 1 << seq
    return struct.pack("!BBBBBhhhI", mode_id, min(255, tally.expected),
                       min(255, tally.decoded_count), min(255, tally.near_missed),
                       min(255, tally.missed), *values, bitmap)


def decode_result(extra: bytes):
    """(mode_id, sent, decoded, near, missed, snr_min, snr_mean, snr_max, seqs)."""
    try:
        (mode_id, sent, decoded, near, missed,
         low, mean, high) = struct.unpack("!BBBBBhhh", extra[:11])
    except struct.error:
        return None
    snrs = tuple(None if value == SNR_NONE else value / 10.0
                 for value in (low, mean, high))
    try:
        bitmap, = struct.unpack("!I", extra[11:15])
    except struct.error:
        # A peer that predates the bitmap still reports counts.
        bitmap = 0
    seqs = tuple(seq for seq in range(SEQ_BITMAP_BITS) if bitmap >> seq & 1)
    return (mode_id, sent, decoded, near, missed) + snrs + (seqs,)


# -- the receiver ----------------------------------------------------------

@dataclass
class _Armed:
    """What the listener is currently counting, and until when."""

    sweep_id: int
    mode: object
    count: int
    frame_size: int
    deadline: float
    tally: Tally


class SweepReceiver(_ReceiverMixin):
    """The decode loop, searching for sweep frames instead of ARQ packets.

    Everything expensive here is ``whale.link_receiver``'s: the decode
    attempt with its cost accounting, the near-miss rule that tells "synced
    but did not check out" from "never seen", and the diagnostic capture.
    What this class replaces is only the part that was about ARQ -- which
    profiles to search and what a decoded payload means -- so the sweep
    measures the same decoder the modem runs, not a copy of it.

    Idle, it searches one candidate: the control mode. Searching every
    waveform continuously for hours is precisely the expense
    ``_budgeted_candidates`` exists to avoid, and an idle listener has
    nothing else to hear.
    """

    def __init__(self, transport, mycall, modes, policy):
        self.transport = transport
        self.mycall = mycall
        self.modes = modes
        self.policy = policy
        # Read by the mixin. The sweep has no session, so these are fixed:
        # LISTENING keeps the inherited helpers on their cheap path, and no
        # peer modes are ever negotiated.
        self.state = "LISTENING"
        self.peer_supported_modes = set()
        self.rx_profile = modes.control
        self.on_event = lambda name, **kw: None
        self._rx_packets = queue.Queue()
        #: Decoded control frames, for whichever role loop is running.
        self.frames = queue.Queue()
        self._armed: _Armed | None = None
        self._held: tuple[int, Tally, float] | None = None
        #: Every decoded burst frame's SNR estimate, in arrival order, for
        #: the report's station-wide receive figure.
        self.snr_db: list = []
        self._lock = threading.Lock()
        self._rx_keep_seconds = max(
            mode.airtime(full_frame_size(mode)) for mode in modes.modes) + 1.0
        self._init_receiver()

    # -- lifecycle --------------------------------------------------------

    def start(self):
        self._start_receiver()

    def stop(self):
        self._stop_receiver()

    # -- arming -----------------------------------------------------------

    def arm(self, sweep_id: int, mode, count: int, frame_size: int,
            duration: float) -> None:
        """Count one mode's burst, for as long as that burst can take.

        The deadline is the whole point: a caller that vanishes after its
        announcement leaves this station armed, and an armed station is not
        listening for the next sweep. Falling back to idle is therefore not
        a cleanup, it is the property that lets two stations leave whale
        running all evening.
        """
        with self._lock:
            existing = self._armed
            if (existing is not None and existing.sweep_id == sweep_id
                    and existing.mode.mode_id == mode.mode_id
                    and existing.tally.decoded_count):
                # A repeated announcement for the measurement already in
                # progress: the caller missed our READY, but frames have
                # arrived since, so keep them and only extend the window.
                existing.deadline = time.monotonic() + duration
                return
            self._armed = _Armed(sweep_id, mode, count, frame_size,
                                 time.monotonic() + duration,
                                 Tally(mode.mode_id, expected=count))
            self.rx_profile = mode

    def check_expiry(self) -> bool:
        """Disarm if the announced burst's window has passed. Returns True then."""
        with self._lock:
            armed = self._armed
            if armed is None or time.monotonic() < armed.deadline:
                return False
            self._disarm_locked()
            return True

    def disarm(self) -> None:
        with self._lock:
            if self._armed is not None:
                self._disarm_locked()

    def _disarm_locked(self) -> None:
        armed = self._armed
        self._armed = None
        self.rx_profile = self.modes.control
        # The burst cost minutes of air time; the tally is kept so a caller
        # whose result request was lost can ask again.
        self._held = (armed.sweep_id, armed.tally, time.monotonic())

    def tally_for(self, sweep_id: int, mode_id: int):
        """The tally for one measurement, armed or lately held."""
        with self._lock:
            armed = self._armed
            if (armed is not None and armed.sweep_id == sweep_id
                    and armed.mode.mode_id == mode_id):
                return armed.tally
            held = self._held
            if held is not None and held[0] == sweep_id and held[1].mode_id == mode_id:
                if time.monotonic() - held[2] <= RESULT_HOLD_SECONDS:
                    return held[1]
            return None

    @property
    def armed(self):
        with self._lock:
            return self._armed

    # -- decoding ---------------------------------------------------------

    def _candidate_decode_profiles(self, snap=None):
        with self._lock:
            armed = self._armed
        if armed is None or armed.mode.mode_id == self.modes.control.mode_id:
            return (self.modes.control,)
        # The armed mode first: while a burst is arriving it is what the
        # audio actually contains, and the control mode is only searched so
        # that a result request landing after the burst is still heard.
        return (armed.mode, self.modes.control)

    def _decode_one(self, snap) -> bool:
        """Decode and consume one sweep frame, or one near-miss, from ``snap``."""
        profiles = self._candidate_decode_profiles(snap)
        results = []
        for profile in profiles:
            result = self._decode_attempt(profile, snap)
            results.append((profile, result))
            payload = result.get("payload")
            if payload is None:
                continue
            end = result.get("end_index", len(snap))
            frame = decode_frame(bytes(payload))
            self._consume_rx(end)
            if frame is not None:
                self._accept(profile, frame, result)
            else:
                # Somebody else's traffic (an ARQ station on the channel).
                # It decoded, so it is not a near miss; drop it and move on.
                logger.debug("[%s] ignoring a non-sweep payload at %s",
                             self.mycall, profile.name)
            return True

        pending = [result for profile, result in results
                   if result.get("confidence", 0) >= profile.confidence_threshold
                   and "end_index" not in result]
        if not pending:
            near = [(profile, result) for profile, result in results
                    if "end_index" in result]
            if near:
                profile, result = min(
                    near, key=lambda item: item[1].get("sync_end_index",
                                                       item[1]["end_index"]))
                skip = result.get("sync_end_index", result["end_index"])
                self._capture_near_miss(snap, result.get("confidence", 0))
                self._note_near_miss(profile)
                self._consume_rx(skip)
                return True
        self._prune_stale(len(snap))
        return False

    def _note_near_miss(self, profile) -> None:
        """Count a frame that synced at the armed mode and did not check out.

        Only the armed mode's own near misses count: a stray sync at the
        control mode is not one of the frames being measured.
        """
        with self._lock:
            armed = self._armed
            if armed is not None and armed.mode.mode_id == profile.mode_id:
                armed.tally.near_missed += 1
                logger.info("[%s] near miss at %s (%d so far)", self.mycall,
                            profile.name, armed.tally.near_missed)

    def _accept(self, profile, frame: SweepFrame, result) -> None:
        if frame.ftype != FT_DATA:
            logger.info("[%s] RX %s from %s at %s", self.mycall, frame.name,
                        frame.from_call, profile.name)
            self.frames.put(frame)
            return
        mode_id, seq = data_fields(frame)
        with self._lock:
            armed = self._armed
            if (armed is None or armed.sweep_id != frame.sweep_id
                    or armed.mode.mode_id != mode_id):
                logger.debug("[%s] burst frame for a sweep we are not armed for",
                             self.mycall)
                return
            expected = frame_payload(frame.sweep_id, mode_id, seq,
                                     armed.frame_size)
            if bytes(result["payload"]) != expected:
                # CRC-clean but not the bytes we generate for this identity:
                # it checked out to the codec and not to the measurement.
                armed.tally.near_missed += 1
                return
            armed.tally.decoded.add(seq)
            snr_db, _ = _decode_snr(result)
            if snr_db is not None:
                armed.tally.snr_db.append(snr_db)
                self.snr_db.append(snr_db)
            # Frames arrive back to back in one keying: extend the window by
            # what is left of the burst rather than letting a slow decode
            # race the deadline it was armed with.
            armed.deadline = max(armed.deadline, time.monotonic()
                                 + armed.mode.airtime(armed.frame_size)
                                 * max(0, armed.count - len(armed.tally.decoded))
                                 + ARM_MARGIN_SECONDS)


# -- the engine ------------------------------------------------------------

class SweepEngine:
    """Both roles of a sweep over one transport, in this process.

    Nothing here spawns a modem: that is the session test's concern, and a
    modem would own the sound card this needs.
    """

    def __init__(self, transport, mycall, modes, policy, *, turnaround=None,
                 frames_per_mode: int = FRAMES_PER_MODE, step=None):
        self.transport = transport
        self.mycall = mycall
        self.modes = modes
        self.policy = policy
        self.turnaround = (policy.tx_turnaround_delay if turnaround is None
                           else turnaround)
        self.frames_per_mode = frames_per_mode
        self.step = step or (lambda text: logger.info("%s", text))
        self.receiver = SweepReceiver(transport, mycall, modes, policy)
        #: Every time this station keyed. One burst is one keying.
        self.keyings = 0
        self.results: list[ModeResult] = []
        self.peers: set = set()
        #: (sweep id, mode id) already written into self.results as an RX
        #: row, so that a re-asked result is answered without recording the
        #: same measurement twice.
        self._recorded: set = set()
        #: The hand-over already answered, so a repeated SWAP is answered
        #: rather than swept again.
        self._last_swap = None
        #: This station's own sweep, once it has driven one.
        self.sweep_id = None
        self.sweeps = 0
        self._stop = threading.Event()

    # -- lifecycle --------------------------------------------------------

    def start(self):
        self.receiver.start()

    def stop(self):
        """Stop the receive loop and put the radio down, unkeyed."""
        self._stop.set()
        self.receiver.stop()
        self.transport.close()

    # -- transmitting -----------------------------------------------------

    def _await_turnaround(self) -> None:
        """Dead air before keying over a peer that has just finished.

        The ARQ layer measures this from where the peer's audio ended; here
        every keying is a reply to something that has just been decoded, so
        the whole allowance is simply waited out. It is short next to a
        burst and it is never the thing being measured.
        """
        if self.turnaround > 0:
            self._stop.wait(self.turnaround)

    def _tx(self, audio) -> float:
        self.keyings += 1
        return self.transport.send(audio)

    def _tx_control(self, ftype: int, peer: str, sweep_id: int,
                    extra: bytes = b"") -> None:
        payload = encode_control(ftype, self.mycall, peer, sweep_id, extra)
        self._await_turnaround()
        control = self.modes.control
        logger.info("[%s] TX %s to %s at %s", self.mycall,
                    FT_NAMES.get(ftype, ftype), peer, control.name)
        self._tx(control.encode(payload))

    def _tx_burst(self, sweep_id: int, mode, count: int, frame_size: int) -> None:
        """One keying carrying the whole burst, back to back.

        Keying once per burst is not an optimisation: the PTT lead-in, the
        settling time and the turnaround would otherwise be a larger part of
        the measurement than the frames are.
        """
        self._await_turnaround()
        audio = np.concatenate([
            mode.encode(frame_payload(sweep_id, mode.mode_id, seq, frame_size))
            for seq in range(count)])
        logger.info("[%s] TX burst of %d frame(s) at %s (%.1fs audio)",
                    self.mycall, count, mode.name, len(audio) / mode.tx_sample_rate)
        self._tx(audio)

    # -- receiving --------------------------------------------------------

    def _await_frame(self, timeout: float, match) -> SweepFrame | None:
        """Wait for a control frame ``match`` accepts, servicing the rest."""
        deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                frame = self.receiver.frames.get(timeout=min(POLL, remaining))
            except queue.Empty:
                continue
            if match(frame):
                return frame
            logger.debug("[%s] ignoring %s while waiting", self.mycall, frame.name)
        return None

    def _addressed(self, frame: SweepFrame) -> bool:
        return frame.to_call == self.mycall

    # -- budgets ----------------------------------------------------------

    def control_reply_timeout(self) -> float:
        """A control-mode round trip, plus slack: what bounds every reply."""
        control = self.modes.control
        return (2 * control.airtime(full_frame_size(control))
                + 2 * self.turnaround + REPLY_SLACK)

    def burst_duration(self, mode, count: int, frame_size: int) -> float:
        return count * mode.airtime(frame_size) + 2 * self.turnaround + REPLY_SLACK

    def arm_window(self, mode, count: int, frame_size: int) -> float:
        burst = self.burst_duration(mode, count, frame_size)
        return burst * (1 + ARM_MARGIN_FRACTION) + ARM_MARGIN_SECONDS

    # -- the caller -------------------------------------------------------

    def run_caller(self, peer: str) -> None:
        """Measure every mode towards ``peer``, then hand the sweep back."""
        sweep_id = self.sweep_id = new_sweep_id()
        self.peers.add(peer)
        self.step(f"Sweeping {peer} as {self.mycall} (sweep {sweep_id:#06x}).")
        self._measure_all(peer, sweep_id)
        self.sweeps += 1
        self.step("Handing over: the other station now sweeps this one.")
        self._hand_over(peer, sweep_id)

    def _measure_all(self, peer: str, sweep_id: int) -> None:
        failures = 0
        for mode in sweep_modes(self.modes):
            if self._stop.is_set():
                raise KeyboardInterrupt
            try:
                result = self._measure(peer, sweep_id, mode)
            except SweepError:
                # An unanswered announcement is not this mode failing -- it
                # went out at the control mode -- so the walk stops here.
                # With modes already measured that is a shorter measurement,
                # not a failed run; with none it is a run that never started.
                if not self.results:
                    raise
                self.step("The other station stopped answering at the control "
                          "mode; keeping what was measured.")
                return
            self.results.append(result)
            if result.passed:
                failures = 0
            else:
                failures += 1
                if failures >= FAILURES_TO_STOP:
                    self.step(f"Stopping the walk up: {FAILURES_TO_STOP} modes "
                              "in a row decoded nothing.")
                    return

    def _measure(self, peer: str, sweep_id: int, mode) -> ModeResult:
        """One mode, announced, burst, and collected."""
        count = self.frames_per_mode
        frame_size = full_frame_size(mode)
        self.step(f"Mode {mode.mode_id} ({mode.name}): announcing "
                  f"{count} x {frame_size} bytes.")
        if not self._announce(peer, sweep_id, mode, count, frame_size):
            raise SweepError(
                f"{peer} did not answer the announcement for mode "
                f"{mode.mode_id} at the control mode "
                f"({self.modes.control.name}), so nothing can be measured.")
        self._tx_burst(sweep_id, mode, count, frame_size)
        self.step(f"Mode {mode.mode_id}: burst sent; asking for the result.")
        collected = self._collect(peer, sweep_id, mode)
        if collected is None:
            self.step(f"Mode {mode.mode_id}: no result came back.")
            return ModeResult("TX", mode.mode_id, mode.name, count, 0, 0, count)
        (_, sent, decoded, near, missed, low, mean, high, seqs) = collected
        result = ModeResult("TX", mode.mode_id, mode.name, sent or count,
                            decoded, near, missed, low, mean, high, seqs)
        self.step(f"Mode {mode.mode_id}: {decoded}/{result.sent} decoded "
                  f"{seq_list(seqs, result.sent)}, "
                  f"{near} near missed, {missed} never seen"
                  + ("" if mean is None else f", SNR {mean:.1f} dB mean"))
        return result

    def _announce(self, peer: str, sweep_id: int, mode, count: int,
                  frame_size: int) -> bool:
        """Announce until it is answered. A lost announcement is cheap."""
        extra = struct.pack("!BBH", mode.mode_id, count, frame_size)
        timeout = self.control_reply_timeout()
        for attempt in range(ANNOUNCE_RETRIES):
            self._tx_control(FT_ANNOUNCE, peer, sweep_id, extra)
            ready = self._await_frame(
                timeout * (RETRY_BACKOFF ** attempt),
                lambda frame: (frame.ftype == FT_READY and self._addressed(frame)
                               and frame.sweep_id == sweep_id
                               and mode_field(frame) == mode.mode_id))
            if ready is not None:
                return True
            if self._stop.is_set():
                raise KeyboardInterrupt
            self.step(f"Mode {mode.mode_id}: no answer; announcing again "
                      f"({attempt + 2} of {ANNOUNCE_RETRIES}).")
        return False

    def _collect(self, peer: str, sweep_id: int, mode):
        """Ask for the tally, and ask again: the burst is already spent."""
        extra = bytes([mode.mode_id])
        timeout = self.control_reply_timeout()
        for attempt in range(RESULT_RETRIES):
            self._tx_control(FT_ASK, peer, sweep_id, extra)
            frame = self._await_frame(
                timeout * (RETRY_BACKOFF ** attempt),
                lambda frame: (frame.ftype == FT_RESULT and self._addressed(frame)
                               and frame.sweep_id == sweep_id))
            if frame is not None:
                decoded = decode_result(frame.extra)
                if decoded is not None and decoded[0] == mode.mode_id:
                    return decoded
            if self._stop.is_set():
                raise KeyboardInterrupt
        return None

    def _hand_over(self, peer: str, sweep_id: int) -> None:
        """Ask the far end to sweep this way, and serve it while it does.

        Reciprocity is not assumed: a path that carries a mode one way
        regularly will not carry it the other, which is exactly what two
        stations in different locations need to find out.
        """
        for attempt in range(ANNOUNCE_RETRIES):
            self._tx_control(FT_SWAP, peer, sweep_id)
            if self.serve(timeout=SWAP_TIMEOUT, until_done=True):
                return
            if self._stop.is_set():
                raise KeyboardInterrupt
            self.step(f"No sweep came back; asking again "
                      f"({attempt + 2} of {ANNOUNCE_RETRIES}).")
        self.step("The other station never swept back; "
                  "only this direction was measured.")

    # -- the listener -----------------------------------------------------

    def run_listener(self) -> None:
        """Sit, passive, until somebody sweeps -- and then again, and again.

        Nothing is transmitted until this station is addressed by name. When
        a sweep finishes, this returns to idle: two stations can sweep each
        other all evening without restarting anything.
        """
        self.step(f"Listening as {self.mycall}. Ask the other station to run: "
                  f"whale-test --sweep {self.mycall}")
        while not self._stop.is_set():
            self.serve(timeout=None, until_done=False)

    def serve(self, timeout: float | None, until_done: bool) -> bool:
        """Answer sweep traffic addressed to this station.

        ``timeout`` bounds the wait for the *first* frame of a sweep; once
        one arrives the deadline is pushed out, because the sweep itself is
        long and the far end is demonstrably there. ``until_done`` returns
        as soon as the far end says it has finished, which is what the
        caller's hand-over waits for. Returns whether a sweep was served.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        served = False
        while not self._stop.is_set():
            if self.receiver.check_expiry():
                self.step("The announced burst never came; back to listening.")
            if deadline is not None and time.monotonic() > deadline:
                return served
            try:
                frame = self.receiver.frames.get(timeout=POLL)
            except queue.Empty:
                continue
            if not self._addressed(frame):
                continue
            if deadline is not None:
                deadline = time.monotonic() + max(timeout, SWAP_TIMEOUT)
            served = True
            if self._serve_frame(frame) and until_done:
                return True
        return served

    def _serve_frame(self, frame: SweepFrame) -> bool:
        """Handle one addressed frame. Returns True when the sweep is over."""
        self.peers.add(frame.from_call)
        if frame.ftype == FT_ANNOUNCE:
            self._serve_announce(frame)
        elif frame.ftype == FT_ASK:
            self._serve_ask(frame)
        elif frame.ftype == FT_SWAP:
            peer = frame.from_call
            if frame.sweep_id == self._last_swap:
                # A repeated hand-over for the sweep just answered: our DONE
                # was lost, not the sweep. Say it again rather than keying a
                # second sweep's worth of bursts.
                self._tx_control(FT_DONE, peer, frame.sweep_id)
                return True
            self._last_swap = frame.sweep_id
            self.step(f"{peer} handed the sweep over; "
                      "measuring the other direction.")
            try:
                self._measure_all(peer, new_sweep_id())
            except SweepError as exc:
                # The listener is the station that has to survive this: it
                # was left running to be swept again, so a sweep back that
                # could not start returns it to idle rather than ending it.
                self.step(f"The sweep back could not start: {exc}")
            self.sweeps += 1
            self._tx_control(FT_DONE, peer, frame.sweep_id)
            self.step("Sweep complete; back to listening.")
            return True
        elif frame.ftype == FT_DONE:
            self.step(f"{frame.from_call} finished its sweep.")
            return True
        return False

    def _serve_announce(self, frame: SweepFrame) -> None:
        fields = announce_fields(frame)
        if fields is None:
            return
        mode_id, count, frame_size = fields
        mode = self.modes.by_id.get(mode_id)
        if mode is None:
            logger.info("[%s] announced mode %d is not in this registry",
                        self.mycall, mode_id)
            return
        window = self.arm_window(mode, count, frame_size)
        self.receiver.arm(frame.sweep_id, mode, count, frame_size, window)
        self.step(f"{frame.from_call} announced mode {mode_id} ({mode.name}): "
                  f"{count} frame(s), listening for up to {window:.0f}s.")
        self._tx_control(FT_READY, frame.from_call, frame.sweep_id,
                         bytes([mode_id]))

    def _serve_ask(self, frame: SweepFrame) -> None:
        mode_id = mode_field(frame)
        if mode_id is None:
            return
        # The burst is over: stop counting before reporting, so the numbers
        # sent and the numbers kept are the same ones.
        armed = self.receiver.armed
        if (armed is not None and armed.sweep_id == frame.sweep_id
                and armed.mode.mode_id == mode_id):
            self.receiver.disarm()
        tally = self.receiver.tally_for(frame.sweep_id, mode_id)
        if tally is None:
            logger.info("[%s] asked for a tally we do not have (sweep %#06x "
                        "mode %d)", self.mycall, frame.sweep_id, mode_id)
            return
        mode = self.modes.by_id.get(mode_id)
        span = tally.snr_range()
        if (frame.sweep_id, mode_id) not in self._recorded:
            self._recorded.add((frame.sweep_id, mode_id))
            self.results.append(ModeResult(
                "RX", mode_id, mode.name if mode else str(mode_id), tally.expected,
                tally.decoded_count, tally.near_missed, tally.missed,
                *(span or (None, None, None)),
                tuple(sorted(tally.decoded))))
        self.step(f"Mode {mode_id}: heard {tally.decoded_count}/{tally.expected} "
                  f"{seq_list(sorted(tally.decoded), tally.expected)}, "
                  f"{tally.near_missed} near missed, {tally.missed} never seen.")
        self._tx_control(FT_RESULT, frame.from_call, frame.sweep_id,
                         encode_result(mode_id, tally))


# -- report ----------------------------------------------------------------

SNR_NOTE = ("SNR is whale's own per-decode estimate. It ranks modes on this "
            "path; it is not a lab measurement and is not comparable between "
            "differently configured stations.")


def seq_list(seqs, sent: int) -> str:
    """The burst's sequence numbers as a fixed-width hit/miss strip.

    A count says how badly a mode did; this says *where* it went wrong,
    which is the difference between an onset problem, an end-of-burst
    problem and ordinary marginal decoding at the top of the ladder.
    """
    seen = set(seqs)
    if sent > SEQ_BITMAP_BITS:
        return "?" * SEQ_BITMAP_BITS + "+"
    return "".join("#" if seq in seen else "." for seq in range(sent))


def result_table(results) -> list[str]:
    """The per-mode table both ends put in their report."""
    header = (f"{'Dir':<4}{'Mode':<6}{'Waveform':<10}{'Sent':>5}{'Dec':>5}"
              f"{'Near':>6}{'Miss':>6}{'SNR min':>9}{'mean':>7}{'max':>7}"
              f"  {'Frames'}")
    lines = [header, "-" * len(header)]
    if not results:
        lines.append("no mode was measured")
        return lines
    for row in results:
        def snr(value):
            return "      --" if value is None else f"{value:8.1f}"
        lines.append(
            f"{row.direction:<4}{row.mode_id:<6}{row.mode_name:<10}"
            f"{row.sent:>5}{row.decoded:>5}{row.near_missed:>6}{row.missed:>6}"
            f"{snr(row.snr_min):>9}{snr(row.snr_mean):>7}{snr(row.snr_max):>7}"
            f"  {seq_list(row.seqs, row.sent)}")
    lines.append("")
    lines.append("Frames: one character per sequence number in the burst, "
                 "'#' decoded and '.' not.")
    return lines
