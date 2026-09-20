"""Offline analysis of a `scripts/head_bench.py` run. No radios, no PTT.

Three tools, one per question the campaign asks:

  --capture PATH   Stare at one keying. Level envelope against time,
                   acquisition confidence against time, the true preamble
                   position marked, and the offset / raw BER / per-carrier
                   SNR the decode reported. This is what you open when a
                   decode failed and you need to know *why* rather than
                   *whether*.

  --trim PATH      QUANTITY (B): trim progressively more of the settling
                   head off the front of a capture, re-acquire and re-decode
                   at each trim point, and report the shortest head that
                   still decodes. One on-air keying with a long head yields
                   the whole acquisition-side sweep for free.

  --aggregate LOG  Read a whole run's JSONL and print the summary table:
                   per mode, per direction, per scenario, per head length --
                   decode rate and the measured settle-time distribution.

WHAT --trim DOES NOT MEASURE, AND IT MATTERS
    Trimming removes recorded audio. The AGC transient that audio carries
    already happened, in the analogue domain, before anything was recorded:
    a capture trimmed to a 0.1 s head still contains a receiver that spent
    the preceding 0.5 s settling. So --trim answers the ACQUISITION question
    (B) -- how much signal the sync search needs in front of the preamble --
    and says NOTHING about the ANALOGUE question (A) -- how long the PTT ramp
    and the receiver AGC take.

    A head must cover both. The shortest head --trim reports is therefore a
    LOWER BOUND on the shippable head, never the answer. The upper of the two
    is the answer, and (A) comes from head_bench.py's `settle_seconds`,
    measured from the level envelope with no decode involved. Every --trim
    report repeats this in its own output, on purpose: the one way this
    campaign can go wrong is for someone to read a trim result as a head
    budget.

Run:
    python scripts/head_analyse.py --aggregate logs/head/run1.jsonl
    python scripts/head_analyse.py --capture logs/head/run1/single_ab_hc0_0.2_1_1_0_-_1.npy
    python scripts/head_analyse.py --trim logs/head/run1/....npy --log logs/head/run1.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

import head_bench as hb

#: Trim step for the head sweep, in seconds. 25 ms is finer than any head
#: length a mode will actually be shipped with is specified to, and coarse
#: enough that a sweep over a 1 s head is 40 decodes rather than 1000.
TRIM_STEP_SECONDS = 0.025

#: Window the confidence-against-time profile acquires over, in seconds. It
#: has to be long enough to hold a whole sync preamble or every score is
#: meaningless; 1.0 s exceeds the preamble of every HF mode here.
PROFILE_WINDOW_SECONDS = 1.0

#: Step between profile windows. 50 ms is half the envelope's own resolution
#: -- fine enough to see a confidence peak's shape, coarse enough that a
#: 6 s capture is ~120 acquisitions rather than thousands.
PROFILE_STEP_SECONDS = 0.05

#: Width of the printed sparklines, in columns.
PLOT_WIDTH = 72
_RAMP = " .:-=+*#%@"


def sparkline(values, width=PLOT_WIDTH, lo=None, hi=None) -> str:
    """One line of ASCII for a series. A plot in a terminal script beats a
    PNG nobody opens; the numbers are printed underneath it either way."""
    values = np.asarray(values, dtype=np.float64)
    if not len(values):
        return ""
    # Always exactly `width` columns, whichever way the series has to be
    # resampled: the caret line under it marks sample positions against the
    # same width, and a sparkline of a different length would put every
    # marker in the wrong place.
    edges = np.linspace(0, len(values), width + 1)
    # Max rather than mean when decimating: a confidence spike one window
    # wide is exactly what this is being read for.
    values = np.array([
        values[int(a):max(int(a) + 1, int(b))].max()
        for a, b in zip(edges[:-1], edges[1:])])
    lo = float(np.min(values)) if lo is None else lo
    hi = float(np.max(values)) if hi is None else hi
    if hi - lo < 1e-9:
        return _RAMP[0] * len(values)
    scaled = np.clip((values - lo) / (hi - lo), 0, 1) * (len(_RAMP) - 1)
    return "".join(_RAMP[int(round(v))] for v in scaled)


def marker_line(positions, total, width=PLOT_WIDTH, char="^") -> str:
    """A caret line under a sparkline, marking sample positions."""
    row = [" "] * width
    for position in positions:
        if position is None or total <= 0:
            continue
        column = int(round(width * position / total))
        if 0 <= column < width:
            row[column] = char
    return "".join(row)


# -- one capture ----------------------------------------------------------

def confidence_profile(name: str, audio_rx, window=PROFILE_WINDOW_SECONDS,
                       step=PROFILE_STEP_SECONDS):
    """Acquisition confidence against time, by re-acquiring in a window.

    Re-runs the mode's own acquisition over a sliding window rather than
    reaching into its correlator, so the numbers printed are the numbers the
    receiver would actually see -- including inside the settling head, which
    is the score `whale/streaming.py` gates at 0.12 and the OFDM head has
    been measured at 0.10-0.13.
    """
    audio_rx = np.asarray(audio_rx, dtype=np.float64).reshape(-1)
    window_n = int(window * hb.RX_RATE)
    step_n = max(1, int(step * hb.RX_RATE))
    offsets, scores = [], []
    for start in range(0, max(1, len(audio_rx) - window_n), step_n):
        piece = audio_rx[start:start + window_n]
        offsets.append(start / hb.RX_RATE)
        scores.append(_acquire_score(name, piece))
    return np.array(offsets), np.array(scores)


def _acquire_score(name: str, audio_rx) -> float:
    """The best acquisition score in this slice, by the mode's own search."""
    try:
        if name in ("hr0", "hc0", "hc1w"):
            from whale.phy import hc0, hc1w, hr0
            phy = {"hr0": hr0, "hc0": hc0, "hc1w": hc1w}[name]
            return float(phy.demodulate(audio_rx).get("confidence", 0.0) or 0.0)
        return float(hb._ofdm_phy(name).acquire(audio_rx)[0])
    except Exception:
        # A slice too short for the preamble is not an error here, it is a
        # zero; the profile's leading and trailing windows are both like that.
        return 0.0


