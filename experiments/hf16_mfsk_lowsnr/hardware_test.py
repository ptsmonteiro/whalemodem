"""Real-radio trial runner for the HF16 MFSK modes.

One keying per frame, no ARQ, no retries: modulate -> TX -> capture ->
demodulate, so a data point is a property of the path and the waveform and
nothing else. Several configurations run inside one radio session, because
opening the pair and waiting out the warm-up costs more than the frames do.

Every raw 12 kHz capture is retained by default. That is the point: receiver
work (acquisition, soft metrics, combining) can then be re-run offline against
real recordings of this path without keying anything, and only the transmit
side needs new airtime. A decode improvement measured that way is real
evidence about the recorded frames and *not* a new hardware result -- see
`replay.py`, which is careful about the distinction.

Direction: --tx is keyed, --rx is opened structurally receive-only (no PTT
backend is constructed, so nothing in the process can key it). The IC-705 is
the transmitter for this campaign, which the repo owner authorised on
2026-09-03; --allow-ic705-tx keeps that an explicit act rather than a default.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import numpy as np

import bench
from whale import transport as _transport
from experiments.hf16_mfsk_lowsnr.mfsk_mode import mode_for

DEFAULT_CAPTURE_TAIL = 1.2
DEFAULT_INTER_TRIAL = 0.5


def parse_config(text: str) -> dict:
    """`M:repeat:K[:frame_seconds[:sync_seconds]]`, e.g. `64:2:7:6.0:1.0`."""
    parts = text.split(":")
    if len(parts) < 3:
        raise argparse.ArgumentTypeError(
            "config must be M:repeat:K[:frame_seconds[:sync_seconds]]")
    cfg = {"tone_count": int(parts[0]), "repeat": int(parts[1]),
           "constraint": int(parts[2])}
    cfg["frame_seconds"] = float(parts[3]) if len(parts) > 3 else 6.0
    cfg["sync_seconds"] = float(parts[4]) if len(parts) > 4 else 0.5
    return cfg


def run_trial(tx, rx, mode, label, trial, trials, seed, *, capture_tail,
              capture_dir, amplitude_note):
    """One keying. Kept separate from the loop so the caller can order the
    (config, trial) plan however it likes -- which matters more than it
    sounds. This path fades for seconds at a time, so running a config's
    trials as a contiguous block confounds the config with whatever the
    channel was doing during that block: screen1 recorded a config at 0/5
    that is strictly more robust than one recorded at 3/5 immediately
    before it, purely because a fade covered its whole block. Configs are
    therefore round-robined, so every config sees the same slow variation.
    """
    rx.consume_rx(len(rx.snapshot_rx()))

    rng = np.random.default_rng(np.random.SeedSequence([seed, trial]))
    payload = rng.integers(0, 256, mode.max_payload_bytes,
                           dtype=np.uint8).tobytes()
    audio = mode.modulate(payload)
    keyed = tx.send(audio)
    time.sleep(capture_tail)
    captured = np.asarray(rx.snapshot_rx(), dtype=np.float64)

    cap_rms = float(np.sqrt(np.mean(captured ** 2))) if captured.size else None
    cap_peak = float(np.max(np.abs(captured))) if captured.size else None
    clipped = int(np.sum(np.abs(captured) >= 0.999)) if captured.size else 0

    result = mode.demodulate(captured)
    decoded = result["payload"] == payload
    if decoded:
        outcome = "decoded"
    elif not result["synced"]:
        outcome = "no_sync"
    elif not result["crc_ok"]:
        outcome = "crc_fail"
    else:
        outcome = "payload_mismatch"

    record = {
        "trial": trial, "label": label, "outcome": outcome,
        "decoded": bool(decoded), "keyed_seconds": keyed,
        "utc": datetime.now(timezone.utc).isoformat(),
        "rx_samples": int(captured.size),
        "sync_score": result["sync_score"],
        "offset_hz": result["offset_hz"],
        "start_index": result["start_index"],
        "tone_snr_db": result["tone_snr_db"],
        "capture_rms": cap_rms, "capture_peak": cap_peak,
        "capture_clipped": clipped,
        "payload_bytes": mode.max_payload_bytes,
        "amplitude": amplitude_note,
    }
    if capture_dir is not None:
        name = f"{label}_t{trial:02d}.npy"
        np.save(capture_dir / name, captured.astype(np.float32))
        record["capture_file"] = name

    snr = record["tone_snr_db"]
    snr_text = "n/a" if snr is None else f"{snr:.1f}dB"
    off = record["offset_hz"]
    off_text = "n/a" if off is None else f"{off:+.2f}Hz"
    print(f"    [{trial}/{trials}] {label:28s} keyed={keyed:5.2f}s "
          f"rms={cap_rms:.4f} peak={cap_peak:.3f}"
          f"{f' CLIP({clipped})' if clipped else ''} "
          f"sync={record['sync_score']:.3f} off={off_text} "
          f"tone_snr={snr_text} {outcome}")
    return record


def summarise(records):
    total = len(records)
    decoded = sum(r["decoded"] for r in records)
    def mean(key):
        vals = [r[key] for r in records if r.get(key) is not None]
        return float(np.mean(vals)) if vals else None
    return {"total": total, "decoded": decoded,
            "decode_rate": decoded / total if total else None,
            "mean_sync_score": mean("sync_score"),
            "mean_tone_snr_db": mean("tone_snr_db"),
            "mean_offset_hz": mean("offset_hz"),
            "mean_capture_rms": mean("capture_rms"),
            "max_capture_peak": max((r["capture_peak"] for r in records
                                     if r["capture_peak"] is not None),
                                    default=None),
            "clipped_trials": sum(1 for r in records if r["capture_clipped"])}


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tx", default="ic705")
    ap.add_argument("--rx", default="ic7300")
    ap.add_argument("--allow-ic705-tx", action="store_true")
    ap.add_argument("--config", dest="configs", action="append",
                    type=parse_config, required=True,
                    help="M:repeat:K[:frame_seconds[:sync_seconds]]")
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--amplitude", type=float, default=0.5)
    ap.add_argument("--capture-tail", type=float, default=DEFAULT_CAPTURE_TAIL)
    ap.add_argument("--inter-trial", type=float, default=DEFAULT_INTER_TRIAL)
    ap.add_argument("--rx-buffer-seconds", type=float, default=None,
                    help="override whale.transport.RX_BUFFER_SECONDS; by "
                         "default it is raised just enough for the longest "
                         "configured frame")
    ap.add_argument("--no-save-captures", action="store_true")
    ap.add_argument("--label", default="")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    if args.tx == "ic705" and not args.allow_ic705_tx:
        ap.error("transmitting on the IC-705 requires --allow-ic705-tx")

    modes = []
    # `whale.transport` keeps 10 s of receive audio, which silently truncates
    # any frame longer than that -- the head and sync would be the part
    # thrown away, so it presents as a total acquisition failure rather than
    # as a short buffer. This campaign deliberately tests frames far longer
    # than the path's multi-second fades, so the buffer is raised here for
    # the experiment's own process. Both call sites read the constant at run
    # time, so setting it before any transport is constructed is enough.
    #
    # This is a real constraint on the design, not just on the harness: a
    # deployed mode with 30 s frames needs a receiver buffer to match.
    longest = max(mode_for(amplitude=args.amplitude, **c).frame_seconds()
                  for c in args.configs)
    wanted = max(_transport.RX_BUFFER_SECONDS,
                 longest + args.capture_tail + 2.0)
    if args.rx_buffer_seconds is not None:
        wanted = args.rx_buffer_seconds
    if wanted != _transport.RX_BUFFER_SECONDS:
        print(f"  raising receive buffer "
              f"{_transport.RX_BUFFER_SECONDS:.1f}s -> {wanted:.1f}s "
              f"for a {longest:.1f}s frame")
        _transport.RX_BUFFER_SECONDS = wanted

    for cfg in args.configs:
        mode = mode_for(amplitude=args.amplitude, **cfg)
        label = (f"M{mode.tone_count}_r{mode.repeat}_K{mode.constraint}"
                 f"_f{cfg['frame_seconds']:g}_s{cfg['sync_seconds']:g}")
        modes.append((label, mode))

    args.out.mkdir(parents=True, exist_ok=True)
    capture_dir = None
    if not args.no_save_captures:
        capture_dir = args.out / "captures"
        capture_dir.mkdir(parents=True, exist_ok=True)

    print(f"hf16 MFSK hardware test {args.label}")
    print(f"  {args.tx}(TX) -> {args.rx}(RX)  trials={args.trials} "
          f"seed={args.seed} amplitude={args.amplitude}")
    airtime = sum(m.frame_seconds() for _, m in modes) * args.trials
    print(f"  {len(modes)} configs, about {airtime:.0f}s of keying total")

    record = {"utc": datetime.now(timezone.utc).isoformat(),
              "tx": args.tx, "rx": args.rx, "seed": args.seed,
              "trials": args.trials, "amplitude": args.amplitude,
              "label": args.label, "configs": [], "results": []}

    for label, mode in modes:
        record["configs"].append({
            "label": label, "tone_count": mode.tone_count,
            "repeat": mode.repeat, "constraint": mode.constraint,
            "spacing_hz": mode.spacing_hz,
            "symbol_seconds": mode.symbol_seconds,
            "sync_symbols": mode.sync_symbols,
            "payload_symbols": mode.payload_symbols,
            "max_payload_bytes": mode.max_payload_bytes,
            "frame_seconds": mode.frame_seconds(),
            "raw_bit_rate": mode.raw_bit_rate(),
            "net_bit_rate": mode.net_bit_rate(),
            "describe": mode.describe()})
        print(f"  {label}: {mode.describe()}")

    by_label = {label: [] for label, _ in modes}
    all_records = []
    with pair_factory(args.tx, args.rx, warmup=3.0,
                      b_receive_only=True) as (txp, rxp):
        # Round 1 of every config, then round 2, and so on -- never a config's
        # trials as a contiguous block. See run_trial's docstring.
        for trial in range(1, args.trials + 1):
            for label, mode in modes:
                rec = run_trial(txp, rxp, mode, label, trial, args.trials,
                                args.seed, capture_tail=args.capture_tail,
                                capture_dir=capture_dir,
                                amplitude_note=args.amplitude)
                by_label[label].append(rec)
                all_records.append(rec)
                time.sleep(args.inter_trial)

    for label, _ in modes:
        record["results"].append({"label": label,
                                  "summary": summarise(by_label[label]),
                                  "trials": by_label[label]})
    record["overall"] = summarise(all_records)
    (args.out / "result.json").write_text(json.dumps(record, indent=1))
    print(f"\nwrote {args.out / 'result.json'}")
    print("\nsummary:")
    for res in record["results"]:
        cfg = next(c for c in record["configs"] if c["label"] == res["label"])
        s = res["summary"]
        print(f"  {res['label']:32s} {s['decoded']:2d}/{s['total']:<2d} "
              f"net={cfg['net_bit_rate']:6.1f}bps "
              f"sync={s['mean_sync_score']:.3f} "
              f"tone_snr={s['mean_tone_snr_db']:.1f}dB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
