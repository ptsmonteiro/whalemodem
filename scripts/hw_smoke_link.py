"""Full Link-level smoke test over real hardware, in one process, no
TCP/VARA layer: connect, send a small message each direction, disconnect.
Isolates the ARQ/link protocol from the VARA-API socket server -- run this
before or instead of the full acceptance_test.py when tracking down a
hardware/protocol issue, since it prints per-frame timing and events
directly instead of through the VARA status-line layer.

Run: python scripts/hw_smoke_link.py
       python scripts/hw_smoke_link.py --profile 600baud

--profile seeds both sides' mode_history so connect() proposes/negotiates
straight into that profile instead of starting at CONTROL_PROFILE (300baud)
and stepping up -- lets you bench a specific speed mode directly, including
the pre-TX (TX_TURNAROUND_DELAY) and post-TX (ptt_lead/ptt_tail) settling
that has to hold up at that profile's shorter frame timing. Printed timings
below (PTT-on -> PTT-off wall clock per TX) are how you check that math:
compare against afsk.frame_seconds(...) for the payload sent -- the gap
between them is turnaround/PTT margin actually consumed, not just assumed.
"""
import argparse
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from whale import afsk, link as link_mod, mode_history, transport as transport_mod
from whale.link import Link
from whale.transport import RadioTransport

MSG_AB = b"hello from STA1 to STA2 " * 4   # 96 bytes
MSG_BA = b"reply from STA2 to STA1 " * 4   # 96 bytes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    profile_names = sorted(p.name for p in afsk.PROFILES)
    ap.add_argument("--profile", default=None, choices=profile_names,
                     help="seed mode_history so both sides connect straight into this profile "
                          "(default: start at the control profile, as a normal connect would)")
    args = ap.parse_args()

    print(f"opening radios... (TX_TURNAROUND_DELAY={link_mod.TX_TURNAROUND_DELAY}s, "
          f"ptt_lead={transport_mod.PTT_LEAD}s, ptt_tail={transport_mod.PTT_TAIL}s -- see "
          f"whale/transport.py for the latter two, both baud-independent radio settling "
          f"measured by scripts/sweep_ptt_timing.py)")
    t1 = RadioTransport("ic705")
    t2 = RadioTransport("ht")

    history_a, history_b = {}, {}
    if args.profile is not None:
        profile = next(p for p in afsk.PROFILES if p.name == args.profile)
        mode_history.record_good_mode(history_a, "STA1", "STA2", profile.mode_id)
        mode_history.record_good_mode(history_b, "STA2", "STA1", profile.mode_id)
        print(f"forcing connect to start at profile {profile.name} via seeded mode_history")

    def _make_ptt_logger(station):
        """Prints keyed-PTT duration per TX (PTT on -> PTT off wall clock),
        so pre/post-TX delay margins are visible numbers, not assumptions."""
        state = {}

        def on_event(name, **kw):
            if name == "PTT" and kw.get("on"):
                state["t0"] = time.time()
            elif name == "PTT" and kw.get("off"):
                dt = time.time() - state.pop("t0", time.time())
                print(f"[{station} event] PTT keyed for {dt:.2f}s")
            else:
                print(f"[{station} event] {name} {kw}")
        return on_event

    link1 = Link(t1, "STA1", on_event=_make_ptt_logger("STA1"), mode_history_store=history_a)
    link2 = Link(t2, "STA2", on_event=_make_ptt_logger("STA2"), mode_history_store=history_b)

    ok = {}
    try:
        link1.start()
        link2.start()
        print("warming up 3s...")
        time.sleep(3)

        def do_listen():
            ok["listen"] = link2.listen_once(timeout=30)

        th = threading.Thread(target=do_listen, daemon=True)
        th.start()
        time.sleep(1.0)

        print("STA1 connecting to STA2...")
        ok["connect"] = link1.connect("STA2", timeout_per_try=6, retries=3)
        th.join(timeout=30)
        print("connect result:", ok.get("connect"), "listen result:", ok.get("listen"))
        if not ok.get("connect") or not ok.get("listen"):
            print("CONNECT FAILED, aborting")
            return 1

        print(f"STA1 -> STA2 sending {len(MSG_AB)} bytes...")
        sent1 = {}

        def send1():
            try:
                link1.send_message(MSG_AB)
                sent1["ok"] = True
            except Exception as e:
                sent1["ok"] = False
                print("send1 FAILED:", repr(e))

        th1 = threading.Thread(target=send1, daemon=True)
        th1.start()
        got_ab = link2.recv_message(timeout=60)
        th1.join(timeout=10)
        print("send ok:", sent1.get("ok"), "received:", got_ab == MSG_AB, f"({len(got_ab or b'')} bytes)")

        print(f"STA2 -> STA1 sending {len(MSG_BA)} bytes...")
        sent2 = {}

        def send2():
            try:
                link2.send_message(MSG_BA)
                sent2["ok"] = True
            except Exception as e:
                sent2["ok"] = False
                print("send2 FAILED:", repr(e))

        th2 = threading.Thread(target=send2, daemon=True)
        th2.start()
        got_ba = link1.recv_message(timeout=60)
        th2.join(timeout=10)
        print("send ok:", sent2.get("ok"), "received:", got_ba == MSG_BA, f"({len(got_ba or b'')} bytes)")

        print("STA1 disconnecting...")
        disc_ok = link1.disconnect(timeout=10, retries=3)
        print("disconnect result:", disc_ok)

        all_ok = (
            ok.get("connect") and ok.get("listen")
            and sent1.get("ok") and got_ab == MSG_AB
            and sent2.get("ok") and got_ba == MSG_BA
        )
        print("\n== OVERALL:", "PASS" if all_ok else "FAIL", "==")
        return 0 if all_ok else 1
    finally:
        link1.stop()
        link2.stop()
        t1.close()
        t2.close()


if __name__ == "__main__":
    sys.exit(main())
