"""Controlled over-air runner for VF4 on the IC-705 / HT bench."""

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

import vf4
from whale.hw import audio_io
from whale.hw.radios import RADIOS


class _TimedPtt:
    def __init__(self, delegate):
        self.delegate = delegate
        self.key_state_unknown = False
        self.started = None
        self.keyed_seconds = None

    def reset(self):
        self.started = self.keyed_seconds = None

    def key(self, on):
        outcome = self.delegate.key(on)
        self.key_state_unknown = getattr(self.delegate, "key_state_unknown", False)
        now = time.perf_counter()
        if on:
            self.started = now
        elif self.started is not None:
            self.keyed_seconds = now - self.started
        return outcome


def run_direction(direction, trials, rng, capture_dir):
    tx_name, rx_name = direction.split("-to-")
    tx_radio, rx_radio = RADIOS[tx_name], RADIOS[rx_name]
    tx_device, _ = tx_radio.devices()
    _, rx_device = rx_radio.devices()
    raw_ptt = tx_radio.ptt()
    ptt = _TimedPtt(raw_ptt)
    records = []
    print(f"\n{direction}: {trials} full-capacity frame(s)")
    try:
        for trial in range(1, trials + 1):
            payload = rng.integers(
                0, 256, vf4.MAX_PAYLOAD_BYTES, dtype=np.uint8).tobytes()
            ptt.reset()
            captured = audio_io.capture_while_transmitting(
                vf4.modulate(payload), tx_device, rx_device, ptt,
                samplerate=vf4.SAMPLE_RATE, pre_roll=0.4, post_roll=0.8,
                ptt_lead=0.22, ptt_tail=0.05)
            decoded = vf4.demodulate_debug(captured, payload)
            good = decoded.get("payload") == payload
            stem = f"vf4_{direction.replace('-to-', '_to_')}_t{trial:02d}"
            audio_path, payload_path = capture_dir / f"{stem}.npy", capture_dir / f"{stem}.bin"
            repeat = 2
            while audio_path.exists() or payload_path.exists():
                audio_path = capture_dir / f"{stem}_r{repeat}.npy"
                payload_path = capture_dir / f"{stem}_r{repeat}.bin"
                repeat += 1
            np.save(audio_path, captured)
            payload_path.write_bytes(payload)
            record = {
                "trial": trial, "direction": direction, "decoded": good,
                "keyed_seconds": ptt.keyed_seconds,
                "capture_samples": len(captured), "audio": str(audio_path),
                "payload_file": str(payload_path),
                "confidence": decoded.get("confidence", 0.0),
                "start_index": decoded.get("start_index"),
                "crc_ok": decoded.get("crc_ok", False),
                "rs_ok": decoded.get("rs_ok", False),
                "rs_corrected_bytes": decoded.get("rs_corrected_bytes"),
                "total_bit_errors": decoded.get("total_bit_errors"),
                "ber": decoded.get("ber"),
                "clock_offset_ppm": decoded.get("clock_offset_ppm"),
                "present_carriers": decoded.get("present_carriers"),
                "median_carrier_snr_db": float(np.median(decoded["carrier_snr_db"])),
                "median_payload_evm_db": float(np.median(
                    decoded["symbol_evm_db"][vf4.HEADER_SYMBOLS:])),
                "failure": decoded.get("failure"),
            }
            records.append(record)
            print(f"  {trial}/{trials}: keyed={ptt.keyed_seconds:.2f}s "
                  f"conf={record['confidence']:.3f} "
                  f"BER={record['ber']} RS={record['rs_corrected_bytes']} "
                  f"decoded={good}")
            time.sleep(0.6)
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
    print(vf4.describe())
    records = []
    if args.direction in ("ht-to-ic705", "both"):
        records.extend(run_direction("ht-to-ic705", args.trials, rng, capture_dir))
    if args.direction in ("ic705-to-ht", "both"):
        records.extend(run_direction("ic705-to-ht", args.trials, rng, capture_dir))
    output = {
        "when": datetime.now(timezone.utc).isoformat(),
        "waveform": {
            "sample_rate": vf4.SAMPLE_RATE, "frame_samples": vf4.FRAME_SAMPLES,
            "frame_seconds": vf4.FRAME_SECONDS, "symbol_samples": vf4.SYMBOL_SAMPLES,
            "symbols": vf4.TOTAL_SYMBOLS, "carriers_hz": vf4.CARRIER_HZ.tolist(),
            "modulation": "differential star 8-QAM",
            "bits_per_carrier_symbol": 3,
            "payload_bits": vf4.PAYLOAD_BITS,
            "convolutional_packet_bytes": vf4.PACKET_BYTES,
            "outer_fec": (f"{vf4.RS_BLOCKS}x shortened "
                          f"RS({vf4.RS_CODEWORD_BYTES},{vf4.RS_DATA_BYTES})"),
            "rs_packet_bytes": vf4.RS_PACKET_BYTES,
            "user_payload_bytes": vf4.MAX_PAYLOAD_BYTES,
        },
        "direction": args.direction, "trials_per_direction": args.trials,
        "seed": args.seed, "passed": sum(r["decoded"] for r in records),
        "total": len(records), "records": records,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2) + "\n")
    print(f"\nResult: {output['passed']}/{output['total']} frames decoded byte-for-byte")
    print(f"Wrote {out_path}")
    return 0 if output["passed"] == output["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
