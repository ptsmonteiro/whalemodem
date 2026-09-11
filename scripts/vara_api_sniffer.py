"""Transparent TCP proxy that logs traffic between a VARA client (terminal
app) and one or more real VARA instances, so the wire-level command/status/
data traffic can be captured and turned into a spec-compatibility gap list
against whale.vara_server.

Point your VARA terminal app at the proxy's listen ports instead of at VARA
directly. Every byte in both directions is logged with a timestamp, a label,
and a direction, to stdout and optionally to a file.

Usage example (two VARA instances at their default port pairs):

    python scripts/vara_api_sniffer.py \
        --pair vara1-cmd:18300:127.0.0.1:8300 \
        --pair vara1-data:18301:127.0.0.1:8301 \
        --pair vara2-cmd:18310:127.0.0.1:8310 \
        --pair vara2-data:18311:127.0.0.1:8311 \
        --log capture.log

Then point the terminal app's two VARA connections at 127.0.0.1:18300/18301
and 127.0.0.1:18310/18311 respectively.
"""

import argparse
import datetime
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_log_file = None


def _log(label, direction, data: bytes):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"{ts} [{label}] {direction} ({len(data)}B) {data!r}"
    with _lock:
        print(line, flush=True)
        if _log_file is not None:
            _log_file.write(line + "\n")
            _log_file.flush()


def _pump(src: socket.socket, dst: socket.socket, label: str, direction: str):
    try:
        while True:
            chunk = src.recv(4096)
            if not chunk:
                return
            _log(label, direction, chunk)
            dst.sendall(chunk)
    except OSError:
        return
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle_client(client: socket.socket, target_host: str, target_port: int, label: str):
    try:
        upstream = socket.create_connection((target_host, target_port))
    except OSError as exc:
        _log(label, "!!", f"failed to connect upstream: {exc}".encode())
        client.close()
        return
    t1 = threading.Thread(target=_pump, args=(client, upstream, label, "client->vara"), daemon=True)
    t2 = threading.Thread(target=_pump, args=(upstream, client, label, "vara->client"), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    client.close()
    upstream.close()


def _serve_pair(listen_port: int, target_host: str, target_port: int, label: str):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", listen_port))
    listener.listen(4)
    print(f"[{label}] listening on 127.0.0.1:{listen_port} -> {target_host}:{target_port}",
          file=sys.stderr)
    while True:
        conn, addr = listener.accept()
        print(f"[{label}] client connected from {addr}", file=sys.stderr)
        threading.Thread(target=_handle_client, args=(conn, target_host, target_port, label),
                          daemon=True).start()


def _parse_pair(spec: str):
    label, listen_port, target_host, target_port = spec.split(":")
    return label, int(listen_port), target_host, int(target_port)


def _wait_for_port(port, processes, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(proc.poll() is not None for proc in processes):
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pair", action="append",
                    metavar="label:listen_port:target_host:target_port",
                    help="a proxied port pair; repeat for each VARA cmd/data port")
    ap.add_argument("--log", help="also append the capture to this file")
    ap.add_argument("--launch-stations", action="store_true",
                    help="launch two Whale HF stations and sniff through them")
    ap.add_argument("--radio-config", help="TOML radio inventory for launched stations")
    ap.add_argument("--station-a-radio", default="ic705")
    ap.add_argument("--station-b-radio", default="ic7300")
    ap.add_argument("--station-a-call", default="STA1")
    ap.add_argument("--station-b-call", default="STA2")
    args = ap.parse_args()

    station_procs = []
    if args.launch_stations:
        if args.pair:
            ap.error("--pair cannot be combined with --launch-stations")
        root = Path(__file__).resolve().parent.parent
        stations = [(args.station_a_radio, args.station_a_call, 8300, 8301),
                    (args.station_b_radio, args.station_b_call, 8310, 8311)]
        for radio, call, cmd_port, data_port in stations:
            cmd = [sys.executable, "-m", "whale.vara_server", "--radio", radio,
                   "--mycall", call, "--cmd-port", str(cmd_port),
                   "--data-port", str(data_port), "--channel", "hf", "--verbose"]
            if args.radio_config:
                cmd += ["--radio-config", args.radio_config]
            print(f"launching station {call}: {' '.join(cmd)}", file=sys.stderr)
            station_procs.append(subprocess.Popen(cmd, cwd=root))
        # The data listener is bound at startup but Whale only accepts a data
        # connection after the radio link reaches CONNECTED.  Waiting for a
        # successful data-port connect here would therefore time out during
        # normal idle startup.
        for _, _, cmd_port, data_port in stations:
            if not _wait_for_port(cmd_port, station_procs):
                for proc in station_procs:
                    if proc.poll() is None:
                        proc.terminate()
                ap.error(f"launched station did not open command port {cmd_port}")
        args.pair = ["sta1-cmd:18300:127.0.0.1:8300",
                     "sta1-data:18301:127.0.0.1:8301",
                     "sta2-cmd:18310:127.0.0.1:8310",
                     "sta2-data:18311:127.0.0.1:8311"]
    if not args.pair:
        ap.error("specify --pair or use --launch-stations")

    global _log_file
    if args.log:
        _log_file = open(args.log, "a", encoding="utf-8")

    threads = []
    for spec in args.pair:
        label, listen_port, target_host, target_port = _parse_pair(spec)
        t = threading.Thread(target=_serve_pair, args=(listen_port, target_host, target_port, label),
                             daemon=True)
        t.start()
        threads.append(t)

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        pass
    finally:
        for proc in station_procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in station_procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
