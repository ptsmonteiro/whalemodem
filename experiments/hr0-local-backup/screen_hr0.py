"""Seeded three-trial HR0 viability screen at -15 dB SNR/3kHz."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import hr0  # noqa: E402

from whale.channel import (AwgnChannel, ChannelChain, SnrSpec,  # noqa: E402
                           WattersonChannel)


PRESETS = ("awgn", "mid_latitude_quiet", "mid_latitude_moderate",
           "mid_latitude_disturbed")


def make_channel(name: str, seed: int, snr_db: float):
    noise = AwgnChannel(hr0.SAMPLE_RATE, SnrSpec(snr_db), seed ^ 0x5A5A)
    if name == "awgn":
        return noise
    return ChannelChain((
        WattersonChannel.from_preset(hr0.SAMPLE_RATE, name, seed), noise))


def run(trials: int = 3, snr_db: float = -15.0, seed: int = 20260831):
    rows = []
    for preset_index, preset in enumerate(PRESETS):
        passed = 0
        trial_rows = []
        for trial in range(trials):
            trial_seed = int(np.random.SeedSequence(
                [seed, preset_index, trial]).generate_state(1)[0])
            rng = np.random.default_rng(trial_seed)
            payload = rng.integers(0, 256, hr0.MAX_PAYLOAD_BYTES,
                                   dtype=np.uint8).tobytes()
            transmitted = hr0.modulate(payload)
            channel = make_channel(preset, trial_seed, snr_db)
            started = time.perf_counter()
            impaired = channel.process(transmitted)
            result = hr0.demodulate(impaired.audio)
            elapsed = time.perf_counter() - started
            ok = result["payload"] == payload
            passed += ok
            trial_rows.append({
                "trial": trial + 1, "seed": trial_seed, "decoded": ok,
                "crc_ok": result["crc_ok"], "ldpc_ok": result["ldpc_ok"],
                "ldpc_iterations": result["ldpc_iterations"],
                "wall_seconds": elapsed,
                "channel_measurements": impaired.measurements,
            })
        row = {"channel": preset, "snr_3khz_db": snr_db,
               "passed": passed, "total": trials,
               "delivery_rate": passed / trials, "trials": trial_rows}
        rows.append(row)
        print(f"{preset:24s} {passed}/{trials} decoded at {snr_db:g} dB")
    return {"experiment": "hr0_oracle_start_zero_cfo", "seed": seed,
            "geometry": hr0.describe(), "results": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--snr", type=float, default=-15.0)
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).with_name("screen_results.json"))
    args = parser.parse_args()
    document = run(args.trials, args.snr, args.seed)
    args.out.write_text(json.dumps(document, indent=2, allow_nan=False) + "\n")
    print(f"wrote {args.out}")
    return 0 if all(row["passed"] == row["total"]
                    for row in document["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
