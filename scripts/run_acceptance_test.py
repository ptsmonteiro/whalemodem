"""Runs the end-to-end acceptance test and manages both station-server
processes it needs, so there's one command to run the whole thing instead of
three manual terminals (two `python -m whale.vara_server` + acceptance_test.py).

Starts station A and station B as subprocesses (each logging to logs/, same
as when run by hand), waits for both command ports to accept a connection,
runs acceptance_test.py against them, then tears both servers down --
whether the test passed, failed, or raised.

Each station's environment can be extended with --a-env/--b-env, which is
how the frame-loss and profile hooks in whale/link.py (WHALE_DROP_PTYPE,
WHALE_FORCE_MODE, WHALE_MODE_STEP_SCRIPT -- see the "test affordances" note
there) are driven on the bench. They have to be per station rather than
inherited from this process: the interesting scenarios are asymmetric, and
"the responder loses its MODE_ACK" is not the same run as "both ends lose
one".

Run:
    python scripts/run_acceptance_test.py
    python scripts/run_acceptance_test.py --a-radio ic705 --b-radio ht --size 4096

    # start both legs at 600 baud, have each station step up after its
    # first ACKed chunk, and lose the first MODE_ACK at each end -- i.e.
    # the 600->1200 transition failing in both directions at once
    python scripts/run_acceptance_test.py \
        --a-env WHALE_FORCE_MODE=1 --a-env WHALE_MODE_STEP_SCRIPT=1:up \
        --a-env WHALE_DROP_PTYPE=MODE_ACK \
        --b-env WHALE_FORCE_MODE=1 --b-env WHALE_MODE_STEP_SCRIPT=1:up \
        --b-env WHALE_DROP_PTYPE=MODE_ACK
"""
import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import acceptance_test

PORT_WAIT_TIMEOUT = 15.0
PORT_POLL_INTERVAL = 0.3
SERVER_STOP_TIMEOUT = 10.0


def _wait_for_port(host, port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(PORT_POLL_INTERVAL)
    return False


def _pump_output(name, proc, log_file):
    """Streams a station's stdout to the console (prefixed) and the log file
    in realtime, line by line, until the process closes stdout."""
    for line in iter(proc.stdout.readline, b""):
        text = line.decode(errors="replace")
        sys.stdout.write(f"[{name}] {text}")
        sys.stdout.flush()
        log_file.write(text)
        log_file.flush()


def _env_with(overrides):
    """This process's environment plus `overrides` (a list of NAME=VALUE),
    logged so a run's own log records what it was run with -- an injected
    frame loss that is not written down beside the result is worse than no
    result at all."""
    env = dict(os.environ)
    for item in overrides or ():
        name, _, value = item.partition("=")
        env[name.strip()] = value
    return env


def _start_server(radio, mycall, cmd_port, data_port, host, log_path, name, env_overrides=()):
    log_file = open(log_path, "w")
    if env_overrides:
        header = f"# station {name} environment: {' '.join(env_overrides)}\n"
        print(f"[{name}] {header}", end="")
        log_file.write(header)
        log_file.flush()
    proc = subprocess.Popen(
        [sys.executable, "-m", "whale.vara_server",
         "--radio", radio, "--mycall", mycall,
         "--cmd-port", str(cmd_port), "--data-port", str(data_port),
         "--host", host, "--verbose"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=_env_with(env_overrides),
    )
    pump = threading.Thread(target=_pump_output, args=(name, proc, log_file), daemon=True)
    pump.start()
    return proc, log_file, pump


def _stop_server(name, proc, log_file, pump):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=SERVER_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            print(f"station {name} did not exit after terminate(), killing it")
            proc.kill()
            proc.wait(timeout=5)
    pump.join(timeout=5)
    log_file.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--a-radio", default="ic705", help="radio name from whale/hw/radios.py for station A")
    ap.add_argument("--b-radio", default="ht", help="radio name from whale/hw/radios.py for station B")
    ap.add_argument("--a-call", default="STA1")
    ap.add_argument("--b-call", default="STA2")
    ap.add_argument("--a-cmd", type=int, default=8300)
    ap.add_argument("--a-data", type=int, default=8301)
    ap.add_argument("--b-cmd", type=int, default=8310)
    ap.add_argument("--b-data", type=int, default=8311)
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--connect-timeout", type=float, default=180.0)
    ap.add_argument("--transfer-timeout", type=float, default=300.0)
    ap.add_argument("--log-dir", default=str(ROOT / "logs"))
    ap.add_argument("--a-env", action="append", metavar="NAME=VALUE", default=[],
                    help="extra environment for station A (repeatable)")
    ap.add_argument("--b-env", action="append", metavar="NAME=VALUE", default=[],
                    help="extra environment for station B (repeatable)")
    args = ap.parse_args()

    log_dir = Path(args.log_dir)
    log_dir.mkdir(exist_ok=True)

    print(f"starting station A ({args.a_radio}, {args.a_call}) on cmd={args.a_cmd} data={args.a_data}, "
          f"logging to {log_dir / 'sta1.log'}...")
    proc_a, log_a, pump_a = _start_server(args.a_radio, args.a_call, args.a_cmd, args.a_data, args.host,
                                           log_dir / "sta1.log", "A", args.a_env)
    print(f"starting station B ({args.b_radio}, {args.b_call}) on cmd={args.b_cmd} data={args.b_data}, "
          f"logging to {log_dir / 'sta2.log'}...")
    proc_b, log_b, pump_b = _start_server(args.b_radio, args.b_call, args.b_cmd, args.b_data, args.host,
                                           log_dir / "sta2.log", "B", args.b_env)

    exit_code = 1
    try:
        for name, proc, port in (("A", proc_a, args.a_cmd), ("B", proc_b, args.b_cmd)):
            if proc.poll() is not None:
                print(f"station {name} exited immediately (code {proc.returncode}) -- check its log")
                return 1

        print("waiting for both command ports to come up...")
        a_up = _wait_for_port(args.host, args.a_cmd, PORT_WAIT_TIMEOUT)
        b_up = _wait_for_port(args.host, args.b_cmd, PORT_WAIT_TIMEOUT)
        if not (a_up and b_up):
            print("station server(s) never came up -- check "
                  f"{log_dir / 'sta1.log'}, {log_dir / 'sta2.log'}")
            return 1

        test_argv = [
            "acceptance_test.py",
            "--host", args.host,
            "--a-cmd", str(args.a_cmd), "--a-data", str(args.a_data),
            "--b-cmd", str(args.b_cmd), "--b-data", str(args.b_data),
            "--a-call", args.a_call, "--b-call", args.b_call,
            "--size", str(args.size),
            "--connect-timeout", str(args.connect_timeout),
            "--transfer-timeout", str(args.transfer_timeout),
        ]
        old_argv = sys.argv
        sys.argv = test_argv
        try:
            acceptance_test.main()
            exit_code = 0
        except AssertionError as exc:
            print(f"\nACCEPTANCE TEST FAILED: {exc}")
            exit_code = 1
        except TimeoutError as exc:
            print(f"\nACCEPTANCE TEST FAILED: {exc}")
            exit_code = 1
        finally:
            sys.argv = old_argv
    finally:
        # Only reached once acceptance_test.main() has returned/raised, i.e.
        # after the session's own DISCONNECT handshake already completed and
        # PTT is off -- stopping the servers here doesn't cut off a live
        # transmission in the normal (non-Ctrl-C) case.
        print("stopping station servers...")
        _stop_server("A", proc_a, log_a, pump_a)
        _stop_server("B", proc_b, log_b, pump_b)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