def describe_capture(path, record=None, name=None, head_seconds=None,
                     payload=None, profile=True):
    """Print everything one capture has to say. Returns the decode result."""
    audio = np.load(path)
    record = record or {}
    name = name or record.get("mode")
    if name is None:
        raise SystemExit("cannot tell which mode this capture is; pass --mode "
                         "or --log so the record can be found")
    head_seconds = (record.get("head_seconds") if head_seconds is None
                    else head_seconds)
    if head_seconds is None:
        raise SystemExit("the head length cannot be recovered from audio; "
                         "pass --head or --log")

    print(f"capture {path}")
    print(f"  mode {name}  head {head_seconds:g}s  "
          f"{len(audio)} samples ({len(audio) / hb.RX_RATE:.2f}s at "
          f"{hb.RX_RATE} Hz)")

    envelope = hb.level_envelope(audio)
    settle = hb.settle_measurement(envelope)
    preamble_offset = hb.preamble_offset_rx(name, head_seconds)
    onset = (None if settle["onset_seconds"] is None
             else int(round(settle["onset_seconds"] * hb.RX_RATE)))
    true_preamble = None if onset is None else onset + preamble_offset

    print("\n  level envelope (dBFS, whole capture)")
    print("   |" + sparkline(envelope))
    print("   |" + marker_line(
        [onset, true_preamble], len(audio),
        char="^"))
    print(f"    ^ = RF onset and true preamble start "
          f"(onset {_fmt_s(settle['onset_seconds'])}, "
          f"preamble {_fmt_s(None if true_preamble is None else true_preamble / hb.RX_RATE)})")
    print(f"    floor {_fmt_db(settle['floor_db'])}  "
          f"steady {_fmt_db(settle['steady_db'])}  "
          f"peak {_fmt_db(settle['peak_db'])}")
    # This is quantity (A) and it is unreferenced here: describe_capture has
    # only the capture, not the TX buffer head_bench.py divided out, so the
    # figure is coarser than the run log's. The log's is the one to quote.
    print(f"    settle within {settle['tolerance_db']:g} dB: "
          f"{_fmt_s(settle['settle_seconds'])} after onset "
          f"(unreferenced; the run log's value is the measured one)")
    if record.get("settle_seconds") is not None:
        print(f"    run log says {record['settle_seconds'] * 1000:.0f} ms"
              f"{' (referenced to the TX envelope)' if record.get('settle_referenced_to_tx') else ''}")

    if profile:
        offsets, scores = confidence_profile(name, audio)
        print("\n  acquisition confidence vs time")
        print("   |" + sparkline(scores, lo=0.0))
        print("   |" + marker_line([true_preamble], len(audio)))
        best = int(np.argmax(scores)) if len(scores) else None
        if best is not None:
            # The profile's own windows are PROFILE_WINDOW_SECONDS long, so
            # one starting inside the head still reaches the preamble behind
            # it and scores on that. The in-head figure therefore comes from
            # acquisition run on the head alone, which is the quantity that
            # matters: what a receiver can lock onto before the preamble
            # exists at all.
            in_head = hb.in_head_confidence(name, audio, onset or 0,
                                            preamble_offset)
            print(f"    peak {scores[best]:.3f} at {offsets[best]:.3f}s; "
                  f"best score inside the head alone "
                  f"{'n/a' if in_head is None else f'{in_head:.3f}'}")
        print("    streaming.py gates candidates at 0.12 -- a head scoring "
              "above that can outrank the preamble behind it")

    result = None
    if payload is not None:
        result = hb.debug_decode(name, audio, payload)
        print("\n  decode against the known payload")
        print(f"    outcome     {hb.outcome_of(result, record.get('confidence_threshold', 0.12))}")
        print(f"    confidence  {result['confidence']:.3f}")
        print(f"    landed at   {result['start_index']} "
              f"(true preamble {true_preamble}, "
              f"error {_fmt_int(None if (result['start_index'] is None or true_preamble is None) else result['start_index'] - true_preamble)} samples)")
        print(f"    offset      {_fmt_hz(result['cfo_hz'])}")
        print(f"    raw BER     {_fmt_pct(result['raw_ber'])}")
        if result["carrier_snr_db"]:
            snr = np.array(result["carrier_snr_db"])
            print(f"    carriers    {snr.min():.1f}/{np.median(snr):.1f}/"
                  f"{snr.max():.1f} dB (min/med/max), low to high in frequency")
        if result["tone_snr_db"] is not None:
            print(f"    tone SNR    {result['tone_snr_db']:.1f} dB")
        if result["failure"]:
            print(f"    failure     {result['failure']!r}")
    else:
        print("\n  no reference payload available; pass --log so the record's "
              "(run id, mode, sequence) can regenerate it")
    return result


