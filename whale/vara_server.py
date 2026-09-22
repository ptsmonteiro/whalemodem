"""A VARA-API-shaped TCP front end for whale.link.Link.

Two TCP ports, same shape as VARA HF/FM's API:
  - command port: line-oriented ASCII commands in, status lines out
  - data port: raw bytes written are sent over the air, raw bytes received
    over the air are written back out. A decoded message is handed over
    while the link is still keying its ACK, so it reaches the client
    between that burst's PTT ON and PTT OFF, as real VARA's does. The connection is accepted for the
    life of the server rather than per radio session: real VARA keeps it
    attached across connect/disconnect cycles (its clients send their init
    sequence exactly once per capture). Bytes written while no session is up
    cannot be transmitted and are dropped; no capture establishes buffering
    here. Bytes already received over the air outlive their session's
    teardown and are written out on this same connection.

Commands (each line, '\r' or '\n' terminated):
    MYCALL <call>              override the configured station callsign
    LISTEN ON                  accept an incoming CONNECT
    LISTEN OFF                 stop accepting incoming CONNECTs
    CONNECT <mycall> <dstcall> initiate a connection
    DISCONNECT                 send queued data, then tear down the connection
    ABORT                      discard queued outbound data, then tear down
                               immediately (bytes already received over the
                               air are still delivered)
    CHAT ON / CHAT OFF         set chat mode (accepted; no behavior change yet)
    BW<n>                      set bandwidth in Hz, e.g. BW2300 (no space)

Status lines pushed back on the command port:
    OK                         acknowledges MYCALL, CONNECT, DISCONNECT/ABORT,
                                CHAT ON/OFF, and BW<n>
    PTT ON / PTT OFF
    CONNECTED <mycall> <peer> <bandwidth>
                                bandwidth is the value set by a prior BW<n>,
                                or 0 if none was ever sent (see BW<n> above;
                                whale has no channel-derived bandwidth to
                                fall back on -- see _on_modem_event)
    DISCONNECTED                 sent after established teardown or an
                                 exhausted outbound connection attempt
    SN <x.x>                   receive SNR of a decoded DATA burst, sent
                                just before its BITRATE ... RX line
    BITRATE (<id>)  <n> bps <TX|RX>
                                the mode a DATA burst is keyed or decoded at:
                                whale's own mode_id, and that mode's net
                                application bit/s (docs/MODES.md), not a
                                measured channel throughput. Control frames
                                (ACKs and the handshake) report nothing.
    WHALE PROGRESS <TX|RX> <bytes> [<total>]
                                acknowledged TX or decoded RX message progress
    BUFFER 0                   sent after queued application data finishes a
                                link send; nonzero semantics are unconfirmed
    IAMALIVE                   unsolicited keepalive, sent roughly every 60s
                                for the life of the server, connected or not

Not implemented from real VARA's API: compression modes or WINLINK-session
extensions (the available captures establish neither command syntax nor
semantics). They remain on the unknown-command path: no state is changed and
no success acknowledgement is sent. This is
a v1 built for one thing -- two of our own stations
exchanging bytes -- using VARA's API shape because that shape (two ports,
connect/data-stream/disconnect) is a well-understood target, not because
we're driving real VARA software.
"""

import argparse
import logging
import signal
import socket
import threading

from whale import policy
from whale.config import app_config, get_radio
from whale.service import ModemService

logger = logging.getLogger(__name__)

#: How long a signalled stop waits for the session to end politely before
#: giving up on the courtesy. Matches whale-test's own grace period: it is
#: the same operator, pressing the same key, wanting the same thing.
STOP_GRACE = 30.0

PUMP_RECV_TIMEOUT = 0.5

# How often a pending local data-port accept wakes to check for shutdown.
DATA_ACCEPT_POLL = 0.5

# Real VARA emits an unsolicited IAMALIVE roughly every 60s for the life of
# the session, whether or not it is connected. Module-level so tests can
# monkeypatch it to a short interval instead of waiting on the real cadence.
IAMALIVE_INTERVAL = 60.0


def _close_socket(conn):
    if conn is None:
        return
    try:
        conn.close()
    except OSError:
        pass


