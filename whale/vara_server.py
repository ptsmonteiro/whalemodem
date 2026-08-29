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

Status lines pushed back on the command port:
    PTT ON / PTT OFF
    CONNECTED <peer> <mycall>
    CONNECT FAILED
    DISCONNECTED
    BUFFER <n>

Not implemented from real VARA's API: compression modes, bandwidth
selection, WINLINK-specific extensions. This is a v1 built for one thing --
two of our own stations exchanging bytes -- using VARA's API shape because
that shape (two ports, connect/data-stream/disconnect) is a well-understood
target, not because we're driving real VARA software.
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


class StationServer:
    """VARA protocol translation over a transport-independent modem service."""

    def __init__(self, service, mycall, cmd_port, data_port, host="127.0.0.1"):
        self.mycall = mycall
        self.host = host
        self.cmd_port = cmd_port
        self.data_port = data_port

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
            self._send_status(f"CONNECTED {kw['peer']} {kw['mycall']}")
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
        elif cmd == "LISTEN" and len(parts) >= 2:
            if parts[1].upper() == "ON":
                self.service.listen(True)
            else:
                self.service.listen(False)
        elif cmd == "CONNECT" and len(parts) >= 3:
            mycall, dstcall = parts[1], parts[2]
            self.mycall = mycall
            self.service.connect(dstcall, mycall=mycall)
        elif cmd in ("DISCONNECT", "ABORT"):
            self.service.disconnect()
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
