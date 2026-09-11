#!/usr/bin/env python3
"""Launch two whale vara_server instances, one per radio, for VARA-API testing.

Starts:
  - station A on the IC-705 (hf channel), cmd/data ports 8300/8301
  - station B on the IC-7300 (hf channel), cmd/data ports 8310/8311

Point two VARA-chat-compatible clients at 127.0.0.1:8300/8301 and
127.0.0.1:8310/8311 respectively to test a real connection between them.

Usage:
    python scripts/launch_two_stations.py --call-705 N0CALL-1 --call-7300 N0CALL-2
"""

import argparse
import signal
import subprocess
import sys
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--call-705", default="N0CALL-1", help="MYCALL for the IC-705 station")
    ap.add_argument("--call-7300", default="N0CALL-2", help="MYCALL for the IC-7300 station")
    ap.add_argument("--channel-705", default="hf", choices=("fm", "hf"))
    ap.add_argument("--channel-7300", default="hf", choices=("fm", "hf"))
    ap.add_argument("--cmd-port-705", type=int, default=8300)
    ap.add_argument("--data-port-705", type=int, default=8301)
    ap.add_argument("--cmd-port-7300", type=int, default=8310)
    ap.add_argument("--data-port-7300", type=int, default=8311)
    ap.add_argument("--radio-config", help="TOML radio inventory (or set WHALE_RADIO_CONFIG)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    def build_cmd(radio, mycall, channel, cmd_port, data_port):
        cmd = [
            sys.executable, "-m", "whale.vara_server",
            "--radio", radio,
            "--mycall", mycall,
            "--channel", channel,
            "--cmd-port", str(cmd_port),
            "--data-port", str(data_port),
        ]
        if args.radio_config:
            cmd += ["--radio-config", args.radio_config]
        if args.verbose:
            cmd.append("-v")
        return cmd

    stations = [
        ("ic705", build_cmd("ic705", args.call_705, args.channel_705,
                             args.cmd_port_705, args.data_port_705)),
        ("ic7300", build_cmd("ic7300", args.call_7300, args.channel_7300,
                              args.cmd_port_7300, args.data_port_7300)),
    ]

    procs = []
    for name, cmd in stations:
        print(f"[{name}] launching: {' '.join(cmd)}")
        procs.append((name, subprocess.Popen(cmd)))

    print()
    print(f"IC-705  station: cmd 127.0.0.1:{args.cmd_port_705}  data 127.0.0.1:{args.data_port_705}"
          f"  mycall={args.call_705}  channel={args.channel_705}")
    print(f"IC-7300 station: cmd 127.0.0.1:{args.cmd_port_7300}  data 127.0.0.1:{args.data_port_7300}"
          f"  mycall={args.call_7300}  channel={args.channel_7300}")
    print("\nPoint two VARA-chat clients at these cmd/data port pairs. Ctrl-C to stop both.")

    def shutdown(*_):
        for name, proc in procs:
            if proc.poll() is None:
                print(f"[{name}] stopping...")
                proc.terminate()
        for name, proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(f"[{name}] did not exit, killing")
                proc.kill()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while True:
            for name, proc in procs:
                ret = proc.poll()
                if ret is not None:
                    print(f"[{name}] exited with code {ret}")
                    shutdown()
                    sys.exit(ret)
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
