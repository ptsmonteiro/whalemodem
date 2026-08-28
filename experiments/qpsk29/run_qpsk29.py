"""Put qpsk29 frames over the IC-7300 -> IC-705 HF bench path.

One trial is one direct modulate -> TX -> capture -> demodulate operation; no
link-layer retries can hide a waveform failure.  The default direction is the
strong path explicitly used to develop the fastest 4PSK profile.
"""

import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import bench  # noqa: E402
import qpsk29 as q  # noqa: E402


def run_direction(tx, rx, label, profile, trials, rng, capture_dir):
    records = []
    successes = 0
    for trial in range(1, trials + 1):
        payload = rng.integers(0, 256, profile.max_payload,
                               dtype=np.uint8).tobytes()
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        audio = q.modulate(payload, profile)
        keyed = tx.send(audio)
        time.sleep(1.5)
        captured = rx.snapshot_rx()
        started = time.perf_counter()
        result = q.demodulate_debug(captured, profile, payload)
        decode_seconds = time.perf_counter() - started
        good = result.get("payload") == payload
        successes += int(good)
        snr = np.asarray(result.get("carrier_snr_db", []), dtype=float)
        finite_snr = snr[np.isfinite(snr)]
        record = {
            "trial": trial,
            "decoded": bool(good),
            "keyed_seconds": round(float(keyed), 3),
            "decode_seconds": round(decode_seconds, 3),
            "confidence": round(float(result.get("confidence", 0.0)), 5),
            "cfo_hz": round(float(result.get("cfo_hz", 0.0)), 3),
            "raw_ber": (None if result.get("ber") is None
                        else float(result["ber"])),
            "carrier_snr_db": [round(float(value), 2) for value in finite_snr],
            "ldpc_ok": int(np.count_nonzero(result.get("ldpc_ok", []))),
            "ldpc_iterations_max": int(np.max(result.get("ldpc_iterations", [0]))),
            "pilot_error_rms": (None if result.get("pilot_error_rms") is None
                                else round(float(result["pilot_error_rms"]), 5)),
            "failure": result.get("failure"),
        }
        records.append(record)
        snr_text = ("n/a" if not len(finite_snr) else
                    f"{np.min(finite_snr):.1f}/{np.median(finite_snr):.1f}/"
                    f"{np.max(finite_snr):.1f}")
        print(f"  [{label} {trial}/{trials}] keyed={keyed:.2f}s "
              f"decode={decode_seconds:.2f}s conf={record['confidence']:.3f} "
              f"offset={record['cfo_hz']:+.2f}Hz SNR={snr_text}dB "
              f"rawBER={(record['raw_ber'] or 0.0) * 100:.3f}% "
              f"LDPC={record['ldpc_ok']}/{q.CODEWORDS} decoded={good}")
        if not good:
            print(f"      failure: {result.get('failure')}")
        if capture_dir:
            safe = label.replace("->", "_to_")
            stem = capture_dir / f"qpsk29_{safe}_{trial:02d}"
            np.save(stem.with_suffix(".npy"), np.asarray(captured, np.float32))
            stem.with_suffix(".bin").write_bytes(payload)
        time.sleep(0.5)
    print(f"  {label}: {successes}/{trials} byte-for-byte")
    return successes, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", default="ic7300")
    parser.add_argument("--b", default="ic705")
    parser.add_argument("--direction", choices=("ab", "ba", "both"), default="ab")
    parser.add_argument("--fec", choices=("1/2", "2/3", "3/4", "none"),
                        default="2/3")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--amplitude", type=float, default=0.9)
    parser.add_argument("--papr", type=float, default=9.0)
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument("--capture-dir", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials must be positive")
    profile = q.Qpsk29Profile(
        name=f"qpsk29-ldpc{args.fec.replace('/', '')}",
        fec=None if args.fec == "none" else args.fec,
        amplitude=args.amplitude, papr_db=args.papr)
    if args.capture_dir:
        args.capture_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    print(q.describe(profile))
    print(f"peak={profile.amplitude:.2f}, PAPR={profile.papr_db:.1f}dB, "
          f"{args.trials} trial(s)")
    results = {}
    counts = []
    with bench.radio_pair(args.a, args.b, warmup=3.0) as (radio_a, radio_b):
        if args.direction in ("ab", "both"):
            count, rows = run_direction(radio_a, radio_b,
                                        f"{args.a}->{args.b}", profile,
                                        args.trials, rng, args.capture_dir)
            results[f"{args.a}->{args.b}"] = rows
            counts.append(count)
        if args.direction in ("ba", "both"):
            count, rows = run_direction(radio_b, radio_a,
                                        f"{args.b}->{args.a}", profile,
                                        args.trials, rng, args.capture_dir)
            results[f"{args.b}->{args.a}"] = rows
            counts.append(count)
    document = {
        "profile": q.describe(profile), "fec": profile.fec,
        "payload_bytes": profile.max_payload,
        "payload_bitrate": profile.payload_bitrate,
        "amplitude": profile.amplitude, "papr_db": profile.papr_db,
        "trials": args.trials, "directions": results,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(document, indent=2) + "\n")
        print(f"wrote {args.out}")
    return 0 if min(counts) == args.trials else 1


if __name__ == "__main__":
    sys.exit(main())
