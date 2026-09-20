"""Live curses status dashboard for ``whale-server --tui``.

Step 1 of the modem TUI: a read-only view built from three zero-risk data
sources only -- :class:`whale.service.ModemService` events, structurally
parsed ``whale.link`` log records, and read-only polling of counters that
already exist on the transport/link. Nothing here changes decoder, link or
service behavior; the accumulator (:class:`TuiState`) and its log handler
(:class:`LogTap`) are designed to never raise into their callers.

Split the same way as ``whale/config_tui.py``: pure state (``TuiState``)
and pure renderers (``render_header`` / ``render_link`` / ``render_log``)
that return ``list[str]``, testable with no real terminal, plus a thin
``run()`` curses loop that copies the ``_line`` clipping / ``screen.timeout``
idioms from ``whale/level_tui.py``.
"""

from __future__ import annotations

import collections
import curses
import dataclasses
import logging
import re
import threading
import time
from dataclasses import dataclass

# The exact whale.link log format strings this module parses structurally
# (via record.args, not by regexing the rendered text). Kept here rather
# than imported so this module never imports whale.link -- step 1 must not
# depend on, or risk perturbing, the decoder/link stack.
DECODED_FRAME_FORMAT = "[%s] decoded %s body at profile %s (%s; %s)"
RX_FRAME_FORMAT = "[%s] RX %s at %s (%d body byte(s))"

_SNR_RE = re.compile(r"SNR\s+([+-]?\d+(?:\.\d+)?)\s+dB")

MAX_SNR_HISTORY = 60
MAX_LOG_LINES = 500

_SPARK_CHARS = " ▁▂▃▄▅▆▇█"


# --- state -----------------------------------------------------------------

@dataclass
class ProfileStats:
    count: int = 0
    last_snr: float | None = None
    last_seen: float | None = None  # time.monotonic() of the last frame


@dataclass
class Snapshot:
    """An immutable copy of :class:`TuiState`, safe to render without a lock."""

    mycall: str
    radio: str
    channel: str
    connected: bool
    peer: str | None
    ptt_on: bool
    rx_mode_id: int | None
    rx_mode_name: str | None
    rx_bps: float | None
    tx_mode_id: int | None
    tx_mode_name: str | None
    tx_bps: float | None
    rx_overflows: int
    tx_underflows: int
    last_snr: float | None
    snr_history: list[float]
    profiles: dict[str, ProfileStats]
    qualification_metrics: dict
    log_lines: list[str]
    now: float
    poll_error: str | None


def _mode_name(mode_registry, mode_id):
    """Best-effort mode_id -> profile name lookup; never raises."""
    try:
        if mode_registry is None or mode_id is None:
            return None
        by_id = getattr(mode_registry, "by_id", None)
        if by_id is None:
            return None
        profile = by_id.get(mode_id)
        return getattr(profile, "name", None)
    except Exception:
        return None


