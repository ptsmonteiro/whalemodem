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
import socket
import sys
import time


class StationClient:
    def __init__(self, host, cmd_port, data_port):
        self.cmd = socket.create_connection((host, cmd_port), timeout=10)
        self.cmd.settimeout(0.5)
        self._cmd_buf = b""
        self.data_port_addr = (host, data_port)
        self.data = None

    def send_cmd(self, line):
        print(f"  -> CMD: {line}")
        self.cmd.sendall((line + "\r").encode("ascii"))

    def wait_for(self, prefix, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            while b"\r" in self._cmd_buf or b"\n" in self._cmd_buf:
                for sep in (b"\r\n", b"\r", b"\n"):
                    if sep in self._cmd_buf:
                        line, self._cmd_buf = self._cmd_buf.split(sep, 1)
                        break
                text = line.decode("ascii", "replace")
                if text:
                    print(f"     status: {text}")
                if text.startswith(prefix):
                    return text
            try:
                chunk = self.cmd.recv(4096)
                if chunk:
                    self._cmd_buf += chunk
            except socket.timeout:
                pass
        raise TimeoutError(f"never saw a status line starting with {prefix!r}")

    def open_data(self):
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
    a = StationClient(args.host, args.a_cmd, args.a_data)
    b = StationClient(args.host, args.b_cmd, args.b_data)

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
    t0 = time.time()
    a.send_data(payload_ab)
    got = b.recv_data(len(payload_ab), args.transfer_timeout)
    print(f"   B received {len(got)} bytes in {time.time()-t0:.1f}s")
    assert got == payload_ab, "A->B payload MISMATCH"
    print("   A->B payload verified byte-for-byte OK")

    print(f"== roles switch: B -> A: sending {len(payload_ba)} bytes ==")
    t0 = time.time()
    b.send_data(payload_ba)
    got2 = a.recv_data(len(payload_ba), args.transfer_timeout)
    print(f"   A received {len(got2)} bytes in {time.time()-t0:.1f}s")
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
