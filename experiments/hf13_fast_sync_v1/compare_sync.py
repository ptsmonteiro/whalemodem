"""Offline replay: compare sc.SingleCarrierMode.demodulate() (original) vs.
fast_sync.PatchedMode.demodulate() (fused-FFT sync) on the SAME real
over-the-air captures produced by capture_frames.py.

No radio hardware is touched here -- this is pure offline analysis of
already-captured audio. Both sc.py and fast_sync.py are imported read-only,
never modified.

Run (from the repository root), after capture_frames.py has produced
captures/*.npz:
    python experiments/hf13_fast_sync_v1/compare_sync.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np

from experiments.hf5_8psk_4k import sc
from experiments.hf5_8psk_4k_profiling import fast_sync

CAPTURES_DIR = Path(__file__).resolve().parent / "captures"

BAUD = 1500.0
BITS_PER_SYMBOL = 3
PACKET_BYTES = 2994
PILOT_INTERVAL = 150


def main(argv=None):
    captures = sorted(CAPTURES_DIR.glob("hf13_capture_*.npz"))
    if not captures:
        print(f"no captures found in {CAPTURES_DIR}; run capture_frames.py first")
        return 1

    orig_mode = sc.SingleCarrierMode(baud=BAUD, bits_per_symbol=BITS_PER_SYMBOL,
                                      packet_bytes=PACKET_BYTES, pilot_interval=PILOT_INTERVAL)
    fast_mode = fast_sync.PatchedMode(baud=BAUD, bits_per_symbol=BITS_PER_SYMBOL,
                                       packet_bytes=PACKET_BYTES, pilot_interval=PILOT_INTERVAL)

    rows = []
    n_discrepancies = 0
    print(f"{len(captures)} real captures found\n")
    header = (f"{'trial':>5} {'o_sync':>7} {'f_sync':>7} {'o_crc':>6} {'f_crc':>6} "
              f"{'pay_match':>9} {'o_conf':>8} {'f_conf':>8} {'o_foff':>8} {'f_foff':>8} "
              f"{'o_snr':>7} {'f_snr':>7} {'o_ms':>7} {'f_ms':>7} {'speedup':>8}")
    print(header)

    for cap_path in captures:
        npz = np.load(cap_path)
        audio = npz["audio_12k"].astype(np.float64)
        payload_gt = npz["payload"].tobytes()

        t0 = time.perf_counter()
        orig = orig_mode.demodulate(audio)
        t_orig = time.perf_counter() - t0

        t0 = time.perf_counter()
        fastr = fast_mode.demodulate(audio)
        t_fast = time.perf_counter() - t0

        orig_payload_ok = orig.get("payload") == payload_gt
        fast_payload_ok = fastr.get("payload") == payload_gt
        payload_match = orig.get("payload") == fastr.get("payload")

        discrepancy = (
            orig.get("synced") != fastr.get("synced")
            or orig.get("crc_ok") != fastr.get("crc_ok")
            or not payload_match
        )
        if discrepancy:
            n_discrepancies += 1

        conf_o, conf_f = orig.get("confidence"), fastr.get("confidence")
        foff_o, foff_f = orig.get("freq_offset_hz"), fastr.get("freq_offset_hz")
        snr_o, snr_f = orig.get("channel_snr_db"), fastr.get("channel_snr_db")
        speedup = (t_orig / t_fast) if t_fast > 0 else float("inf")

        row = {
            "trial": cap_path.name,
            "orig_synced": orig.get("synced"), "fast_synced": fastr.get("synced"),
            "orig_crc_ok": orig.get("crc_ok"), "fast_crc_ok": fastr.get("crc_ok"),
            "orig_payload_ok_vs_groundtruth": orig_payload_ok,
            "fast_payload_ok_vs_groundtruth": fast_payload_ok,
            "payload_match_orig_vs_fast": payload_match,
            "orig_confidence": conf_o, "fast_confidence": conf_f,
            "orig_freq_offset_hz": foff_o, "fast_freq_offset_hz": foff_f,
            "orig_channel_snr_db": snr_o, "fast_channel_snr_db": snr_f,
            "orig_demod_ms": t_orig * 1000.0, "fast_demod_ms": t_fast * 1000.0,
            "speedup": speedup,
            "discrepancy": discrepancy,
        }
        rows.append(row)

        def fmt(v, spec=".3f"):
            return "n/a" if v is None else format(v, spec)

        print(f"{cap_path.stem[-3:]:>5} {str(orig.get('synced')):>7} {str(fastr.get('synced')):>7} "
              f"{str(orig.get('crc_ok')):>6} {str(fastr.get('crc_ok')):>6} "
              f"{str(payload_match):>9} {fmt(conf_o):>8} {fmt(conf_f):>8} "
              f"{fmt(foff_o,'.2f'):>8} {fmt(foff_f,'.2f'):>8} {fmt(snr_o,'.1f'):>7} {fmt(snr_f,'.1f'):>7} "
              f"{t_orig*1000:7.1f} {t_fast*1000:7.1f} {speedup:8.2f}x")

    print(f"\n{n_discrepancies} discrepancies out of {len(rows)} real captures")
    avg_speedup = sum(r["speedup"] for r in rows) / len(rows)
    print(f"average real speedup: {avg_speedup:.2f}x")

    out_path = Path(__file__).resolve().parent / "compare_results.json"
    out_path.write_text(json.dumps({
        "captures_dir": str(CAPTURES_DIR),
        "config": {"baud": BAUD, "bits_per_symbol": BITS_PER_SYMBOL,
                   "packet_bytes": PACKET_BYTES, "pilot_interval": PILOT_INTERVAL},
        "rows": rows,
        "n_discrepancies": n_discrepancies,
        "average_speedup": avg_speedup,
    }, indent=2, default=str) + "\n")
    print(f"wrote {out_path}")

    return 1 if n_discrepancies else 0


if __name__ == "__main__":
    sys.exit(main())
