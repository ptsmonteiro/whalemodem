"""Drives the acceptance scenario against two already-running whale VARA-API
station servers (see whale/vara_server.py):

    1. connect a session between station A and station B
    2. A sends 1 KB to B
    3. roles switch: B sends 1 KB back to A
    4. either station disconnects

Usage:
    python -m whale.vara_server --radio ic705 --mycall STA1 --cmd-port 8300 --data-port 8301
    python -m whale.vara_server --radio ht    --mycall STA2 --cmd-port 8310 --data-port 8311
    python acceptance_test.py --a-cmd 8300 --a-data 8301 --b-cmd 8310 --b-data 8311 \
        --a-call STA1 --b-call STA2
"""

import argparse
import hashlib
import queue
import socket
import sys
import threading
import time


def _transfer_summary(receiver: str, nbytes: int, elapsed: float) -> str:
    """Format elapsed time and net application-payload throughput."""
    net_bps = nbytes * 8 / elapsed
    return (
        f"   {receiver} received {nbytes} bytes in {elapsed:.1f}s "
        f"({net_bps:.1f} bit/s net)"
    )


class StationClient:
    """Owns one command connection + one data connection to a station
    server. The command socket is drained continuously by a background
    thread from the moment it connects -- status lines (PTT ON/OFF, etc.)
    arrive throughout the whole session, including during long data
    transfers when nothing is actively calling wait_for(). Reading them
    only inside wait_for() (as this used to) left them sitting in the OS
    receive buffer for the entire transfer, so a burst of perfectly normal
    ACK traffic from minutes earlier would all print at once, right before
    whatever status line a later wait_for() call happened to be waiting on
    -- indistinguishable at a glance from a real problem at that moment.
    """

    def __init__(self, name, host, cmd_port, data_port):
        self.name = name
        # The 10s bounds the *connect*, so that a station server which never
        # came up fails the run in seconds instead of hanging it. It is
        # dropped immediately afterwards: create_connection leaves the socket
        # in timeout mode for the rest of its life, and a command socket with
        # nothing to say is not a broken one. The reader below has to be free
        # to sit in recv() for as long as the station stays quiet.
        self.cmd = socket.create_connection((host, cmd_port), timeout=10)
        self.cmd.settimeout(None)
        self._lines = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.data_port_addr = (host, data_port)
        self.data = None

    def _read_loop(self):
        buf = b""
        while True:
            try:
                chunk = self.cmd.recv(4096)
            except socket.timeout:
                # Nothing arrived yet; that is not the connection closing.
                # socket.timeout is a subclass of OSError, so before the
                # socket was put back into blocking mode above this fell
                # into the handler below and killed the reader for good
                # after ten seconds of quiet -- every status line from then
                # on went unqueued and wait_for() could only ever time out.
                # Nothing pointed at it: an acceptance run has PTT ON/PTT
                # OFF crossing the socket every few seconds, so recv was
                # never idle that long. Only a scenario that waits through
                # real silence saw it (scripts/hw_half_open_recovery.py,
                # which sits out whale.link.INACTIVITY_TIMEOUT). The socket
                # no longer has a timeout to take, so this cannot fire; it
                # stays because the failure it guards against is silent and
                # permanent, and one settimeout() elsewhere would restore it.
                continue
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\r" in buf or b"\n" in buf:
                for sep in (b"\r\n", b"\r", b"\n"):
                    if sep in buf:
                        line, buf = buf.split(sep, 1)
                        break
                text = line.decode("ascii", "replace")
                if text:
                    print(f"     [{self.name}] status: {text}")
                    self._lines.put(text)

    def send_cmd(self, line):
        print(f"  -> [{self.name}] CMD: {line}")
        self.cmd.sendall((line + "\r").encode("ascii"))

    def wait_for(self, prefix, timeout):
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"[{self.name}] never saw a status line starting with {prefix!r}")
            try:
                text = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"[{self.name}] never saw a status line starting with {prefix!r}")
            if text.startswith(prefix):
                return text

    def open_data(self):
        # Deliberately not given the settimeout(None) the command socket
        # gets. The leftover connect timeout never reaches a recv: recv_data
        # sets its own before every single one. And a timeout on the data
        # socket means the transfer under test did not arrive in its budget,
        # which is a failure the caller should see -- it comes out of
        # recv_data as an exception rather than being swallowed by a
        # background thread.
        self.data = socket.create_connection(self.data_port_addr, timeout=10)

    def send_data(self, payload: bytes):
        self.data.sendall(payload)

    def recv_data(self, nbytes: int, timeout: float) -> bytes:
        self.data.settimeout(timeout)
        buf = bytearray()
        deadline = time.time() + timeout
        while len(buf) < nbytes and time.time() < deadline:
            self.data.settimeout(max(0.1, deadline - time.time()))
            chunk = self.data.recv(nbytes - len(buf))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--a-cmd", type=int, required=True)
    ap.add_argument("--a-data", type=int, required=True)
    ap.add_argument("--b-cmd", type=int, required=True)
    ap.add_argument("--b-data", type=int, required=True)
    ap.add_argument("--a-call", default="STA1")
    ap.add_argument("--b-call", default="STA2")
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--connect-timeout", type=float, default=180.0)
    ap.add_argument("--transfer-timeout", type=float, default=300.0)
    args = ap.parse_args()

    print("== connecting to station servers ==")
    a = StationClient("A", args.host, args.a_cmd, args.a_data)
    b = StationClient("B", args.host, args.b_cmd, args.b_data)

    print("== B: LISTEN ON ==")
    b.send_cmd(f"MYCALL {args.b_call}")
    b.send_cmd("LISTEN ON")

    print("== A: CONNECT to B ==")
    a.send_cmd(f"CONNECT {args.a_call} {args.b_call}")

    a.wait_for("CONNECTED", args.connect_timeout)
    b.wait_for("CONNECTED", args.connect_timeout)
    print("== session established ==")

    a.open_data()
    b.open_data()

    payload_ab = hashlib.sha256(b"whalemodem-A-to-B").digest() * (args.size // 32 + 1)
    payload_ab = payload_ab[:args.size]
    payload_ba = hashlib.sha256(b"whalemodem-B-to-A").digest() * (args.size // 32 + 1)
    payload_ba = payload_ba[:args.size]

    print(f"== A -> B: sending {len(payload_ab)} bytes ==")
    t0 = time.perf_counter()
    a.send_data(payload_ab)
    got = b.recv_data(len(payload_ab), args.transfer_timeout)
    print(_transfer_summary("B", len(got), time.perf_counter() - t0))
    assert got == payload_ab, "A->B payload MISMATCH"
    print("   A->B payload verified byte-for-byte OK")

    print(f"== roles switch: B -> A: sending {len(payload_ba)} bytes ==")
    t0 = time.perf_counter()
    b.send_data(payload_ba)
    got2 = a.recv_data(len(payload_ba), args.transfer_timeout)
    print(_transfer_summary("A", len(got2), time.perf_counter() - t0))
    assert got2 == payload_ba, "B->A payload MISMATCH"
    print("   B->A payload verified byte-for-byte OK")

    print("== A requests DISCONNECT ==")
    a.send_cmd("DISCONNECT")
    a.wait_for("DISCONNECTED", 30.0)
    b.wait_for("DISCONNECTED", 30.0)

    print("\nACCEPTANCE TEST PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"\nACCEPTANCE TEST FAILED: {exc}")
        sys.exit(1)