class TuiState:
    """Lock-protected accumulator fed by service events, log records and polling.

    Every public method here is exception-safe: it is called from the
    service's event-dispatch thread (``on_event``), from the logging thread
    that owns whatever handler is emitting (via ``LogTap``), and from the UI
    thread (``poll`` / ``snapshot`` / ``reset``). None of them may raise into
    their caller.
    """

    def __init__(self, mycall: str, radio: str, channel: str, mode_registry=None) -> None:
        self.mycall = mycall
        self.radio = radio
        self.channel = channel
        self.mode_registry = mode_registry
        self._lock = threading.Lock()

        self.connected = False
        self.peer: str | None = None
        self.ptt_on = False
        self.rx_mode_id: int | None = None
        self.rx_mode_name: str | None = None
        self.rx_bps: float | None = None
        self.tx_mode_id: int | None = None
        self.tx_mode_name: str | None = None
        self.tx_bps: float | None = None
        self.rx_overflows = 0
        self.tx_underflows = 0
        self.qualification_metrics: dict = {}
        self.log_lines: collections.deque[str] = collections.deque(maxlen=MAX_LOG_LINES)
        self.poll_error: str | None = None
        self._reset_counters_locked()

    def _reset_counters_locked(self) -> None:
        self.last_snr: float | None = None
        self.snr_history: collections.deque[float] = collections.deque(maxlen=MAX_SNR_HISTORY)
        self.profiles: dict[str, ProfileStats] = {}

    def reset(self) -> None:
        """Reset the frame/SNR counters. Connection and PTT state are left alone."""
        with self._lock:
            self._reset_counters_locked()

    # -- service event subscriber (called on the service's dispatch thread) --

    def on_event(self, name: str, **kw) -> None:
        try:
            with self._lock:
                self._handle_event_locked(name, kw)
        except Exception:
            # A subscriber must never raise into ModemService._emit (which
            # already guards its callers, but this stays defensive on its
            # own so TuiState is safe to reuse anywhere).
            pass

    def _handle_event_locked(self, name: str, kw: dict) -> None:
        if name == "PTT":
            self.ptt_on = bool(kw.get("on"))
        elif name == "CONNECTED":
            self.connected = True
            self.peer = kw.get("peer")
        elif name in ("CONNECT_FAILED", "DISCONNECTED"):
            self.connected = False
            self.peer = None
        elif name == "SNR":
            snr_db = kw.get("snr_db")
            if snr_db is not None:
                snr_db = float(snr_db)
                self.last_snr = snr_db
                self.snr_history.append(snr_db)
        elif name == "BITRATE":
            direction = kw.get("direction")
            mode_id = kw.get("mode_id")
            bps = kw.get("bits_per_second")
            mode_name = _mode_name(self.mode_registry, mode_id)
            if direction == "RX":
                self.rx_mode_id, self.rx_mode_name, self.rx_bps = mode_id, mode_name, bps
            elif direction == "TX":
                self.tx_mode_id, self.tx_mode_name, self.tx_bps = mode_id, mode_name, bps
        # OUTBOUND_DRAINED: nothing tracked in step 1.

    # -- log tap callbacks --

    def append_log(self, line: str) -> None:
        with self._lock:
            self.log_lines.append(line)

    def note_frame(self, profile_name: str, snr_db: float | None) -> None:
        with self._lock:
            stats = self.profiles.setdefault(profile_name, ProfileStats())
            stats.count += 1
            if snr_db is not None:
                stats.last_snr = snr_db
            stats.last_seen = time.monotonic()

    # -- UI-thread polling (plain attribute reads only; never raises) --

    def poll(self, service) -> None:
        """Read-only poll of transport/link counters reachable from ``service``.

        Every attribute access is defended individually with ``getattr``
        defaults: none of these are part of any public contract, so a
        rename or refactor there should degrade this dashboard, not crash
        the modem or the TUI.
        """
        rx_overflows = 0
        tx_underflows = 0
        qualification_metrics: dict = {}
        error: str | None = None
        try:
            link = getattr(service, "_link", None)
            transport = getattr(link, "transport", None)
            rx_overflows = int(getattr(transport, "rx_overflows", 0) or 0)
            tx_underflows = int(getattr(transport, "tx_underflows", 0) or 0)
            metrics = getattr(link, "qualification_metrics", None)
            if isinstance(metrics, dict):
                qualification_metrics = dict(metrics)
        except Exception as exc:
            error = f"poll error: {exc}"
        with self._lock:
            self.rx_overflows = rx_overflows
            self.tx_underflows = tx_underflows
            self.qualification_metrics = qualification_metrics
            self.poll_error = error

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                mycall=self.mycall, radio=self.radio, channel=self.channel,
                connected=self.connected, peer=self.peer, ptt_on=self.ptt_on,
                rx_mode_id=self.rx_mode_id, rx_mode_name=self.rx_mode_name, rx_bps=self.rx_bps,
                tx_mode_id=self.tx_mode_id, tx_mode_name=self.tx_mode_name, tx_bps=self.tx_bps,
                rx_overflows=self.rx_overflows, tx_underflows=self.tx_underflows,
                last_snr=self.last_snr, snr_history=list(self.snr_history),
                profiles={name: dataclasses.replace(stats) for name, stats in self.profiles.items()},
                qualification_metrics=dict(self.qualification_metrics),
                log_lines=list(self.log_lines),
                now=time.monotonic(),
                poll_error=self.poll_error,
            )


