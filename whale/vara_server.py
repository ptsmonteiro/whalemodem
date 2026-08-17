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
import queue
import socket
import threading

from whale.link import Link, LinkError
from whale.transport import RadioTransport

logger = logging.getLogger(__name__)

PUMP_RECV_TIMEOUT = 0.5

# How often the wait for the local client's data connection comes up for
# air to service the link. Same figure as PUMP_RECV_TIMEOUT and for the
# same reason: it is the granularity at which this station notices anything
# that is not a socket. See _accept_data_connection.
DATA_ACCEPT_POLL = 0.5


class StationServer:
    def __init__(self, radio_name, mycall, cmd_port, data_port, host="127.0.0.1"):
        self.mycall = mycall
        self.host = host
        self.cmd_port = cmd_port
        self.data_port = data_port

        self.transport = RadioTransport(radio_name)
        self.link = Link(self.transport, mycall, on_event=self._on_link_event)

        self._cmd_conn = None
        self._cmd_lock = threading.Lock()
        self._data_conn = None
        self._data_lock = threading.Lock()

        self._listen_enabled = threading.Event()
        self._disconnect_requested = threading.Event()
        self._outbound_q = queue.Queue()
        self._session_thread = None

    # -- command-port notifications --------------------------------------

    def _on_link_event(self, name, **kw):
        if name == "PTT":
            self._send_status("PTT ON" if kw.get("on") else "PTT OFF")
        elif name == "CONNECTED":
            self._send_status(f"CONNECTED {kw['peer']} {kw['mycall']}")
        elif name == "CONNECT_FAILED":
            self._send_status("CONNECT FAILED")
        elif name == "DISCONNECTED":
            self._send_status("DISCONNECTED")

    def _send_status(self, line):
        logger.info("-> %s", line)
        with self._cmd_lock:
            if self._cmd_conn is not None:
                try:
                    self._cmd_conn.sendall((line + "\r").encode("ascii"))
                except OSError:
                    pass

    # -- data port ----------------------------------------------------------

    def _data_write(self, data: bytes):
        with self._data_lock:
            if self._data_conn is not None:
                try:
                    self._data_conn.sendall(data)
                except OSError:
                    pass

    def _data_reader_loop(self, conn):
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            self._outbound_q.put(chunk)

    # -- the session pump: alternates opportunistic send / receive ---------

    def _pump_until_disconnected(self):
        while True:
            if self._disconnect_requested.is_set():
                self.link.disconnect()
                self._disconnect_requested.clear()
                return
            try:
                data = self._outbound_q.get_nowait()
            except queue.Empty:
                data = None
            if data:
                # Coalesce whatever else is already queued so send_message
                # gets a full buffer to split into chunk_size-sized frames,
                # instead of one small frame per raw recv() burst.
                pending = [data]
                while True:
                    try:
                        pending.append(self._outbound_q.get_nowait())
                    except queue.Empty:
                        break
                data = b"".join(pending)
                try:
                    self.link.send_message(data)
                except LinkError as exc:
                    logger.warning("send failed: %s", exc)
                    return
                continue
            try:
                msg = self.link.recv_message(timeout=PUMP_RECV_TIMEOUT)
            except LinkError:
                return
            if msg is not None:
                self._data_write(msg)
            if self.link.state != "CONNECTED":
                return

    def _accept_data_connection(self):
        """Waits for the local client to open the data port, servicing the
        radio link while it does. Returns the connection, or None if the
        session ended before anyone turned up.

        This used to be a bare blocking accept(), and that was where a
        half-open session lodged. Between going CONNECTED and the client
        connecting, nothing on this station read anything off the air: a
        caller whose CONNECT_ACK was lost retried into that window, got no
        answer, gave up and went IDLE -- while this station stayed
        CONNECTED with accept() having no timeout and Link, at the time,
        having none either. Polling instead lets Link.service_while_idle
        re-answer the handshake, and failing that notice the peer has
        gone."""
        self._data_listener.settimeout(DATA_ACCEPT_POLL)
        while True:
            try:
                conn, addr = self._data_listener.accept()
            except socket.timeout:
                if not self.link.service_while_idle():
                    logger.info("session ended before the data port was opened")
                    return None
                continue
            logger.info("data connection from %s", addr)
            return conn

    def _run_session(self, start_fn):
        """start_fn() blocks until CONNECTED (True) or gives up (False)."""
        ok = start_fn()
        if not ok:
            return
        data_conn = self._accept_data_connection()
        if data_conn is None:
            return
        with self._data_lock:
            self._data_conn = data_conn
        reader = threading.Thread(target=self._data_reader_loop, args=(data_conn,), daemon=True)
        reader.start()
        try:
            self._pump_until_disconnected()
        finally:
            # The pump can return with the link still CONNECTED -- that is
            # what a LinkError from send_message looks like, a peer that
            # stopped answering mid-transfer. Nothing would then be calling
            # recv_message or service_while_idle, so the station would sit
            # CONNECTED with no thread left that could ever notice, which
            # is the same half-open state by a different route. One
            # best-effort DISC ends it; disconnect() is a no-op if the
            # session is already down.
            self.link.disconnect(retries=1)
            with self._data_lock:
                self._data_conn = None
            try:
                data_conn.close()
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
            self.link.mycall = parts[1]
        elif cmd == "LISTEN" and len(parts) >= 2:
            if parts[1].upper() == "ON":
                if self._session_thread is None or not self._session_thread.is_alive():
                    self._session_thread = threading.Thread(
                        target=self._run_session, args=(self._accept_incoming,), daemon=True)
                    self._session_thread.start()
            else:
                self._listen_enabled.clear()
        elif cmd == "CONNECT" and len(parts) >= 3:
            mycall, dstcall = parts[1], parts[2]
            self.mycall = mycall
            self.link.mycall = mycall
            self._session_thread = threading.Thread(
                target=self._run_session, args=(lambda: self.link.connect(dstcall),), daemon=True)
            self._session_thread.start()
        elif cmd in ("DISCONNECT", "ABORT"):
            self._disconnect_requested.set()
        else:
            logger.warning("unknown command: %r", line)

    def _accept_incoming(self):
        self._listen_enabled.set()
        while self._listen_enabled.is_set():
            peer = self.link.listen_once(timeout=2.0)
            if peer is not None:
                return True
        return False

    def _cmd_conn_loop(self, conn):
        buf = b""
        with self._cmd_lock:
            self._cmd_conn = conn
        try:
            while True:
                chunk = conn.recv(4096)
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

    def serve_forever(self):
        self.link.start()
        cmd_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cmd_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cmd_listener.bind((self.host, self.cmd_port))
        cmd_listener.listen(1)

        self._data_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._data_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._data_listener.bind((self.host, self.data_port))
        self._data_listener.listen(1)

        logger.info("whale VARA-API server: mycall=%s cmd=%d data=%d",
                    self.mycall, self.cmd_port, self.data_port)
        while True:
            conn, addr = cmd_listener.accept()
            logger.info("command connection from %s", addr)
            self._cmd_conn_loop(conn)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--radio", required=True, help="radio name from shark/hw/radios.py (ic705, ht, ic7300)")
    ap.add_argument("--mycall", required=True)
    ap.add_argument("--cmd-port", type=int, default=8300)
    ap.add_argument("--data-port", type=int, default=8301)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    server = StationServer(args.radio, args.mycall, args.cmd_port, args.data_port, args.host)
    server.serve_forever()


if __name__ == "__main__":
    main()