# -- quantity (B): the offline trim sweep ---------------------------------

#: Printed with every trim result, and repeated in this module's docstring.
#: The single most likely way for this campaign to reach a wrong conclusion
#: is for a trim number to be quoted as a head budget.
TRIM_CAVEAT = (
    "QUANTITY (B) ONLY -- ACQUISITION, NOT AGC.\n"
    "  Trimming removes audio whose PTT ramp and receiver AGC transient\n"
    "  already happened in the analogue domain before this capture existed.\n"
    "  A capture trimmed to a 0.10 s head still contains a receiver that had\n"
    "  the whole transmitted head to settle. So the shortest head reported\n"
    "  here is a LOWER BOUND on what\n"
    "  is shippable -- it is how much signal ACQUISITION needs in front of the\n"
    "  preamble, and nothing else.\n"
    "  The shippable head is max(this, the analogue settle time), and the\n"
    "  analogue settle time comes from head_bench.py's `settle_seconds`,\n"
    "  measured from the level envelope with no decode involved.")


def trim_sweep(name: str, audio_rx, head_seconds: float, payload: bytes,
               onset_index: int | None = None,
               step=TRIM_STEP_SECONDS, threshold=None):
    """Re-acquire and re-decode with progressively more head trimmed off.

    Trims from the RF onset forward, so trim 0 is the keying exactly as it
    arrived and trim `head_seconds` removes the head entirely and starts the
    audio on the preamble. Returns one row per trim point.
    """
    audio_rx = np.asarray(audio_rx, dtype=np.float64).reshape(-1)
    if onset_index is None:
        settle = hb.settle_measurement(hb.level_envelope(audio_rx))
        onset_index = (0 if settle["onset_seconds"] is None
                       else int(round(settle["onset_seconds"] * hb.RX_RATE)))
    if threshold is None:
        threshold = float(getattr(hb.mode_by_name(name),
                                  "confidence_threshold", 0.12))
    preamble_offset = hb.preamble_offset_rx(name, head_seconds)

    rows = []
    trim_n = 0
    step_n = max(1, int(step * hb.RX_RATE))
    while trim_n <= preamble_offset:
        cut = onset_index + trim_n
        result = hb.debug_decode(name, audio_rx[cut:], payload)
        remaining = (preamble_offset - trim_n) / hb.RX_RATE
        true_preamble = preamble_offset - trim_n
        rows.append({
            "trimmed_seconds": trim_n / hb.RX_RATE,
            "remaining_head_seconds": remaining,
            "outcome": hb.outcome_of(result, threshold),
            "confidence": result["confidence"],
            "start_index": result["start_index"],
            "raw_ber": result["raw_ber"],
            **hb.landing_of(result, true_preamble, threshold, onset_index=0),
        })
        trim_n += step_n
    return rows


