"""Offline post-mortem for HF16 hardware captures. Never touches a radio.

A hardware trial reports one bit -- decoded or not -- and every failure looks
the same from outside. This separates them, because the payload was drawn from
a recorded seed and is therefore known, so the *transmitted tone sequence* is
known too.

Ground truth comes from correlating the capture against the whole known tone
sequence, head and sync and payload together. That is several seconds of
matched filter instead of the receiver's one-second preamble, so it finds the
frame far below the point where the mode itself works, and its answer is the
reference the receiver's own acquisition is scored against.

With the true start in hand, four numbers say what actually happened:

  acq_offset      how far the receiver's acquisition was from the truth
  tone_snr        winning-tone-against-the-rest at the true start, which
                  reads about 10*log10(ln M) on pure noise -- the value to
                  compare against before believing any of the others
  symbol_error    fraction of payload symbols whose strongest tone was not
                  the transmitted one, at the true start
  decode_at_truth whether the frame decodes when handed the true start,
                  which is what separates an acquisition failure from a
                  payload one
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np

from whale.dsp import mfsk as _mfsk
from experiments.hf16_mfsk_lowsnr import mfsk_mode as mm
from experiments.hf16_mfsk_lowsnr.mfsk_mode import mode_for


def trial_payload(mode, seed, trial):
    rng = np.random.default_rng(np.random.SeedSequence([seed, trial]))
    return rng.integers(0, 256, mode.max_payload_bytes, dtype=np.uint8).tobytes()


def true_tones(mode, payload):
    """Every tone the transmitter sent, head and sync and payload."""
    return np.concatenate((mode.head_pattern, mode.sync_pattern,
                           mode.payload_tones(payload)))


def locate_truth(mode, capture, payload, search_hz=30.0):
    """Frame start and carrier offset, by matched filter on the whole frame."""
    tones = true_tones(mode, payload)
    step = mode.spacing_hz / mm.OFFSET_STEP_DIVISOR
    n = max(1, int(np.ceil(search_hz / step)))
    best = {"score": -1.0}
    for hz in np.arange(-n, n + 1) * step:
        shifted = mm.shift_hz(capture, hz, mm.RX_SAMPLE_RATE)
        scores, grid = _mfsk.correlate(mode.rx_bank, shifted, tones)
        if not len(scores):
            continue
        peak = int(np.argmax(scores))
        if scores[peak] > best["score"]:
            best = {"score": float(scores[peak]), "start": peak * grid,
                    "coarse_hz": float(hz), "grid": grid}
    if best["score"] < 0:
        return None
    shifted = mm.shift_hz(capture, best["coarse_hz"], mm.RX_SAMPLE_RATE)
    start = _mfsk.refine(mode.rx_bank, shifted, tones, best["start"],
                         radius=best["grid"], step=2)
    # `start` is the head, which is where the whole-frame pattern begins; the
    # receiver's own `start` means the sync pattern. The fine offset estimator
    # must be given the sync pattern's own window -- handed the head's window
    # it reads a different tone sequence and returns noise.
    head = mode.head_symbols * mode.rx_symbol_samples
    sync_start = int(start) + head
    fine = _mfsk.offset_hz(mode.rx_bank, shifted, sync_start, mode.sync_pattern)
    return {"frame_start": int(start), "sync_start": sync_start,
            "offset_hz": float(best["coarse_hz"] + fine),
            "full_corr_score": best["score"]}


def analyse_trial(mode, capture, payload, seed_offset_hz=None):
    truth = locate_truth(mode, capture, payload)
    if truth is None:
        return {"located": False}
    sync_start = truth["sync_start"]
    hz = truth["offset_hz"]

    shifted = mm.shift_hz(capture, hz, mm.RX_SAMPLE_RATE)
    payload_start = sync_start + mode.sync_symbols * mode.rx_symbol_samples
    values = _mfsk.analyze(mode.rx_bank, shifted, payload_start,
                           mode.payload_symbols)
    out = dict(truth)
    out["located"] = True
    if values is None:
        out["payload_window_short"] = True
        return out

    sent = mode.payload_tones(payload)
    got = np.argmax(np.abs(values), axis=1)
    wrong = got != sent
    out["symbol_error"] = float(np.mean(wrong))
    out["tone_snr_db"] = mode.tone_snr_db(values)

    # The honest per-symbol SNR: power in the tone that was actually sent,
    # against the mean power of the M-1 tones that were not. Unlike
    # `tone_snr_db` this does not assume the winner was right, so it keeps
    # meaning below the point where the mode works and goes to 0 dB, not to
    # a floor of 10*log10(ln M), on pure noise. This is the number that
    # compares one configuration, or one moment on the path, with another.
    power = np.abs(values) ** 2
    idx = np.arange(len(sent))
    sent_power = power[idx, sent]
    other_power = (power.sum(axis=1) - sent_power) / (mode.tone_count - 1)
    ratio = np.mean(sent_power) / max(np.mean(other_power), 1e-30)
    out["sent_tone_snr_db"] = float(10 * np.log10(max(ratio - 1.0, 1e-12)))
    # per-decile, to see the fade rather than average it away
    out["sent_tone_snr_db_by_decile"] = [
        float(10 * np.log10(max(np.mean(s) / max(np.mean(o), 1e-30) - 1.0, 1e-12)))
        for s, o in zip(np.array_split(sent_power, 10),
                        np.array_split(other_power, 10))]

    # Where the errors are says what caused them. Bunched in time is a fade
    # or a burst of interference; spread evenly is plain noise; bunched in
    # frequency is a notch or a tone the chain does not carry; and errors
    # landing on the immediate frequency neighbour are a spacing/offset
    # problem rather than a level one.
    deciles = np.array_split(wrong, 10)
    out["error_by_decile"] = [float(np.mean(d)) for d in deciles if len(d)]
    edges = np.linspace(0, mode.tone_count, 9).astype(int)
    out["error_by_tone_octile"] = [
        float(np.mean(wrong[(sent >= lo) & (sent < hi)]))
        if np.any((sent >= lo) & (sent < hi)) else None
        for lo, hi in zip(edges[:-1], edges[1:])]
    if np.any(wrong):
        out["neighbour_error_fraction"] = float(
            np.mean(np.abs(got[wrong].astype(int) - sent[wrong].astype(int)) == 1))
    else:
        out["neighbour_error_fraction"] = None
    out["noise_floor_tone_snr_db"] = float(
        10 * np.log10(max(np.log(mode.tone_count) - 1.0, 1e-12)))

    combined, _ = mode.soft_payload_bits(capture, sync_start, hz)
    decoded, meta = mode.codec.decode_soft(combined)
    out["decode_at_truth"] = decoded == payload
    out["crc_at_truth"] = bool(meta.get("crc_ok"))

    acq = mode.acquire(capture)
    out["acq_start"] = acq["start"]
    out["acq_score"] = acq["score"]
    out["acq_offset_hz"] = acq["offset_hz"]
    out["acq_start_error"] = (None if acq["start"] is None
                              else int(acq["start"] - sync_start))
    out["acq_symbols_off"] = (None if acq["start"] is None else
                              (acq["start"] - sync_start) / mode.rx_symbol_samples)
    return out


def mode_from_config(cfg):
    return mode_for(tone_count=cfg["tone_count"], repeat=cfg["repeat"],
                    constraint=cfg["constraint"],
                    payload_symbols=cfg["payload_symbols"],
                    sync_seconds=cfg["sync_symbols"] * cfg["symbol_seconds"])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--label", action="append", dest="labels")
    args = ap.parse_args(argv)

    meta = json.loads((args.run_dir / "result.json").read_text())
    seed = meta["seed"]
    captures = args.run_dir / "captures"

    for res in meta["results"]:
        if args.labels and res["label"] not in args.labels:
            continue
        cfg = next(c for c in meta["configs"] if c["label"] == res["label"])
        mode = mode_from_config(cfg)
        print(f"\n=== {res['label']}  {mode.describe()}")
        print(f"    pure-noise tone_snr reference: "
              f"{10 * np.log10(max(np.log(mode.tone_count) - 1.0, 1e-12)):.1f} dB")
        for t in res["trials"]:
            name = t.get("capture_file")
            if not name or not (captures / name).exists():
                continue
            cap = np.load(captures / name).astype(np.float64)
            payload = trial_payload(mode, seed, t["trial"])
            a = analyse_trial(mode, cap, payload)
            if not a.get("located"):
                print(f"  t{t['trial']}: frame NOT located")
                continue
            nbr = a["neighbour_error_fraction"]
            nbr_text = "n/a " if nbr is None else f"{nbr:.2f}"
            print(f"  t{t['trial']}: live={t['outcome']:14s} "
                  f"off={a['offset_hz']:+.2f}Hz "
                  f"acq_err={a['acq_symbols_off']:+.2f}sym "
                  f"snr={a['sent_tone_snr_db']:5.1f}dB "
                  f"sym_err={a['symbol_error']:.3f} nbr={nbr_text} "
                  f"decode_at_truth={a['decode_at_truth']}")
            print("        err/decile " + " ".join(f"{v:.2f}" for v in a["error_by_decile"]))
            print("        snr/decile " + " ".join(
                f"{v:5.1f}" for v in a["sent_tone_snr_db_by_decile"]))
            print("        err/octile " + " ".join(
                "  - " if v is None else f"{v:.2f}" for v in a["error_by_tone_octile"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
