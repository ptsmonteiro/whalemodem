"""A VARA-API-shaped TCP front end for whale.link.Link.

Two TCP ports, same shape as VARA HF/FM's API:
  - command port: line-oriented ASCII commands in, status lines out
  - data port: once connected, raw bytes written are sent over the air,
    raw bytes received over the air are written back out

Commands (each line, '\r' or '\n' terminated):
    MYCALL <call>              set our callsign (also settable via --mycall)
    LISTEN ON                  accept an incoming CONNECT
    LISTEN OFF                 stop accepting incoming CONNECTs
    CONNECT <mycall> <dstcall> initiate a connection
    DISCONNECT                 tear down the current connection
    ABORT                      alias for DISCONNECT
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
    CONNECT FAILED
    DISCONNECTED
    BUFFER <n>                 sent after a data-port write; n is always 0 --
                                a known simplification, see _data_reader_loop
    IAMALIVE                   unsolicited keepalive, sent roughly every 60s
                                for the life of the server, connected or not

Not implemented from real VARA's API: compression modes, WINLINK-specific
extensions. This is a v1 built for one thing -- two of our own stations
exchanging bytes -- using VARA's API shape because that shape (two ports,
connect/data-stream/disconnect) is a well-understood target, not because
we're driving real VARA software.
"""

import argparse
import logging
import socket
import threading

from whale import policy
from whale.service import ModemService

logger = logging.getLogger(__name__)

PUMP_RECV_TIMEOUT = 0.5

# How often a pending local data-port accept checks whether its modem
# session is still active.
DATA_ACCEPT_POLL = 0.5

# Real VARA emits an unsolicited IAMALIVE roughly every 60s for the life of
# the session, whether or not it is connected. Module-level so tests can
# monkeypatch it to a short interval instead of waiting on the real cadence.
IAMALIVE_INTERVAL = 60.0


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
            # no channel-derived bandwidth to offer here (StationServer isn't
            # handed the ChannelPolicy, and ChannelPolicy carries no
            # bandwidth-like field to read even if it were), so this falls
            # back to 0 when the client never sent BW<n>.
            bandwidth = self.bandwidth_hz if self.bandwidth_hz is not None else 0
            self._send_status(f"CONNECTED {kw['mycall']} {kw['peer']} {bandwidth}")
        elif name == "CONNECT_FAILED":
            self._send_status("CONNECT FAILED")
        elif name == "DISCONNECTED":
            self._send_status("DISCONNECTED")
            self._close_data_connection()
        if name == "CONNECTED":
            with self._data_lock:
                if not self._data_accepting:
                    self._data_accepting = True
                    threading.Thread(target=self._accept_data_connection, daemon=True).start()

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
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            try:
                self.service.write(chunk)
            except ConnectionError:
                return
            # Real VARA sends BUFFER <n> after an outbound data burst. whale
            # has no cheap, test-verifiable notion of bytes still queued for
            # transmission (the service's outbound queue drains asynchronously
            # and doesn't correspond to over-the-air backlog), so this always
            # reports 0 -- a known simplification, see LINK.md.
            self._send_status("BUFFER 0")

    def _data_writer_loop(self, conn):
        while True:
            data = self.service.read(timeout=PUMP_RECV_TIMEOUT)
            if data is None:
                if self.service.state != "CONNECTED":
                    return
                continue
            try:
                conn.sendall(data)
            except OSError:
                return

    def _accept_data_connection(self):
        """Attach the next local VARA data connection to the active stream."""
        self._data_listener.settimeout(DATA_ACCEPT_POLL)
        while True:
            try:
                conn, addr = self._data_listener.accept()
            except socket.timeout:
                if self.service.state != "CONNECTED":
                    with self._data_lock:
                        self._data_accepting = False
                    return
                continue
            logger.info("data connection from %s", addr)
            with self._data_lock:
                self._data_conn = conn
                self._data_accepting = False
            threading.Thread(target=self._data_reader_loop, args=(conn,), daemon=True).start()
            threading.Thread(target=self._data_writer_loop, args=(conn,), daemon=True).start()
            return

    def _close_data_connection(self):
        with self._data_lock:
            conn, self._data_conn = self._data_conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

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
            self.service.disconnect()
        elif cmd == "CHAT" and len(parts) >= 2:
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

    def stop(self):
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
        self.service.stop()

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
            self.service.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--radio", required=True, help="radio name from the configured inventory")
    ap.add_argument("--radio-config", help="TOML radio inventory (or set WHALE_RADIO_CONFIG)")
    ap.add_argument("--mycall", required=True)
    ap.add_argument("--cmd-port", type=int, default=8300)
    ap.add_argument("--data-port", type=int, default=8301)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--channel", default="vhf-fm", choices=sorted(policy.CHANNELS),
                    help="which channel this station is on: its timeouts, its "
                         "retry budget and the waveforms it offers "
                         "(see whale/policy.py)")
    ap.add_argument("--mode-level", choices=("default", "optional", "experimental"),
                    default="default", help="qualification registry to advertise; "
                    "optional/experimental require explicit operator selection")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    channel = policy.by_name(args.channel)
    from whale.mode_qualification import registry
    mode_registry = registry(args.channel, args.mode_level,
                             channel.max_useful_frame_seconds)
    logger.info("channel: %s", channel.name)
    logger.info("mode qualification level: %s; IDs: %s",
                args.mode_level, mode_registry.supported_ids)
    service = ModemService.for_radio(args.radio, args.mycall,
                                     radio_config=args.radio_config,
                                     policy=channel, mode_registry=mode_registry)
    server = StationServer(service, args.mycall, args.cmd_port, args.data_port, args.host)
    server.serve_forever()


if __name__ == "__main__":
    main()
