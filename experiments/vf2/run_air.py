"""Controlled over-air runner for the IC-705 / HT VF2 link."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

import vf2
from whale.hw import audio_io
from whale.hw.radios import RADIOS


CAPTURE_TAIL_SECONDS = 0.8
INTER_TRIAL_SECONDS = 0.6
PTT_LEAD_SECONDS = 0.22
PTT_TAIL_SECONDS = 0.05


class _TimedPtt:
    """Delegate PTT control while measuring confirmed key-to-unkey time."""

    def __init__(self, delegate):
        self.delegate = delegate
        self.key_state_unknown = False
        self.started = None
        self.keyed_seconds = None

    def reset(self):
        self.started = None
        self.keyed_seconds = None

    def key(self, on):
        outcome = self.delegate.key(on)
        self.key_state_unknown = getattr(self.delegate, "key_state_unknown", False)
        now = time.perf_counter()
        if on:
            self.started = now
        elif self.started is not None:
            self.keyed_seconds = now - self.started
        return outcome


def _jsonable(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    raise TypeError(type(value).__name__)


def run_direction(direction, trials, rng, capture_dir):
    records = []
    tx_name, rx_name = direction.split("-to-")
    tx_radio, rx_radio = RADIOS[tx_name], RADIOS[rx_name]
    tx_device, _ = tx_radio.devices()
    _, rx_device = rx_radio.devices()
    print(f"\n{direction}: {trials} full-capacity frame(s)")
    raw_ptt = tx_radio.ptt()
    ptt = _TimedPtt(raw_ptt)
    try:
        for trial in range(1, trials + 1):
            payload = rng.integers(
                0, 256, vf2.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
            transmitted = vf2.modulate(payload)
            ptt.reset()
            captured = audio_io.capture_while_transmitting(
                transmitted, tx_device, rx_device, ptt,
                samplerate=vf2.SAMPLE_RATE, pre_roll=0.4,
                post_roll=CAPTURE_TAIL_SECONDS,
                ptt_lead=PTT_LEAD_SECONDS, ptt_tail=PTT_TAIL_SECONDS)
            keyed = ptt.keyed_seconds
            decoded = vf2.demodulate_debug(captured, payload)
            good = decoded.get("payload") == payload

            stem = f"vf2_{direction.replace('-to-', '_to_')}_t{trial:02d}"
            audio_path = capture_dir / f"{stem}.npy"
            payload_path = capture_dir / f"{stem}.bin"
            suffix = 2
            while audio_path.exists() or payload_path.exists():
                audio_path = capture_dir / f"{stem}_r{suffix}.npy"
                payload_path = capture_dir / f"{stem}_r{suffix}.bin"
                suffix += 1
            np.save(audio_path, captured)
            payload_path.write_bytes(payload)

            record = {
                "trial": trial,
                "direction": direction,
                "decoded": good,
                "keyed_seconds": keyed,
                "capture_samples": len(captured),
                "capture_seconds": len(captured) / vf2.SAMPLE_RATE,
                "audio": str(audio_path),
                "payload_file": str(payload_path),
                "confidence": decoded.get("confidence", 0.0),
                "start_index": decoded.get("start_index"),
                "crc_ok": decoded.get("crc_ok", False),
                "total_bit_errors": decoded.get("total_bit_errors"),
                "ber": decoded.get("ber"),
                "cfo_hz": decoded.get("cfo_hz"),
                "clock_offset_ppm": decoded.get("clock_offset_ppm"),
                "present_carriers": decoded.get("present_carriers"),
                "median_carrier_snr_db": float(np.median(decoded["carrier_snr_db"])),
                "median_payload_evm_db": float(np.median(
                    decoded["symbol_evm_db"][vf2.HEADER_SYMBOLS:])),
                "failure": decoded.get("failure"),
            }
            records.append(record)
            keyed_text = f"{keyed:.2f}s" if keyed is not None else "unknown"
            clock = record["clock_offset_ppm"]
            clock_text = f"{clock:.1f}ppm" if clock is not None else "unknown"
            print(f"  {trial}/{trials}: keyed={keyed_text} "
                  f"conf={record['confidence']:.3f} "
                  f"clock={clock_text} "
                  f"BER={record['ber']} decoded={good}")
            time.sleep(INTER_TRIAL_SECONDS)
    finally:
        raw_ptt.close()
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direction", choices=("ht-to-ic705", "ic705-to-ht", "both"),
                        default="both")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260820)
    parser.add_argument("--capture-dir", default=str(HERE / "results" / "captures"))
    parser.add_argument("--out", default=str(HERE / "results" / "air_test.json"))
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials must be positive")

    capture_dir = Path(args.capture_dir)
    capture_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    print(vf2.describe())
    print("Opening both radios; current radio frequencies/settings are left untouched.")
    records = []
    if args.direction in ("ht-to-ic705", "both"):
        records.extend(run_direction(
            "ht-to-ic705", args.trials, rng, capture_dir))
    if args.direction in ("ic705-to-ht", "both"):
        records.extend(run_direction(
            "ic705-to-ht", args.trials, rng, capture_dir))

    output = {
        "when": datetime.now(timezone.utc).isoformat(),
        "waveform": {
            "sample_rate": vf2.SAMPLE_RATE,
            "frame_samples": vf2.FRAME_SAMPLES,
            "frame_seconds": vf2.FRAME_SECONDS,
            "symbol_samples": vf2.SYMBOL_SAMPLES,
            "symbols": vf2.TOTAL_SYMBOLS,
            "carriers_hz": vf2.CARRIER_HZ.tolist(),
            "payload_bits": vf2.PAYLOAD_BITS,
            "user_payload_bytes": vf2.MAX_PAYLOAD_BYTES,
        },
        "direction": args.direction,
        "trials_per_direction": args.trials,
        "seed": args.seed,
        "passed": sum(record["decoded"] for record in records),
        "total": len(records),
        "records": records,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, default=_jsonable) + "\n")
    print(f"\nResult: {output['passed']}/{output['total']} frames decoded byte-for-byte")
    print(f"Wrote {out_path}")
    return 0 if output["passed"] == output["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