def shortest_decoding_head(rows) -> float | None:
    """The shortest remaining head that still decoded, over the trim rows.

    "Shortest that decoded" rather than "longest that failed": the sweep is
    not guaranteed monotone (an acquisition that lands one symbol early can
    decode while a slightly longer head does not), and the shortest head
    with a decode is the claim that is actually supported.
    """
    decoded = [r["remaining_head_seconds"] for r in rows
               if r["outcome"] == "decoded"]
    return min(decoded) if decoded else None


def print_trim(name, rows):
    print(f"\n== head trim sweep, {name} ==")
    print(f"{'trimmed':>9}{'head left':>11}{'outcome':>13}{'conf':>8}"
          f"{'landing':>14}{'rawBER':>9}")
    for row in rows:
        ber = "  -" if row["raw_ber"] is None else f"{row['raw_ber'] * 100:.2f}%"
        print(f"{row['trimmed_seconds']:>8.3f}s{row['remaining_head_seconds']:>10.3f}s"
              f"{row['outcome']:>13}{row['confidence']:>8.3f}"
              f"{row['landing']:>14}{ber:>9}")
    shortest = shortest_decoding_head(rows)
    print(f"\n  shortest head that still decoded: "
          f"{'none did' if shortest is None else f'{shortest:.3f} s'}")
    print("\n  " + TRIM_CAVEAT.replace("\n", "\n  "))


# -- the aggregate report -------------------------------------------------

def _percentiles(values):
    if not values:
        return None
    arr = np.array(values, dtype=float)
    return (float(np.percentile(arr, 50)), float(np.percentile(arr, 90)),
            float(np.max(arr)))


def aggregate(records, group_by=("mode", "direction", "scenario",
                                 "head_seconds")):
    """Decode rate and settle-time distribution, per cell of the matrix."""
    groups = defaultdict(lambda: {"keyings": 0, "decoded": 0, "frames": 0,
                                  "frames_decoded": 0, "settle": [],
                                  "in_head": [], "landings": defaultdict(int),
                                  "xruns": 0, "unmeasured": defaultdict(int),
                                  "receive_paths": defaultdict(int)})
    for record in records:
        key = tuple(record.get(field) for field in group_by)
        bucket = groups[key]
        bucket["keyings"] += 1
        bucket["frames"] += record.get("frame_count", 0)
        bucket["frames_decoded"] += record.get("decoded_count", 0)
        bucket["decoded"] += int(record.get("decoded_count", 0)
                                 >= max(1, record.get("frame_count", 1)))
        if record.get("settle_seconds") is not None:
            bucket["settle"].append(record["settle_seconds"])
        elif record.get("settle_unmeasured_reason"):
            # Counted, never averaged in as a zero or a maximum: a cell that
            # could not be measured must read as unmeasured, which is a
            # different statement from "settled quickly" or "settled slowly".
            bucket["unmeasured"][record["settle_unmeasured_reason"]] += 1
        if record.get("in_head_confidence") is not None:
            bucket["in_head"].append(record["in_head_confidence"])
        for frame in record.get("frames", ()):
            bucket["landings"][frame.get("landing", "?")] += 1
        # A keying that saw an xrun is not a head measurement; counted so a
        # suspicious cell can be told from a genuinely failing one.
        if record.get("rx_overflows") or record.get("tx_underflows"):
            bucket["xruns"] += 1
        # Which receiver produced the record. A record with no such field
        # predates the bench moving onto the shipped receive machinery and
        # was decoded by a whole-capture best-argmax search instead; the two
        # disagree on every burst, so a table mixing them is not comparable
        # and must say so rather than average them together.
        bucket["receive_paths"][record.get("receive_path")
                                or PRE_STREAMING_PATH] += 1
    return groups


