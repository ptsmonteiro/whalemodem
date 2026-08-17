"""Kills one station mid-session and times how long the other takes to
give up -- the bench check on whale.link.INACTIVITY_TIMEOUT.

Everything else that ends a session is a message from the peer: a DISC, a
DISC_ACK, a caller's parting DISC, a run of unanswered retransmits. This
scenario removes all of them at once. The peer's process is killed while
the session is up and idle, so nothing is ever sent again in either
direction and the surviving station has no evidence of any kind to work
from except the passage of time.

That is the only situation the inactivity timeout is actually for, and it
is not reachable from the acceptance test: DISCONNECT and ABORT both send a
DISC, which is precisely the signal being removed here.

The station is killed while the surviving side is *idle* rather than
mid-transfer, deliberately. Mid-transfer, ARQ notices first -- MAX_RETRIES
unanswered retransmits raise a LinkError in a bit under a minute and
vara_server tears the session down on that. Idle, there is nothing waiting
on anything, and before INACTIVITY_TIMEOUT existed the station stayed
CONNECTED for as long as the process lived.

Run:
    python scripts/hw_half_open_recovery.py
    python scripts/hw_half_open_recovery.py --kill a     # kill the caller instead
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acceptance_test import StationClient  # noqa: E402
from whale import link  # noqa: E402

from run_acceptance_test import _start_server, _stop_server, _wait_for_port  # noqa: E402

PORT_WAIT_TIMEOUT = 15.0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--a-radio", default="ic705")
    ap.add_argument("--b-radio", default="ht")
    ap.add_argument("--a-call", default="STA1")
    ap.add_argument("--b-call", default="STA2")
    ap.add_argument("--a-cmd", type=int, default=8300)
    ap.add_argument("--a-data", type=int, default=8301)
    ap.add_argument("--b-cmd", type=int, default=8310)
    ap.add_argument("--b-data", type=int, default=8311)
    ap.add_argument("--kill", choices=("a", "b"), default="b",
                    help="which station's process to kill mid-session")
    ap.add_argument("--size", type=int, default=128,
                    help="bytes to exchange before the kill, to prove the session was live")
    ap.add_argument("--connect-timeout", type=float, default=120.0)
    ap.add_argument("--log-dir", default=str(ROOT / "logs" / "d_half_open"))
    args = ap.parse_args()

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    servers = {
        "a": _start_server(args.a_radio, args.a_call, args.a_cmd, args.a_data, args.host,
                           log_dir / "sta1.log", "A"),
        "b": _start_server(args.b_radio, args.b_call, args.b_cmd, args.b_data, args.host,
                           log_dir / "sta2.log", "B"),
    }
    survivor_name = "a" if args.kill == "b" else "b"
    exit_code = 1
    try:
        for port in (args.a_cmd, args.b_cmd):
            if not _wait_for_port(args.host, port, PORT_WAIT_TIMEOUT):
                print(f"station on port {port} never came up -- check {log_dir}")
                return 1

        a = StationClient("A", args.host, args.a_cmd, args.a_data)
        b = StationClient("B", args.host, args.b_cmd, args.b_data)
        clients = {"a": a, "b": b}
        # StationClient builds its command socket with
        # socket.create_connection(timeout=10), which leaves the socket in
        # timeout mode -- so its reader thread takes a socket.timeout, sees
        # an OSError and quietly exits after ten seconds of silence. The
        # acceptance test never notices because PTT status lines arrive
        # every few seconds throughout. This scenario is nothing *but*
        # silence, for INACTIVITY_TIMEOUT, so the reader has to be allowed
        # to block indefinitely or the status line being waited for is
        # never read at all.
        for client in clients.values():
            client.cmd.settimeout(None)

        print("== B: LISTEN ON ==")
        b.send_cmd(f"MYCALL {args.b_call}")
        b.send_cmd("LISTEN ON")
        print("== A: CONNECT to B ==")
        a.send_cmd(f"CONNECT {args.a_call} {args.b_call}")
        a.wait_for("CONNECTED", args.connect_timeout)
        b.wait_for("CONNECTED", args.connect_timeout)
        a.open_data()
        b.open_data()

        payload = bytes((i * 13 + 7) % 256 for i in range(args.size))
        print(f"== A -> B: {len(payload)} bytes, to prove the session is live ==")
        a.send_data(payload)
        got = b.recv_data(len(payload), 180.0)
        assert got == payload, f"payload mismatch before the kill ({len(got)}/{len(payload)})"
        print("   payload verified; session is up and now goes idle")

        # Let the surviving station settle into its idle recv_message loop
        # before the peer vanishes, so the clock below measures the
        # inactivity backstop and not the tail of a transfer.
        time.sleep(5)

        print(f"== killing station {args.kill.upper()} ==")
        proc, log_file, pump = servers[args.kill]
        proc.kill()
        proc.wait(timeout=10)
        killed_at = time.time()

        budget = link.INACTIVITY_TIMEOUT + 60
        print(f"== waiting up to {budget:.0f}s for station {survivor_name.upper()} to give up "
              f"(INACTIVITY_TIMEOUT is {link.INACTIVITY_TIMEOUT:.0f}s) ==")
        clients[survivor_name].wait_for("DISCONNECTED", budget)
        elapsed = time.time() - killed_at
        print(f"\nstation {survivor_name.upper()} went DISCONNECTED {elapsed:.1f}s after its "
              f"peer was killed")
        if elapsed > link.INACTIVITY_TIMEOUT + 30:
            print("HALF-OPEN RECOVERY FAILED: took longer than the timeout plus its teardown")
        else:
            print("HALF-OPEN RECOVERY PASSED")
            exit_code = 0
    except (AssertionError, TimeoutError) as exc:
        print(f"\nHALF-OPEN RECOVERY FAILED: {exc}")
    finally:
        print("stopping station servers...")
        for name, (proc, log_file, pump) in servers.items():
            _stop_server(name.upper(), proc, log_file, pump)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
