"""How long a healthy session can legitimately go without hearing anything
from the peer -- the measurement behind whale.link.INACTIVITY_TIMEOUT.

An inactivity timeout is only safe if it is longer than the longest silence
a *working* link produces. That is not a number anyone should guess: it
depends on the ACK timeout, which depends on frame airtime, which depends
on the profile in use, and it is at its worst during exactly the events a
healthy session is entitled to have -- a run of retransmits, and a mode
step whose ack goes missing.

So this reads it out of the station logs instead. For each log it finds the
CONNECTED span and reports the largest gap between consecutive frames
decoded off the air, which is precisely what INACTIVITY_TIMEOUT is measured
against. Both stations are reported separately, because the two legs of
this bench differ materially in SNR (see whale/afsk.py) and the worse one
is the one that has to decide.

The two events the measurement has to include are usually absent from a
clean run, so their computed worst cases are printed alongside: a full
MAX_RETRIES cycle and an unanswered mode step, evaluated from the same
formulas whale/link.py uses at runtime.

Run:
    python scripts/measure_peer_gap.py logs/sta1.log logs/sta2.log
"""

import argparse
import re
from datetime import datetime
from pathlib import Path

from whale import afsk, link

TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) ")
RX_FRAME = re.compile(r"\[(?P<call>\S+)\] RX (?P<ptype>\S+) at (?P<profile>\S+)")
SESSION_START = re.compile(r"\] (?:connected to|accepted connection from) ")
SESSION_END = re.compile(r"-> DISCONNECTED|\] CONNECT to .* gave up")


def _stamp(line):
    m = TIMESTAMP.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f")


def worst_gap(path):
    """(worst_seconds, description) over every CONNECTED span in one log."""
    marks = []          # (time, kind, detail)
    for line in Path(path).read_text(errors="replace").splitlines():
        when = _stamp(line)
        if when is None:
            continue
        if SESSION_START.search(line):
            marks.append((when, "start", line.strip()))
        elif SESSION_END.search(line):
            marks.append((when, "end", line.strip()))
        else:
            m = RX_FRAME.search(line)
            if m:
                marks.append((when, "rx", m.group("ptype")))

    worst, detail, previous = 0.0, "", None
    for when, kind, text in marks:
        if kind == "start":
            previous = (when, "the handshake")
            continue
        if previous is None:
            continue
        gap = (when - previous[0]).total_seconds()
        if gap > worst:
            worst = gap
            worst_from = previous[1]
            detail = (f"{gap:6.1f}s between {worst_from} and "
                      f"{'the session ending' if kind == 'end' else 'RX ' + text} "
                      f"at {when:%H:%M:%S}")
        previous = None if kind == "end" else (when, "RX " + text)
    return worst, detail


def computed_worst_cases():
    """The two silences a clean run does not necessarily contain, from the
    same arithmetic _recompute_timings and control_ack_timeout use."""
    rows = []
    for profile in afsk.PROFILES:
        tx_airtime = afsk.frame_seconds(
            profile.chunk_size + afsk.DATA_FRAME_HEADER_BYTES, profile)
        ack_airtime = afsk.frame_seconds(3, profile)
        data_ack_timeout = (tx_airtime + ack_airtime
                            + 2 * link.TX_TURNAROUND_DELAY + 3.0)
        rows.append((profile.name, data_ack_timeout,
                     link.MAX_RETRIES * data_ack_timeout))
    control = afsk.frame_seconds(link._CONTROL_FRAME_LEN_ESTIMATE,
                                 afsk.CONTROL_PROFILE) + 3.0
    return rows, control


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+", help="station log files")
    args = ap.parse_args()

    print("== measured: worst silence in each station's logged session(s) ==")
    measured = 0.0
    for path in args.logs:
        gap, detail = worst_gap(path)
        measured = max(measured, gap)
        print(f"  {path}: {gap:.1f}s")
        if detail:
            print(f"      {detail}")

    print("\n== computed: the two silences a clean run need not contain ==")
    rows, control = computed_worst_cases()
    print(f"  {'profile':<10} {'data_ack_timeout':>17} {'full MAX_RETRIES cycle':>24}")
    for name, one, full in rows:
        print(f"  {name:<10} {one:>16.1f}s {full:>23.1f}s")
    print(f"  unanswered mode step (control_ack_timeout): {control:.1f}s")

    worst_cycle = max(full for _, _, full in rows)
    total = worst_cycle + control
    print(f"\nworst legitimate silence = {worst_cycle:.1f}s (retries) + {control:.1f}s "
          f"(mode step) = {total:.1f}s")
    print(f"worst measured in these logs                              = {measured:.1f}s")
    print(f"whale.link.INACTIVITY_TIMEOUT is currently {link.INACTIVITY_TIMEOUT:.0f}s "
          f"({link.INACTIVITY_TIMEOUT / total:.1f}x the worst legitimate silence)")


if __name__ == "__main__":
    main()