# --- log tap -----------------------------------------------------------------

def _parse_decoded_frame(record: logging.LogRecord) -> tuple[str, float | None] | None:
    """Best-effort structural parse of the "decoded ... body at profile ..." line.

    Reads ``record.args`` (the format arguments actually passed to
    ``logger.info``), not the rendered text, matching link.py:1002's
    ``"[%s] decoded %s body at profile %s (%s; %s)"``. Returns ``None`` for
    anything that doesn't match exactly -- callers fall back to showing the
    record as a plain log line.
    """
    if record.msg != DECODED_FRAME_FORMAT:
        return None
    args = record.args
    if not isinstance(args, tuple) or len(args) != 5:
        return None
    profile_name = args[2]
    snr_summary = args[4]
    snr_db = None
    match = _SNR_RE.search(str(snr_summary))
    if match:
        try:
            snr_db = float(match.group(1))
        except ValueError:
            snr_db = None
    return str(profile_name), snr_db


class LogTap(logging.Handler):
    """Logging handler that feeds a ring buffer and a best-effort frame parse.

    Never raises: ``emit`` wraps its entire body in ``try/except``, and
    ``handleError`` is overridden to swallow rather than fall back to
    logging's default stderr traceback dump (which could itself recurse
    through this same handler if the root logger has no other handlers).
    """

    def __init__(self, state: TuiState, level=logging.NOTSET) -> None:
        super().__init__(level)
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emit(record)
        except Exception:
            pass

    def _emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            message = "<unformattable log record>"
        level = record.levelname[:4] if record.levelname else "?"
        self.state.append_log(f"{level:>4} {record.name}: {message}")
        try:
            parsed = _parse_decoded_frame(record)
        except Exception:
            parsed = None
        if parsed is not None:
            profile_name, snr_db = parsed
            self.state.note_frame(profile_name, snr_db)

    def handleError(self, record: logging.LogRecord) -> None:
        pass


# --- pure renderers ----------------------------------------------------------

def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text[:width]


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo
    chars = []
    for value in values:
        if span <= 0:
            index = len(_SPARK_CHARS) - 1
        else:
            index = int(round((value - lo) / span * (len(_SPARK_CHARS) - 1)))
        chars.append(_SPARK_CHARS[index])
    return "".join(chars)


def _format_mode(mode_id: int | None, mode_name: str | None, bps: float | None) -> str:
    if mode_id is None:
        return "-"
    bps_text = f"{bps:.0f} bps" if bps is not None else "? bps"
    # Name and ID together: the logs speak in IDs, so showing one or the
    # other made the same event look like two.
    if mode_name:
        return f"{mode_name} ({mode_id}, {bps_text})"
    return f"mode {mode_id} ({bps_text})"


def render_header(snapshot: Snapshot, width: int) -> list[str]:
    state_desc = f"connected to {snapshot.peer}" if snapshot.connected else "listening"
    lines = [
        f"mycall {snapshot.mycall}  radio {snapshot.radio}  channel {snapshot.channel}  "
        f"state: {state_desc}",
    ]
    ptt = "PTT ON" if snapshot.ptt_on else "ptt off"
    rx = _format_mode(snapshot.rx_mode_id, snapshot.rx_mode_name, snapshot.rx_bps)
    tx = _format_mode(snapshot.tx_mode_id, snapshot.tx_mode_name, snapshot.tx_bps)
    lines.append(f"{ptt}   RX {rx}   TX {tx}")
    lines.append(f"RX overflows {snapshot.rx_overflows}   TX underflows {snapshot.tx_underflows}")
    if snapshot.poll_error:
        lines.append(snapshot.poll_error)
    return [_clip(line, width) for line in lines]


