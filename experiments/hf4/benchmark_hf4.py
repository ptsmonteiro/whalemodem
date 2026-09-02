"""Monte Carlo benchmark harness for the HF4 candidate waveform.

Wraps `hf4.py`'s raw `modulate`/`demodulate` in a thin bench-only
`WaveformMode`-shaped adapter (mirroring `experiments/hf3/benchmark_hf3.py`,
which itself mirrors `experiments/hf2/benchmark_hf2.py`) so
`whale.qualification`'s Monte Carlo helpers can run it over the one channel
point Level 4 requires: benign/static at +13 dB waveform SNR and above (see
SPEED_LADDERS.md). Reports acquisition/FER/useful bit/s with Wilson
intervals per MODE_QUALIFICATION.md's statistical gates.

Like HF3's harness, this script builds the *benign/static* channel point
itself (there is no ready-made "benign/static" entry in
`whale.qualification.channel_factory`, and the shared
`scripts/benchmark_simulated_channels.py` only offers awgn/watterson/fm): a
full measured/reproducible SSB path -- filter, frequency offset + drift,
gain, light nonlinearity (clipping), then waveform-referenced AWGN -- built
directly from `whale.channel`'s stage classes, at the tolerances
SPEED_LADDERS.md defines for benign/static (<=0.1 ms differential delay
spread, <=0.005 Hz Doppler spread). This is deliberately not an
identity/AWGN-only channel; the exact same stage recipe and tolerances HF3
used are reused verbatim here for parity across HF-SSB campaigns.

This script does not modify hf4.py's waveform or DSP; it only measures it.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from whale.channel import (AwgnChannel, ChannelChain, ClippingChannel,
                           FilterChannel, FrequencyOffsetChannel,
                           GainChannel, SnrSpec,
                           WATTERSON_PRESETS, WattersonChannel)
from whale.qualification import (channel_factory, channel_point_label,
                                 run_frame_trials)
from whale.trials import TrialRun

from . import hf4


# -- WaveformMode adapter -----------------------------------------------------

@dataclass(frozen=True)
class Hf4BenchMode:
    """Bench-only `WaveformMode` adapter over `hf4.modulate`/`hf4.demodulate`.

    `mode_id=244` marks this as unregistered/placeholder, following
    `experiments/hf3/benchmark_hf3.py`'s 243 (hf2's 242, hr0's 240/241).
    `chunk_size` is `hf4.MAX_PAYLOAD_BYTES`: HF4's frame is self-contained
    (its own length field + CRC32, `whale.framing`'s PN-sync/air-header
    format bypassed, same choice HC0/HC1/VF3/HF2/HF3 each made
    independently), so there is no `framing.AIR_HEADER_BYTES` to reserve.
    Every trial below passes `payload_bytes=hf4.MAX_PAYLOAD_BYTES` explicitly
    so `run_frame_trial` does not add an air header on top.
    """

    name: str = "hf4"
    mode_id: int = 244
    chunk_size: int = hf4.MAX_PAYLOAD_BYTES
    confidence_threshold: float = hf4.ACQUISITION_THRESHOLD
    tx_sample_rate: int = hf4.SAMPLE_RATE
    rx_sample_rate: int = hf4.RX_SAMPLE_RATE

    def encode(self, payload: bytes, *, include_head: bool = True,
              head_seconds: float = hf4.DEFAULT_HEAD_SECONDS) -> np.ndarray:
        del include_head, head_seconds  # HF4's lead-in is fixed; no knob to vary.
        return hf4.modulate(payload)

    def decode(self, audio: np.ndarray, **kwargs) -> dict:
        return hf4.demodulate(audio, **kwargs)

    def airtime(self, payload_len: int) -> float:
        del payload_len  # HF4's frame is fixed-length, like HC1/HF2/HF3's.
        return hf4.airtime()


MODE = Hf4BenchMode()


# -- benign/static channel ----------------------------------------------------

#: SPEED_LADDERS.md: benign/static is <=0.1 ms differential delay spread and
#: <=0.005 Hz Doppler spread, and must retain a complete filter/frequency-
#: offset/drift/level/nonlinearity description -- not identity/AWGN-only.
#: These are the exact same stage recipe and tolerances
#: `experiments/hf3/benchmark_hf3.py` uses, reused verbatim here so the two
#: HF-SSB campaigns are comparable; see that module's comments for the full
#: rationale (in particular why a `SampleClockChannel` stage and an
#: equal-power second Watterson path were each tried and dropped).
BENIGN_STATIC_DELAY_SECONDS = 0.00005   # 0.05 ms, half the 0.1 ms ceiling
BENIGN_STATIC_DOPPLER_HZ = 0.002        # half the 0.005 Hz ceiling
BENIGN_STATIC_FREQUENCY_OFFSET_HZ = 0.4
BENIGN_STATIC_DRIFT_HZ_PER_SECOND = 0.002
BENIGN_STATIC_CLIP_LIMIT = 0.97
BENIGN_STATIC_GAIN_DB = -2.0
BENIGN_STATIC_TX_BAND_HZ = (250.0, 3_100.0)
BENIGN_STATIC_RX_BAND_HZ = (250.0, 3_100.0)
BENIGN_STATIC_SECOND_PATH_POWER = 0.02


def benign_static_channel(point_db: float, seed: int) -> ChannelChain:
    from whale.channel import WattersonPath
    rate = hf4.SAMPLE_RATE
    paths = (WattersonPath(0.0, BENIGN_STATIC_DOPPLER_HZ),
             WattersonPath(BENIGN_STATIC_DELAY_SECONDS,
                           BENIGN_STATIC_DOPPLER_HZ,
                           power=BENIGN_STATIC_SECOND_PATH_POWER))
    stages = (
        FilterChannel(rate, low_hz=BENIGN_STATIC_TX_BAND_HZ[0],
                     high_hz=BENIGN_STATIC_TX_BAND_HZ[1]),
        FrequencyOffsetChannel(rate, BENIGN_STATIC_FREQUENCY_OFFSET_HZ,
                               BENIGN_STATIC_DRIFT_HZ_PER_SECOND),
        WattersonChannel(rate, paths, seed=seed, preset_name="benign_static"),
        GainChannel(rate, gain_db=BENIGN_STATIC_GAIN_DB),
        ClippingChannel(rate, BENIGN_STATIC_CLIP_LIMIT),
        AwgnChannel(rate, SnrSpec(point_db), seed=seed ^ 0x4257474E),
        FilterChannel(rate, low_hz=BENIGN_STATIC_RX_BAND_HZ[0],
                     high_hz=BENIGN_STATIC_RX_BAND_HZ[1]),
    )
    return ChannelChain(stages)


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
    elif args.model == "benign_static":
        factory = lambda seed: benign_static_channel(point_db, seed)
        label = f"benign_static, waveform SNR {point_db:g} dB"
    else:
        factory = channel_factory("watterson", point_db,
                                  watterson_preset=args.watterson_preset)
        label = channel_point_label("watterson", point_db,
                                    watterson_preset=args.watterson_preset)

    records = run_frame_trials(
        MODE, factory, args.trials, args.seed, point_index, label,
        payload_bytes=hf4.MAX_PAYLOAD_BYTES)

    total = len(records)
    acquired = sum(1 for r in records
                   if (r.decoder_metrics.get("confidence") is not None
                       and float(r.decoder_metrics["confidence"])
                       >= MODE.confidence_threshold))
    decoded = sum(r.decoded for r in records)
    errors = sum(1 for r in records if r.error is not None)
    frame_seconds = MODE.airtime(hf4.MAX_PAYLOAD_BYTES)
    useful_bps = hf4.MAX_PAYLOAD_BYTES * 8 * decoded / (total * frame_seconds)

    row = {
        "model": args.model,
        "watterson_preset": args.watterson_preset if args.model == "watterson" else None,
        "point_db": point_db,
        "label": label,
        "trials": total,
        "acquisition": proportion(acquired, total),
        "frame_success": proportion(decoded, total),
        "frame_error_rate": proportion(total - decoded, total),
        "error_count": errors,
        "payload_bytes": hf4.MAX_PAYLOAD_BYTES,
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

    if args.model == "awgn":
        channel_desc = {"type": "awgn"}
    elif args.model == "benign_static":
        channel_desc = {
            "type": "benign_static",
            "delay_seconds": BENIGN_STATIC_DELAY_SECONDS,
            "doppler_hz": BENIGN_STATIC_DOPPLER_HZ,
            "frequency_offset_hz": BENIGN_STATIC_FREQUENCY_OFFSET_HZ,
            "drift_hz_per_second": BENIGN_STATIC_DRIFT_HZ_PER_SECOND,
            "clip_limit": BENIGN_STATIC_CLIP_LIMIT,
            "gain_db": BENIGN_STATIC_GAIN_DB,
        }
    else:
        channel_desc = {"type": "watterson", "preset": args.watterson_preset}
    channel_desc["points_db"] = list(args.points)
    serialized = TrialRun(
        channel=channel_desc, trials=all_records, seed=args.seed,
        metadata={"benchmark": "hf4_screen", "model": args.model,
                  "watterson_preset": (args.watterson_preset
                                       if args.model == "watterson" else None)},
    ).to_dict()["trials"]

    artifact = {
        "schema": "whalemodem.hf4-benchmark.v1",
        "qualification_evidence": args.model != "awgn",
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed, "trials_per_point": args.trials,
        "model": args.model,
        "watterson_preset": (args.watterson_preset
                             if args.model == "watterson" else None),
        "points_db": list(args.points),
        "summaries": summaries,
        "trials": serialized,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2, allow_nan=False) + "\n")
    print(f"wrote {args.out}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("awgn", "watterson", "benign_static"),
                        default="benign_static")
    parser.add_argument("--watterson-preset",
                        choices=sorted(WATTERSON_PRESETS),
                        default="mid_latitude_quiet")
    parser.add_argument("--points", type=float, nargs="+", required=True)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260901)
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