#: What a record with no `receive_path` field was decoded by.
PRE_STREAMING_PATH = "whole-capture (pre-streaming log)"


def receive_path_note(groups) -> str | None:
    """A warning line when a table's records did not share one receiver."""
    paths = defaultdict(int)
    for bucket in groups.values():
        for name, count in bucket["receive_paths"].items():
            paths[name] += count
    if PRE_STREAMING_PATH not in paths:
        return None if len(paths) <= 1 else (
            "  WARNING: these records came from more than one receive path "
            "(" + ", ".join(f"{k}:{v}" for k, v in sorted(paths.items()))
            + "); they are not comparable.")
    return ("  WARNING: " + str(paths[PRE_STREAMING_PATH]) + " of these "
            "records predate the bench decoding through the shipped "
            "streaming\n  receive path and were decoded by a whole-capture "
            "best-argmax search. That search\n  takes the best-scoring frame "
            "in a keying rather than the first adequate one, so its\n  burst "
            "rows are not comparable with a current run's. Re-run them.")


def print_aggregate(groups, group_by):
    header = "".join(f"{field:<12}" for field in group_by)
    print(f"{header}{'keyed':>7}{'ok':>5}{'frames':>11}"
          f"{'settle p50/p90/max (ms)':>26}{'unmeas':>8}{'in-head':>9}"
          f"{'xrun':>6}  landings")
    for key in sorted(groups, key=lambda k: tuple(str(v) for v in k)):
        bucket = groups[key]
        cells = "".join(f"{_fmt_cell(v):<12}" for v in key)
        settle = _percentiles(bucket["settle"])
        settle_text = ("        -" if settle is None else
                       f"{settle[0] * 1000:.0f}/{settle[1] * 1000:.0f}/"
                       f"{settle[2] * 1000:.0f}")
        unmeasured = sum(bucket["unmeasured"].values())
        unmeasured_text = "-" if not unmeasured else str(unmeasured)
        in_head = ("   -" if not bucket["in_head"]
                   else f"{max(bucket['in_head']):.3f}")
        landings = " ".join(f"{k}:{v}" for k, v in
                            sorted(bucket["landings"].items()))
        frames = f"{bucket['frames_decoded']}/{bucket['frames']}"
        print(f"{cells}{bucket['keyings']:>7}{bucket['decoded']:>5}{frames:>11}"
              f"{settle_text:>26}{unmeasured_text:>8}{in_head:>9}"
              f"{bucket['xruns']:>6}  {landings}")
        if unmeasured:
            print(" " * len(cells) + "  unmeasured: " + " ".join(
                f"{k}:{v}" for k, v in sorted(bucket["unmeasured"].items())))
    note = receive_path_note(groups)
    if note:
        print()
        print(note)
    print("\n  settle = QUANTITY (A), the analogue ramp, measured from the "
          "level envelope.\n"
          "  It is NOT the acquisition requirement; for that run "
          "--trim on a capture.\n"
          "  in-head = the worst (highest) acquisition score found inside a "
          "settling head\n"
          "  in that cell. Above 0.12 it can outrank the preamble behind it.")


def _fmt_cell(value):
    if isinstance(value, float):
        return f"{value:g}"
    return "-" if value is None else str(value)


def _fmt_s(value):
    return "n/a" if value is None else f"{value:.3f}s"


def _fmt_db(value):
    return "n/a" if value is None else f"{value:.1f} dB"


def _fmt_hz(value):
    return "n/a" if value is None else f"{value:+.2f} Hz"