class StationServer:
    """VARA protocol translation over a transport-independent modem service."""

    def __init__(self, service, mycall, cmd_port, data_port, host="127.0.0.1"):
        self.mycall = mycall
        self.host = host
        self.cmd_port = cmd_port
        self.data_port = data_port

        self.chat_mode = False
        self.bandwidth_hz = None

        self.service = service
        self.service.subscribe(self._on_modem_event)

        self._cmd_conn = None
        self._cmd_lock = threading.Lock()
        self._data_conn = None
        self._data_lock = threading.Lock()

        self._data_accepting = False
        self._stopping = threading.Event()
        self.ready = threading.Event()
        self._cmd_listener = None
        self._data_listener = None

    # -- command-port notifications --------------------------------------

    def _on_modem_event(self, name, **kw):
        if name == "PTT":
            self._send_status("PTT ON" if kw.get("on") else "PTT OFF")
        elif name == "CONNECTED":
            # Real VARA: CONNECTED <mycall> <dstcall> <bandwidth>. whale has
            # only outbound-caller capture evidence for that ordering. For
            # an incoming connection, Link supplies the accepting station as
            # mycall and the caller as peer; use the same local/peer shape
            # until an accepting-side capture establishes otherwise.
            #
            # no channel-derived bandwidth to offer here (StationServer isn't
            # handed the ChannelPolicy, and ChannelPolicy carries no
            # bandwidth-like field to read even if it were), so this falls
            # back to 0 when the client never sent BW<n>.
            bandwidth = self.bandwidth_hz if self.bandwidth_hz is not None else 0
            self._send_status(f"CONNECTED {kw['mycall']} {kw['peer']} {bandwidth}")
        elif name == "CONNECT_FAILED":
            # A failed outbound attempt is an internal distinction. VARA HF
            # 4.3.0 reports the exhausted attempt as DISCONNECTED on its
            # command port (capture-conn-fail.log), with no CONNECT FAILED
            # line, even though no CONNECTED status preceded it.
            self._send_status("DISCONNECTED")
        elif name == "DISCONNECTED":
            self._send_status("DISCONNECTED")
        elif name == "SNR":
            # Real VARA reports the receive SNR of a decoded data burst as
            # "SN <x.x>" just before the mode line (capture-file-transfer-
            # official-vara.log). The unit is not stated in any capture;
            # whale sends its own decode's dB figure.
            self._send_status(f"SN {kw['snr_db']:.1f}")
        elif name == "BITRATE":
            # Captured shape: "BITRATE (4)  175 bps TX" and
            # "BITRATE (3)  82 bps TX" -- two literal spaces after the
            # parenthesised index in both, so the gap is fixed, not a
            # right-aligned field. The index is VARA's own mode numbering,
            # which whale has no mapping for, so it carries whale's mode_id;
            # the rate is the mode's net application bit/s (see
            # link.net_bits_per_second and docs/MODES.md), not a measured
            # channel throughput.
            self._send_status(f"BITRATE ({kw['mode_id']})  "
                              f"{round(kw['bits_per_second'])} bps "
                              f"{kw['direction']}")
        elif name == "TRANSFER_PROGRESS":
            fields = ["WHALE", "PROGRESS", kw["direction"],
                      str(kw["transferred"])]
            if kw.get("total") is not None:
                fields.append(str(kw["total"]))
            self._send_status(" ".join(fields))
        elif name == "OUTBOUND_DRAINED":
            # All five captured BUFFER reports were zero and followed the
            # data-bearing TX burst.  Do not invent uncaptured units or a
            # nonzero range; translate only the service's empty boundary.
            self._send_status("BUFFER 0")

    def _iamalive_loop(self):
        """Send IAMALIVE roughly every IAMALIVE_INTERVAL seconds.

        Runs for the life of the server, independent of connection state,
        matching real VARA. Uses self._stopping.wait() rather than
        time.sleep() so stop() interrupts it immediately instead of after
        up to a full interval.
        """
        while not self._stopping.wait(IAMALIVE_INTERVAL):
            if self._cmd_conn is not None:
                self._send_status("IAMALIVE")

    def _send_status(self, line):
        logger.info("-> %s", line)
        with self._cmd_lock:
            if self._cmd_conn is not None:
                try:
                    self._cmd_conn.sendall((line + "\r").encode("ascii"))
                except OSError:
                    pass

    # -- data port ----------------------------------------------------------

    def _data_reader_loop(self, conn):
        """Feed data-port bytes to the service for the life of ``conn``.

        The connection outlives radio sessions, so a write that lands while
        no session is up must not end this loop. ModemService.write() raises
        ConnectionError in that case; those bytes cannot be transmitted and
        are dropped. No capture establishes what real VARA does with
        data-port bytes written between sessions, so nothing is buffered on
        their behalf.
        """
        while not self._stopping.is_set():
            try:
                chunk = conn.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            try:
                self.service.write(chunk)
            except ConnectionError:
                logger.debug("dropped %d data-port bytes: no active session",
                             len(chunk))
        self._detach_data_connection(conn)

    def _data_writer_loop(self, conn):
        """Write inbound stream bytes to ``conn`` for the life of ``conn``.

        Like the reader, this survives session teardown: it ends only when
        the connection is closed or replaced, or the server stops.
        """
        while not self._stopping.is_set():
            with self._data_lock:
                if self._data_conn is not conn:
                    return
            data = self.service.read(timeout=PUMP_RECV_TIMEOUT)
            if data is None:
                continue
            try:
                conn.sendall(data)
            except OSError:
                break
        self._detach_data_connection(conn)

    def _accept_data_connection(self):
        """Accept local VARA data connections for the life of the server.

        Real VARA's data port is not tied to a radio session: in every
        capture the client sends its cold-start init sequence exactly once,
        and connect/disconnect cycles happen underneath a data socket that is
        never dropped (in capture1.log the client ends the session with ABORT
        and re-inits nothing afterwards). So this loop starts when the server
        starts serving and keeps an accept pending at all times, leaving no
        window between sessions where the listener is unattended.

        One data connection is attached at a time; a new one replaces and
        closes its predecessor.
        """
        self._data_listener.settimeout(DATA_ACCEPT_POLL)
        try:
            while not self._stopping.is_set():
                try:
                    conn, addr = self._data_listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                logger.info("data connection from %s", addr)
                with self._data_lock:
                    previous, self._data_conn = self._data_conn, conn
                _close_socket(previous)
                threading.Thread(target=self._data_reader_loop, args=(conn,),
                                 daemon=True).start()
                threading.Thread(target=self._data_writer_loop, args=(conn,),
                                 daemon=True).start()
        finally:
            with self._data_lock:
                self._data_accepting = False

    def _detach_data_connection(self, conn):
        """Close ``conn``, clearing it if it is still the attached one."""
        with self._data_lock:
            if self._data_conn is conn:
                self._data_conn = None
        _close_socket(conn)

    def _close_data_connection(self):
        with self._data_lock:
            conn, self._data_conn = self._data_conn, None
        _close_socket(conn)

    # -- command handling -------------------------------------------------

    def _handle_command(self, line: str):
        parts = line.strip().split()
        if not parts:
            return
        cmd = parts[0].upper()
        if cmd == "MYCALL" and len(parts) >= 2:
            self.mycall = parts[1]
            self.service.set_callsign(parts[1])
            self._send_status("OK")
        elif cmd == "LISTEN" and len(parts) >= 2:
            # Not observed acking with OK in the capture; left unconfirmed.
            if parts[1].upper() == "ON":
                self.service.listen(True)
            else:
                self.service.listen(False)
        elif cmd == "CONNECT" and len(parts) >= 3:
            mycall, dstcall = parts[1], parts[2]
            self.mycall = mycall
            self.service.connect(dstcall, mycall=mycall)
            self._send_status("OK")
        elif cmd in ("DISCONNECT", "ABORT"):
            # Real VARA acks ABORT immediately, before teardown completes --
            # DISCONNECTED follows later, once the actual handshake with the
            # peer finishes. Send OK first so the ordering matches regardless
            # of how long service.disconnect() takes.
            self._send_status("OK")
            if cmd == "ABORT":
                self.service.abort()
            else:
                self.service.disconnect()
        elif (cmd == "CHAT" and len(parts) == 2
              and parts[1].upper() in ("ON", "OFF")):
            # CHAT ON is observed and CHAT OFF is retained as its conventional
            # symmetric setting.  Neither setting transforms stream bytes:
            # the captures cannot establish whether the chat application's
            # decimal length prefix is interpreted by VARA at all.
            self.chat_mode = parts[1].upper() == "ON"
            self._send_status("OK")
        elif cmd.startswith("BW") and len(cmd) > 2 and cmd[2:].isdigit():
            self.bandwidth_hz = int(cmd[2:])
            self._send_status("OK")
        else:
            logger.warning("unknown command: %r", line)

    def _cmd_conn_loop(self, conn):
        buf = b""
        with self._cmd_lock:
            self._cmd_conn = conn
        try:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    # stop() deliberately shuts down and closes this socket
                    # to wake a thread blocked in recv(). EBADF/ENOTCONN from
                    # that close is normal shutdown, but an I/O failure while
                    # the server is live must still be reported.
                    if self._stopping.is_set():
                        return
                    raise
                if not chunk:
                    return
                buf += chunk
                while b"\r" in buf or b"\n" in buf:
                    for sep in (b"\r\n", b"\r", b"\n"):
                        if sep in buf:
                            line, buf = buf.split(sep, 1)
                            break
                    self._handle_command(line.decode("ascii", "replace"))
        finally:
            with self._cmd_lock:
                self._cmd_conn = None

    # -- server bootstrap ---------------------------------------------------

    def stop(self, *, graceful: bool = True, timeout: float | None = None):
        """Stop accepting clients and release the service and TCP sockets.

        Production normally ends this server by terminating its process, but
        an explicit lifecycle makes the exact same server usable by an
        in-process end-to-end test (and by embedders) without leaking daemon
        threads or listening sockets.
        """
        self._stopping.set()
        self._close_data_connection()
        with self._cmd_lock:
            cmd_conn, self._cmd_conn = self._cmd_conn, None
        if cmd_conn is not None:
            try:
                cmd_conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                cmd_conn.close()
            except OSError:
                pass
        for listener in (self._cmd_listener, self._data_listener):
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
        return self.service.stop(graceful=graceful, timeout=timeout)

    def serve_forever(self):
        self.service.start()
        self._stopping.clear()
        self.ready.clear()
        self._cmd_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._cmd_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._cmd_listener.bind((self.host, self.cmd_port))
        self._cmd_listener.listen(1)
        self._cmd_listener.settimeout(DATA_ACCEPT_POLL)
        self.cmd_port = self._cmd_listener.getsockname()[1]

        self._data_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._data_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._data_listener.bind((self.host, self.data_port))
        self._data_listener.listen(1)
        self.data_port = self._data_listener.getsockname()[1]

        with self._data_lock:
            self._data_accepting = True
        threading.Thread(target=self._accept_data_connection, daemon=True).start()

        logger.info("whale VARA-API server: mycall=%s cmd=%d data=%d",
                    self.mycall, self.cmd_port, self.data_port)
        threading.Thread(target=self._iamalive_loop, daemon=True).start()
        self.ready.set()
        try:
            while not self._stopping.is_set():
                try:
                    conn, addr = self._cmd_listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stopping.is_set():
                        break
                    raise
                logger.info("command connection from %s", addr)
                self._cmd_conn_loop(conn)
        finally:
            self.ready.clear()
            if not self._stopping.is_set():
                # Only when the loop ended on its own. stop() has already
                # stopped the service, and may have stopped it with
                # graceful=False; a second, defaulted stop() here would set
                # the graceful flag back to True under a worker that is
                # still finishing a burst, and so put the DISC that was
                # explicitly not wanted back on the air.
                self.service.stop()


