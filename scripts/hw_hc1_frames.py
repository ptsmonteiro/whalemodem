"""HC1 frames over the real HF pair: one modulate -> TX -> capture ->
demodulate per trial, both directions, no ARQ and no sockets.

Same method as everything else in this directory (see scripts/bench.py):
bypass whale.link entirely so a data point is a property of the channel and
the DSP rather than of the retry logic on top of them.  What is different is
what it prints, because on HF the interesting numbers are different ones:

    offset       the carrier frequency error HC1 measured and corrected.
                 This is the quantity the FM profiles have no estimate of at
                 all, and the first thing to look at if frames fail -- past
                 +-46.9 Hz (hc1.COARSE_OFFSET_LIMIT_HZ) the coarse estimator
                 wraps and nothing downstream can recover.
    raw BER      bit errors before the convolutional code, against the known
                 payload.  A frame that decodes at 8% raw BER is working as
                 designed; one that fails at 1% means something other than
                 noise (timing, offset, a notch) is wrong.
    carriers     per-carrier SNR from the header fit, low to high in
                 frequency -- this is the SSB filter's shape, measured.  A
                 skirt eating the edge carriers shows up here and nowhere
                 else.
    level        capture RMS and peak.  An HF receiver with AGC and a data
                 mode that is over-driven clips, and clipping an OFDM signal
                 destroys it far faster than noise does.

Run:
    python scripts/hw_hc1_frames.py
    python scripts/hw_hc1_frames.py --trials 5 --payload 74
    python scripts/hw_hc1_frames.py --a ic7300 --b ic705 --capture-dir logs/hc1
"""

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

import bench
from whale.modes import hc1

CAPTURE_TAIL = 1.5
INTER_TRIAL = 0.5


def _payload(size, rng):
    return bytes(rng.integers(0, 256, size, dtype=np.uint8))


def one_trial(tx, rx, payload, label, capture_tail=CAPTURE_TAIL):
    stale = rx.snapshot_rx()
    rx.consume_rx(len(stale))

    audio = hc1.modulate(payload)
    keyed = tx.send(audio)
    time.sleep(capture_tail)
    captured = rx.snapshot_rx()

    result = hc1.demodulate_debug(captured, payload)
    ok = result.get("payload") == payload
    rms = float(np.sqrt(np.mean(np.asarray(captured, float) ** 2))) if len(captured) else 0.0
    peak = float(np.max(np.abs(captured))) if len(captured) else 0.0

    snr = result.get("carrier_snr_db")
    snr_line = ""
    if snr is not None and np.all(np.isfinite(snr)):
        snr_line = (f" carriers {np.min(snr):.1f}/{np.median(snr):.1f}/"
                    f"{np.max(snr):.1f} dB (min/med/max)")
    ber = result.get("ber")
    print(f"  [{label}] keyed={keyed:.2f}s level rms={rms:.3f} peak={peak:.3f} "
          f"conf={result.get('confidence', 0.0):.3f} "
          f"offset={result.get('cfo_hz', 0.0):+.2f} Hz "
          f"clock={result.get('clock_offset_ppm', 0.0):+.1f} ppm "
          f"present={result.get('present_carriers', 0)}/{hc1.N_CARRIERS}"
          + (f" rawBER={ber * 100:.2f}%" if ber is not None else "")
          + snr_line
          + f" decoded={ok}")
    if not ok:
        print(f"      failure: {result.get('failure')!r}")
    return ok, result, captured


def run_direction(tx, rx, label, trials, size, rng, capture_dir=None):
    print(f"\n== {label} ==")
    ok_count = 0
    records = []
    for i in range(1, trials + 1):
        payload = _payload(size, rng)
        ok, result, captured = one_trial(tx, rx, payload, f"{label} {i}/{trials}")
        ok_count += int(ok)
        records.append({
            "trial": i, "decoded": bool(ok),
            "confidence": float(result.get("confidence", 0.0)),
            "cfo_hz": float(result.get("cfo_hz", 0.0)),
            "clock_offset_ppm": float(result.get("clock_offset_ppm", 0.0)),
            "present_carriers": int(result.get("present_carriers", 0)),
            "raw_ber": None if result.get("ber") is None else float(result["ber"]),
            "carrier_snr_db": (None if result.get("carrier_snr_db") is None
                               or not np.all(np.isfinite(result["carrier_snr_db"]))
                               else [round(float(v), 2) for v in result["carrier_snr_db"]]),
            "head_cores": result.get("head_cores_received"),
            "failure": result.get("failure"),
        })
        if capture_dir is not None:
            # "->" is not a legal Windows filename character.
            safe = label.replace(" ", "").replace("->", "_to_")
            stem = Path(capture_dir) / f"{safe}_{i:02d}"
            np.save(stem.with_suffix(".npy"), np.asarray(captured, np.float32))
            stem.with_suffix(".bin").write_bytes(payload)
        time.sleep(INTER_TRIAL)
    print(f"  [{label}] => {ok_count}/{trials} decoded byte-for-byte")
    return ok_count, records


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300", help="station A radio name")
    ap.add_argument("--b", default="ic705", help="station B radio name")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--payload", type=int, default=hc1.MAX_PAYLOAD_BYTES,
                    help=f"payload bytes per frame (max {hc1.MAX_PAYLOAD_BYTES})")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--direction", default="both", choices=("both", "ab", "ba"),
                    help="which leg(s) to run; a one-legged run is for a bench "
                         "where the reverse path is known to be broken")
    ap.add_argument("--capture-dir", default=None,
                    help="save each capture and its payload here")
    ap.add_argument("--out", default=None, help="write a JSON summary here")
    args = ap.parse_args()

    if args.payload > hc1.MAX_PAYLOAD_BYTES:
        ap.error(f"payload must be <= {hc1.MAX_PAYLOAD_BYTES}")
    if args.capture_dir:
        Path(args.capture_dir).mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(args.seed)
    print(hc1.describe())
    print(f"payload {args.payload} B, {args.trials} trials each direction")

    ok_ab = ok_ba = None
    rec_ab = rec_ba = []
    with bench.radio_pair(args.a, args.b, warmup=3.0) as (ta, tb):
        if args.direction in ("both", "ab"):
            ok_ab, rec_ab = run_direction(ta, tb, f"{args.a}->{args.b}", args.trials,
                                          args.payload, rng, args.capture_dir)
        if args.direction in ("both", "ba"):
            ok_ba, rec_ba = run_direction(tb, ta, f"{args.b}->{args.a}", args.trials,
                                          args.payload, rng, args.capture_dir)

    print("\n== RESULTS ==")
    if ok_ab is not None:
        print(f"{args.a} -> {args.b}: {ok_ab}/{args.trials}")
    if ok_ba is not None:
        print(f"{args.b} -> {args.a}: {ok_ba}/{args.trials}")
    worst = min(v for v in (ok_ab, ok_ba) if v is not None)
    print(f"worst direction run: {worst}/{args.trials}")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "mode": hc1.describe(), "payload_bytes": args.payload,
            "trials": args.trials, "a": args.a, "b": args.b,
            f"{args.a}->{args.b}": rec_ab, f"{args.b}->{args.a}": rec_ba,
        }, indent=2))
        print(f"wrote {args.out}")
    return 0 if worst == args.trials else 1


if __name__ == "__main__":
    sys.exit(main())