def _fmt_pct(value):
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _fmt_int(value):
    return "n/a" if value is None else f"{value:+d}"


def find_record(log_path, capture_path):
    """The JSONL record a capture belongs to, matched on its saved path."""
    capture = Path(capture_path).resolve()
    for record in hb.RunLog(log_path).records():
        saved = record.get("capture_path")
        if saved and Path(saved).resolve() == capture:
            return record
    # Fall back to the filename, which is the cell key with '|' swapped for
    # '_': a log moved between machines keeps matching.
    stem = capture.stem
    for record in hb.RunLog(log_path).records():
        if record.get("cell_key", "").replace("|", "_") == stem:
            return record
    return None


def payload_from_record(record, seq=0):
    """Regenerate exactly the bytes that keying transmitted."""
    mode = hb.mode_by_name(record["mode"])
    return hb.payload_for(record["run_id"], mode, seq)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--capture", help="diagnose one keying's .npy capture")
    what.add_argument("--trim", metavar="CAPTURE",
                      help="offline head-trim sweep on one capture: "
                           "QUANTITY (B), the acquisition requirement only")
    what.add_argument("--aggregate", metavar="JSONL",
                      help="summary table over a whole run log")
    ap.add_argument("--log", help="the run's JSONL, to find the capture's "
                                  "record and regenerate its payload")
    ap.add_argument("--mode", help="override the mode of a capture with no record")
    ap.add_argument("--head", type=float,
                    help="override the head length of a capture with no record")
    ap.add_argument("--trim-step", type=float, default=TRIM_STEP_SECONDS,
                    help="trim step in seconds")
    ap.add_argument("--no-profile", action="store_true",
                    help="skip the confidence-vs-time profile, which is the "
                         "slow part of --capture")
    ap.add_argument("--group-by", nargs="+",
                    default=["mode", "direction", "scenario", "head_seconds"],
                    help="record fields the aggregate table groups on")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON instead of the table")
    args = ap.parse_args(argv)

    if args.aggregate:
        records = hb.RunLog(args.aggregate).records()
        if not records:
            print(f"no records in {args.aggregate}")
            return 1
        groups = aggregate(records, tuple(args.group_by))
        if args.json:
            print(json.dumps({
                "|".join(map(str, k)): {
                    "keyings": v["keyings"], "keyings_ok": v["decoded"],
                    "frames": v["frames"], "frames_decoded": v["frames_decoded"],
                    "settle_seconds": v["settle"],
                    "settle_unmeasured": dict(v["unmeasured"]),
                    "in_head_confidence_max": (max(v["in_head"])
                                               if v["in_head"] else None),
                    "landings": dict(v["landings"]), "xrun_keyings": v["xruns"],
                    "receive_paths": dict(v["receive_paths"]),
                } for k, v in groups.items()}, indent=2))
        else:
            print(f"{len(records)} keyings from {args.aggregate}\n")
            print_aggregate(groups, tuple(args.group_by))
        return 0

    path = args.capture or args.trim
    record = find_record(args.log, path) if args.log else None
    name = args.mode or (record or {}).get("mode")
    head = args.head if args.head is not None else (record or {}).get("head_seconds")
    if name is None or head is None:
        ap.error("need --log (to find the capture's record) or both --mode "
                 "and --head; neither the mode nor the head length can be "
                 "recovered from the audio alone")
    payload = payload_from_record(record) if record else None

    if args.capture:
        describe_capture(path, record, name, head, payload,
                         profile=not args.no_profile)
        return 0

    if payload is None:
        ap.error("--trim compares against the payload that was sent, so it "
                 "needs --log to regenerate it")
    audio = np.load(path)
    onset = (record or {}).get("onset_index")
    rows = trim_sweep(name, audio, head, payload, onset_index=onset,
                      step=args.trim_step)
    if args.json:
        print(json.dumps({"mode": name, "head_seconds": head,
                          "shortest_decoding_head_seconds":
                              shortest_decoding_head(rows),
                          "measures": "quantity B (acquisition) only; see "
                                      "TRIM_CAVEAT -- this is a lower bound "
                                      "on a shippable head, not a head budget",
                          "rows": rows}, indent=2))
    else:
        print_trim(name, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
