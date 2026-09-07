"""Sweep HF4 (mode ID 11) TX audio level on IC-7300 -> IC-705.

The level is applied to the encoded transmit audio immediately before
``RadioTransport.send()``.  IC-705 is opened receive-only, so this harness
cannot key it.  HF4's ``channel_snr_db`` is its measured SNR in the standard
3 kHz reference bandwidth; synced-but-CRC-failed frames still report it and
are useful for locating the boundary.

Run from the repository root, for example::

    python scripts/hf4_tx_volume_sweep.py --trials 2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bench
from whale import framing
from whale.modes.hf4_mode import HF4


DEFAULT_LEVELS = (1.00, 0.75, 0.50, 0.35, 0.25, 0.18, 0.12, 0.08)
DEFAULT_TRIALS = 2
CAPTURE_TAIL = 1.0
INTER_TRIAL = 0.5


def run_level(tx, rx, level: float, trials: int, seed: int) -> list[dict]:
    payload_bytes = HF4.chunk_size + framing.AIR_HEADER_BYTES
    rows = []
    print(f"\n== tx audio scale {level:.3f} ({trials} trials) ==")
    for trial in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        rng = np.random.default_rng(np.random.SeedSequence([seed, trial]))
        payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
        audio = HF4.encode(payload)
        tx_audio = (audio * level).astype(np.float32)
        keyed = tx.send(tx_audio)
        time.sleep(CAPTURE_TAIL)
        captured = rx.snapshot_rx()
        result = HF4.decode(captured)
        decoded = result.get("payload") == payload
        row = {
            "level": level,
            "trial": trial,
            "decoded": bool(decoded),
            "outcome": ("decoded" if decoded else
                        "no_sync" if not result.get("synced") else
                        "crc_fail" if not result.get("crc_ok") else
                        "payload_mismatch"),
            "keyed_seconds": keyed,
            "rx_samples_12k": len(captured),
            "confidence": result.get("confidence"),
            "channel_snr_db_3khz": result.get("channel_snr_db"),
            "freq_offset_hz": result.get("freq_offset_hz"),
            "crc_ok": result.get("crc_ok"),
        }
        rows.append(row)
        snr = row["channel_snr_db_3khz"]
        snr_text = "n/a" if snr is None else f"{snr:.2f} dB/3kHz"
        conf = row["confidence"]
        conf_text = "n/a" if conf is None else f"{conf:.3f}"
        print(f"  {trial}/{trials}: keyed={keyed:.2f}s conf={conf_text} "
              f"snr={snr_text} {row['outcome']}")
        if trial != trials:
            time.sleep(INTER_TRIAL)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--levels", type=float, nargs="+", default=DEFAULT_LEVELS)
    ap.add_argument("--seed", type=int, default=20260907)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    if any(level <= 0 or level > 1 for level in args.levels):
        ap.error("every level must be > 0 and <= 1")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or ROOT / "logs" / "mode_qualification" / "hf-ssb" / "hf4" / f"{stamp}-tx-volume-sweep.json"
    records = []
    print(f"HF4 mode_id={HF4.mode_id}: IC-7300 -> IC-705; IC-705 receive-only")
    print(f"levels={', '.join(f'{v:.3f}' for v in args.levels)}; trials/level={args.trials}")
    with bench.radio_pair("ic7300", "ic705", warmup=3.0,
                          b_receive_only=True) as (tx, rx):
        for level in args.levels:
            records.extend(run_level(tx, rx, level, args.trials, args.seed))

    print("\n== SUMMARY ==")
    for level in args.levels:
        rows = [r for r in records if r["level"] == level]
        ok = sum(r["decoded"] for r in rows)
        snrs = [r["channel_snr_db_3khz"] for r in rows
                if r["channel_snr_db_3khz"] is not None]
        snr_text = "n/a" if not snrs else f"{min(snrs):.2f}-{max(snrs):.2f} dB/3kHz"
        print(f"  scale={level:.3f}: {ok}/{len(rows)} decoded; SNR={snr_text}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "mode_id": HF4.mode_id,
        "mode_name": HF4.name,
        "direction": "ic7300->ic705",
        "tx_audio_scale_is_applied_before_send": True,
        "trials": records,
    }, indent=2, allow_nan=False) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
