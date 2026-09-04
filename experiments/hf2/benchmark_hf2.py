"""Monte Carlo benchmark harness for the HF2 candidate waveform.

Stage 3 of `experiments/hf2/PLAN.md`: wrap `hf2.py`'s raw `modulate`/
`demodulate` in a thin `WaveformMode`-shaped adapter (unregistered,
benchmark-only -- see PLAN.md step 6 for the later, real promotion) so
`whale.qualification`'s Monte Carlo helpers can run it over AWGN and
Watterson channels, then report acquisition/FER/throughput with Wilson
intervals per `MODE_QUALIFICATION.md`'s statistical gates.

This script does not modify `hf2.py`'s waveform or DSP; it only measures it.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from whale.channel import WATTERSON_PRESETS
from whale.qualification import (channel_factory, channel_point_label,
                                 run_frame_trials)
from whale.trials import TrialRun

from . import hf2


# -- WaveformMode adapter -----------------------------------------------------

@dataclass(frozen=True)
class Hf2BenchMode:
    """Bench-only `WaveformMode` adapter over `hf2.modulate`/`hf2.demodulate`.

    `mode_id=242` marks this as unregistered/placeholder (following
    `experiments/hr1/hr1.py`'s 240 and `hr1b.py`'s 241): HF2 is not on any
    `ModeRegistry` yet (PLAN.md step 6 is a later, separate promotion).
    `whale.qualification.trial_seed` also requires a non-negative mode_id.
    `chunk_size` is `hf2.MAX_PAYLOAD_BYTES` -- HF2's frame is self-contained
    (its own length field + CRC32, `whale.framing`'s PN-sync/air-header
    format bypassed, same as HC0/HC1/VF3 per DESIGN.md), so there is no
    `framing.AIR_HEADER_BYTES` to reserve out of it the way `hc1_mode.py`'s
    `CHUNK_SIZE` does for HC1 riding inside the link's framing.  Every trial
    below passes `payload_bytes=hf2.MAX_PAYLOAD_BYTES` explicitly so
    `run_frame_trial` does not add an air header on top.
    """

    name: str = "hf2"
    mode_id: int = 242
    chunk_size: int = hf2.MAX_PAYLOAD_BYTES
    confidence_threshold: float = hf2.ACQUISITION_THRESHOLD
    tx_sample_rate: int = hf2.SAMPLE_RATE
    rx_sample_rate: int = hf2.RX_SAMPLE_RATE

    def encode(self, payload: bytes, *, include_head: bool = True,
              head_seconds: float = hf2.DEFAULT_HEAD_SECONDS) -> np.ndarray:
        if not include_head:
            head_seconds = hf2.DEFAULT_HEAD_SECONDS
        return hf2.modulate(payload, head_seconds=head_seconds)

    def decode(self, audio: np.ndarray, *,
              head_seconds: float = hf2.DEFAULT_HEAD_SECONDS, **kwargs) -> dict:
        return hf2.demodulate(audio, head_seconds=head_seconds, **kwargs)

    def airtime(self, payload_len: int) -> float:
        del payload_len  # HF2's frame is fixed-length, like HC1's.
        return hf2.frame_seconds()


MODE = Hf2BenchMode()


# -- statistics ----------------------------------------------------------------

def wilson(successes: int, total: int, z: float = 1.959963984540054):
    if total == 0:
        return [None, None]
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z / denominator * math.sqrt(
        p * (1 - p) / total + z * z / (4 * total * total))
    return [centre - margin, centre + margin]


def proportion(successes: int, total: int) -> dict:
    return {
        "count": successes, "total": total,
        "rate": successes / total if total else None,
        "wilson_95": wilson(successes, total),
    }


# -- run -------------------------------------------------------------------

def run_point(args, point_index: int, point_db: float) -> tuple[dict, list]:
    if args.model == "awgn":
        factory = channel_factory("awgn", point_db)
        label = channel_point_label("awgn", point_db)
    else:
        factory = channel_factory("watterson", point_db,
                                  watterson_preset=args.watterson_preset)
        label = channel_point_label("watterson", point_db,
                                    watterson_preset=args.watterson_preset)

    records = run_frame_trials(
        MODE, factory, args.trials, args.seed, point_index, label,
        payload_bytes=hf2.MAX_PAYLOAD_BYTES)

    total = len(records)
    acquired = sum(1 for r in records
                   if (r.decoder_metrics.get("confidence") is not None
                       and float(r.decoder_metrics["confidence"])
                       >= MODE.confidence_threshold))
    decoded = sum(r.decoded for r in records)
    errors = sum(1 for r in records if r.error is not None)
    frame_seconds = MODE.airtime(hf2.MAX_PAYLOAD_BYTES)
    useful_bps = hf2.MAX_PAYLOAD_BYTES * 8 * decoded / (total * frame_seconds)

    row = {
        "model": args.model,
        "watterson_preset": args.watterson_preset if args.model != "awgn" else None,
        "point_db": point_db,
        "label": label,
        "trials": total,
        "acquisition": proportion(acquired, total),
        "frame_success": proportion(decoded, total),
        "frame_error_rate": proportion(total - decoded, total),
        "error_count": errors,
        "payload_bytes": hf2.MAX_PAYLOAD_BYTES,
        "frame_seconds": frame_seconds,
        "useful_bps": useful_bps,
    }
    print(f"{label}: acquire {acquired}/{total} "
          f"(Wilson {row['acquisition']['wilson_95'][0]:.3f}-"
          f"{row['acquisition']['wilson_95'][1]:.3f}), "
          f"decoded {decoded}/{total} "
          f"(FER Wilson-UB {row['frame_error_rate']['wilson_95'][1]:.3f}), "
          f"errors {errors}, {useful_bps:.0f} bit/s")
    return row, records


def run(args):
    summaries, all_records = [], []
    for point_index, point_db in enumerate(args.points):
        row, records = run_point(args, point_index, point_db)
        summaries.append(row)
        all_records.extend(records)

    channel_desc = ({"type": "awgn"} if args.model == "awgn"
                    else {"type": "watterson", "preset": args.watterson_preset})
    channel_desc["points_db"] = list(args.points)
    serialized = TrialRun(
        channel=channel_desc, trials=all_records, seed=args.seed,
        metadata={"benchmark": "hf2_screen", "model": args.model,
                  "watterson_preset": (args.watterson_preset
                                       if args.model != "awgn" else None)},
    ).to_dict()["trials"]

    artifact = {
        "schema": "whalemodem.hf2-benchmark.v1",
        "qualification_evidence": args.model != "awgn",
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed, "trials_per_point": args.trials,
        "model": args.model,
        "watterson_preset": (args.watterson_preset
                             if args.model != "awgn" else None),
        "points_db": list(args.points),
        "summaries": summaries,
        "trials": serialized,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2, allow_nan=False) + "\n")
    print(f"wrote {args.out}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("awgn", "watterson"),
                        default="watterson")
    parser.add_argument("--watterson-preset",
                        choices=sorted(WATTERSON_PRESETS),
                        default="mid_latitude_quiet")
    parser.add_argument("--points", type=float, nargs="+", required=True)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.trials < 1:
        parser.error("--trials must be positive")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
