"""Matched AWGN SNR sweep for HF15 K7 puncturing rates.

All rates use the same application payload and the same per-trial seed.  The
mode ID is held constant for seed generation so the channel realization is
identical across rates.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from whale import framing
from whale.qualification import channel_factory, run_frame_trial, trial_seed
from whale.trials import _jsonable

from experiments.hf15_resilient.fec_rate_sweep import FecRateSweepMode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", nargs="+", type=float,
                        default=[6, 8, 10, 12, 14, 16])
    parser.add_argument("--trials", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20260911)
    parser.add_argument("--out", type=Path,
                        default=Path("logs/sim_hf15_fec_rate_awgn_20260911.json"))
    args = parser.parse_args()

    if args.trials < 1:
        parser.error("--trials must be positive")
    modes = [FecRateSweepMode(rate) for rate in ("1/2", "2/3", "3/4")]
    payload_bytes = framing.AIR_HEADER_BYTES + modes[0].chunk_size
    rows = []
    for point_index, point in enumerate(args.points):
        print(f"AWGN {point:g} dB")
        for mode in modes:
            delivered = 0
            acquired = 0
            errors = []
            for trial in range(1, args.trials + 1):
                # Deliberately use one logical mode ID for every rate: matched
                # channel/noise realizations are the point of this benchmark.
                seed = trial_seed(args.seed, 120, point_index, trial)
                result = run_frame_trial(
                    mode, channel_factory("awgn", point)(seed), seed, trial,
                    f"AWGN SNR/3 kHz {point:g} dB", payload_bytes=payload_bytes)
                delivered += int(result.decoded)
                acquired += int(result.outcome.value in ("decoded", "payload_failed"))
                if result.error:
                    errors.append(result.error)
            rows.append({
                "rate": mode.rate,
                "snr_db": point,
                "trials": args.trials,
                "delivered": delivered,
                "delivery_rate": delivered / args.trials,
                "acquired": acquired,
                "acquisition_rate": acquired / args.trials,
                "errors": errors,
                "packet_bytes": mode._packet_bytes,
                "airtime_seconds": mode.airtime(payload_bytes),
                "net_throughput_bps": mode.net_throughput_bps,
            })
            print(f"  {mode.rate}: {delivered}/{args.trials}")
    document = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "trials_per_point": args.trials,
        "payload_bytes": payload_bytes,
        "points_db": args.points,
        "rates": [mode.rate for mode in modes],
        "matched_seed_policy": "trial_seed(seed, 120, point_index, trial)",
        "rows": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(_jsonable(document), indent=2) + "\n",
                         encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