def stop_on_signals(server, grace: float = STOP_GRACE):
    """Stop `server` -- unkeyed -- when this process is asked to end.

    Terminating the process is how this server is normally stopped, by an
    operator's Ctrl-C and by whale-test, which runs it as a child. Left to
    the default handlers, SIGTERM ends the interpreter outright: PTT is
    never taken down, the radio is never closed, and a transmitter that was
    keyed at that moment stays keyed. Running stop() instead does both.

    The stop escalates, the same way whale-test's own teardown does, because
    the two things an operator can mean by Ctrl-C are different and only
    they know which one it is:

      1. The first signal asks for a graceful stop and waits `grace`
         seconds for it. A session still up is ended with a parting DISC --
         one more keying, but it saves the far end waiting out its own
         inactivity timeout.
      2. A second signal, or a first that ran out its grace, stops the
         service without the courtesy: off the air now, peer times out.
      3. The handlers are then put back the way they were found, so a third
         signal ends the interpreter outright. That is still not a keyed
         radio left behind: RadioTransport registers an atexit un-key for
         exactly this, and a KeyboardInterrupt unwinds through it.

    SIGINT is included because the KeyboardInterrupt it raises by default
    unwinds through serve_forever's finally, which stops the service without
    ever passing through this ladder. SIGBREAK is Windows' equivalent of a
    signal a parent can send, and is how whale-test stops its child.

    The interrupt is re-raised after each stop so that the signal is not
    swallowed if it lands before the accept loop has begun -- during the
    seconds the radio takes to open, say, where a stop that only set the
    stopping flag would be cleared again by serve_forever a moment later.
    """
    installed = {}
    asked = []

    def restore():
        for number, handler in installed.items():
            try:
                signal.signal(number, handler)
            except (OSError, ValueError):
                pass

    def stop(signum, frame):
        if asked:
            # Pressed again while the first stop was still being polite.
            # Nothing more is owed the far end, and nothing more is owed
            # this handler either: put the defaults back so the operator
            # cannot be made to ask a third time.
            logger.info("signal %s again: stopping now", signum)
            restore()
            server.stop(graceful=False)
            raise KeyboardInterrupt
        asked.append(signum)
        logger.info("signal %s: stopping", signum)
        if not server.stop(graceful=True, timeout=grace):
            logger.warning("the session did not end within %.0fs -- stopping now", grace)
            restore()
            server.stop(graceful=False)
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            installed[number] = signal.signal(number, stop)
        except (OSError, ValueError):  # not the main thread, or not supported
            logger.debug("no %s handler installed", name, exc_info=True)