def render_link(snapshot: Snapshot, width: int) -> list[str]:
    snr_text = f"{snapshot.last_snr:.1f} dB" if snapshot.last_snr is not None else "n/a"
    spark = _sparkline(snapshot.snr_history)
    lines = [f"Last SNR: {snr_text}  {spark}".rstrip()]
    lines.append(f"{'Profile':<14} {'Count':>5}  {'LastSNR':>7}  {'Age':>5}")
    for name in sorted(snapshot.profiles):
        stats = snapshot.profiles[name]
        age = "-" if stats.last_seen is None else f"{max(0.0, snapshot.now - stats.last_seen):.0f}s"
        snr = "-" if stats.last_snr is None else f"{stats.last_snr:.1f}"
        lines.append(f"{name:<14} {stats.count:>5}  {snr:>7}  {age:>5}")
    metrics = snapshot.qualification_metrics
    if metrics:
        mode_changes = metrics.get("mode_changes")
        mode_changes_count = len(mode_changes) if isinstance(mode_changes, list) else 0
        lines.append(
            f"qualification: attempts {metrics.get('data_attempts', 0)}  "
            f"retransmits {metrics.get('retransmissions', 0)}  "
            f"ack_timeouts {metrics.get('ack_timeouts', 0)}  "
            f"dup_data {metrics.get('duplicate_data', 0)}  "
            f"mode_changes {mode_changes_count}")
    return [_clip(line, width) for line in lines]


def render_log(snapshot: Snapshot, width: int, height: int) -> list[str]:
    if height <= 0:
        return []
    tail = snapshot.log_lines[-height:]
    return [_clip(line, width) for line in tail]


# --- curses loop ---------------------------------------------------------

def _line(screen, row: int, message: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if row < 0 or row >= height or width < 2:
        return
    try:
        screen.addnstr(row, 0, message, width - 1, attr)
    except curses.error:
        pass


def _draw(screen, snapshot: Snapshot, has_color: bool, server_thread) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    text_width = max(0, width - 1)
    row = 0
    for line in render_header(snapshot, text_width):
        _line(screen, row, line, curses.A_BOLD)
        row += 1
    row += 1
    if server_thread is not None and not server_thread.is_alive():
        attr = curses.A_REVERSE
        if has_color:
            attr |= curses.color_pair(1)
        _line(screen, row, "SERVER THREAD STOPPED -- see log file", attr)
        row += 1
    for line in render_link(snapshot, text_width):
        _line(screen, row, line)
        row += 1
    row += 1
    _line(screen, row, "[r] reset counters  [q] quit")
    row += 1
    log_top = row
    log_height = max(0, height - log_top - 1)
    for offset, line in enumerate(render_log(snapshot, text_width, log_height)):
        _line(screen, log_top + offset, line)
    screen.refresh()


def run(screen, state: TuiState, server, service, server_thread=None) -> None:
    """Curses main loop: 200ms tick, 'r' resets counters, 'q'/Ctrl+C stops the server.

    ``server_thread``, when given, is the thread running ``server.serve_forever()``;
    if it dies, the dashboard shows that instead of silently going stale.
    """
    curses.curs_set(0)
    screen.timeout(200)
    has_color = False
    try:
        has_color = curses.has_colors()
        if has_color:
            curses.start_color()
            curses.init_pair(1, curses.COLOR_RED, curses.COLOR_BLACK)
    except curses.error:
        has_color = False

    try:
        while True:
            state.poll(service)
            snapshot = state.snapshot()
            _draw(screen, snapshot, has_color, server_thread)
            key = screen.getch()
            if key == ord("q"):
                break
            if key == ord("r"):
                state.reset()
            # curses.KEY_RESIZE and anything else just redraws on the next
            # tick with a fresh getmaxyx().
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.stop()
        except Exception:
            pass