def main(argv=None):
    from whale.version import __version__, add_version_argument

    ap = argparse.ArgumentParser(description=__doc__)
    add_version_argument(ap)
    ap.add_argument("--radio", help="radio name (default: the configured channel default)")
    ap.add_argument("--config", help="application configuration TOML (or set WHALE_CONFIG)")
    ap.add_argument("--cmd-port", type=int, help="override the configured command port")
    ap.add_argument("--data-port", type=int, help="override the configured data port")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--channel", default="fm", choices=sorted(policy.CHANNELS),
                    help="which channel this station is on: its timeouts, its "
                         "retry budget and the waveforms it offers "
                         "(see whale/policy.py)")
    ap.add_argument("--mode-level", choices=("default", "optional", "experimental"),
                    default="default", help="qualification registry to advertise; "
                    "optional/experimental require explicit operator selection")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--tui", action="store_true",
                    help="run a live curses status dashboard instead of logging to stderr")
    ap.add_argument("--log-file", help="override the configured log file (default: stderr)")
    args = ap.parse_args(argv)
    try:
        config = app_config(args.config)
        radio = get_radio(args.radio, args.channel, args.config)
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    radio_name = radio.id
    mycall = config.station_callsign
    cmd_port = args.cmd_port if args.cmd_port is not None else config.cmd_port
    data_port = args.data_port if args.data_port is not None else config.data_port
    log_file = args.log_file if args.log_file is not None else config.log_file
    if not 1 <= cmd_port <= 65535 or not 1 <= data_port <= 65535:
        ap.error("command and data ports must be between 1 and 65535")
    if cmd_port == data_port:
        ap.error("command and data ports must be different")

    if not args.tui:
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                            filename=log_file)

        logger.info("Whale %s", __version__)
        channel = policy.by_name(args.channel)
        from whale.mode_qualification import registry
        mode_registry = registry(args.channel, args.mode_level,
                                 channel.max_useful_frame_seconds)
        logger.info("channel: %s", channel.name)
        logger.info("mode qualification level: %s; IDs: %s",
                    args.mode_level, mode_registry.supported_ids)
        service = ModemService.for_radio(radio_name, mycall,
                                         radio_config=args.config,
                                         policy=channel, mode_registry=mode_registry)
        server = StationServer(service, mycall, cmd_port, data_port, args.host)
        stop_on_signals(server)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            # The handler above has already stopped the server, unkeyed; all
            # that is left is to exit quietly rather than on a traceback.
            logger.info("stopped")
        return

    # --tui: an additive branch. It builds the same channel/radio/mode
    # registry/service/server as above, but runs the server's accept loop on
    # a background thread and drives a curses dashboard on the main thread
    # instead of logging to stderr. See whale/modem_tui.py.
    import curses

    from whale import modem_tui

    channel = policy.by_name(args.channel)
    from whale.mode_qualification import registry
    mode_registry = registry(args.channel, args.mode_level,
                             channel.max_useful_frame_seconds)

    state = modem_tui.TuiState(mycall, radio_name, args.channel, mode_registry=mode_registry)
    handlers = [modem_tui.LogTap(state)]
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handlers.append(file_handler)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                         handlers=handlers, force=True)
    logger.info("channel: %s", channel.name)
    logger.info("mode qualification level: %s; IDs: %s",
                args.mode_level, mode_registry.supported_ids)

    service = ModemService.for_radio(radio_name, mycall,
                                     radio_config=args.config,
                                     policy=channel, mode_registry=mode_registry)
    server = StationServer(service, mycall, cmd_port, data_port, args.host)
    unsubscribe = service.subscribe(state.on_event)
    server_thread = threading.Thread(target=server.serve_forever, name="vara-server", daemon=True)
    server_thread.start()
    try:
        curses.wrapper(modem_tui.run, state, server, service, server_thread)
    finally:
        unsubscribe()
        server.stop()
        server_thread.join(timeout=5.0)


if __name__ == "__main__":
    main()
