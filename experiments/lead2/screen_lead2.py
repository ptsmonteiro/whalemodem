"""The screen that sized the lead.

The question is the one that decides whether a uniform mode-independent lead
is affordable: **how short can a figure be and still name the frame and
locate it at the SNR where the weakest mode still decodes?**  HC0 decodes to
-16 dB waveform SNR (`FRAMING.md`), so the lead has to be read correctly
there, and whatever duration that takes becomes the floor
`ADAPTIVE_TIMING.md`'s feedback may never shorten the head below.  The
previous experiment's floor is 0.68 s, which is +20% on HC0's keying and
+98% on HC1's frame; beating that floor is the point of this one.

Every trial goes through the production receive path -- transmit at 48 kHz,
impair at 48 kHz, `rx_audio.downsample` to 12 kHz -- because the lead's
frequency resolution is set by the decode rate, not the transmit rate.  The
lead is followed by an HC0-shaped payload waveform rather than by silence, so
a false lock inside the frame counts as a failure, and preceded by zeros
standing in for the squelch blackout.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from whale import rx_audio                                     # noqa: E402
from whale.channel import (AwgnChannel, ChannelChain,          # noqa: E402
                           ClippingChannel, FrequencyOffsetChannel,
                           SnrSpec, WattersonChannel)
from whale.dsp import mfsk as _mfsk                             # noqa: E402
from whale.modes import hc0                                     # noqa: E402

from experiments.lead2 import lead2                             # noqa: E402

TX_AMPLITUDE = 0.5
#: HC0's payload length, as the thing the lead has to not be confused with.
FRAME_SYMBOLS = 307


def _frame_audio(rng: np.random.Generator) -> np.ndarray:
    tones = rng.integers(0, hc0.TONE_COUNT, FRAME_SYMBOLS)
    return _mfsk.modulate(hc0.BANK, tones, TX_AMPLITUDE)


def _capture(geometry: lead2.Geometry, label: int, burn_repeats: int,
             blackout_notes: int, snr_db: float, offset_hz: float,
             clip: float | None, seed: int, watterson: str | None
             ) -> tuple[np.ndarray, int, int]:
    """One 12 kHz capture, the true frame start, and surviving burn repeats."""
    rng = np.random.default_rng(seed)
    full = (lead2.modulate(label, burn_repeats, geometry=geometry,
                           amplitude=TX_AMPLITUDE) if burn_repeats >= 0
            else np.zeros(0))       # negative means "no lead at all"

    # The blackout eats the *front* of the lead; what is left is what the
    # receiver may use, and what it must be able to count.
    eaten = blackout_notes * geometry.note_samples * lead2.DECIMATION
    kept = full[eaten:]
    transmitted = np.concatenate((
        np.zeros(eaten, np.float64), kept, _frame_audio(rng)))
    frame_start_tx = len(transmitted) - FRAME_SYMBOLS * hc0.SYMBOL_SAMPLES

    stages = []
    if watterson:
        stages.append(WattersonChannel.from_preset(
            lead2.TX_SAMPLE_RATE, watterson, seed=seed ^ 0x5A5A))
    if offset_hz:
        stages.append(FrequencyOffsetChannel(lead2.TX_SAMPLE_RATE, offset_hz))
    if clip is not None:
        stages.append(
            ClippingChannel(lead2.TX_SAMPLE_RATE, clip * TX_AMPLITUDE))
    # SNR is referenced to the lead itself, so the sweep's x-axis means "what
    # the receiver sees of the lead".  With no lead at all -- the false-alarm
    # case -- it falls back to the frame, which has the same amplitude and is
    # also constant-envelope, so the noise level is the same either way.
    reference = ((eaten, frame_start_tx) if frame_start_tx > eaten
                 else (frame_start_tx, len(transmitted)))
    stages.append(AwgnChannel(lead2.TX_SAMPLE_RATE,
                              SnrSpec(db=snr_db,
                                      reference_start=reference[0],
                                      reference_stop=reference[1]),
                              seed=seed ^ 0xA5A5))
    chain = ChannelChain(stages)
    impaired = chain.process(transmitted.astype(np.float32))
    drained = chain.drain()
    audio = np.concatenate((
        np.asarray(impaired.audio, np.float32),
        np.asarray(drained.audio, np.float32),
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, np.float32)))
    captured = rx_audio.downsample(audio)
    # Only whole burn repeats are what the receiver's leading-loss count
    # reports; the figure itself is not a burn repeat.
    kept_notes = len(kept) // (lead2.DECIMATION * geometry.note_samples)
    surviving = max(0, (kept_notes - geometry.figure_notes)
                     // geometry.figure_notes)
    return captured, frame_start_tx // lead2.DECIMATION, surviving


def screen(geometry: lead2.Geometry, snr_db: float, *, burn_repeats: int,
           trials: int, offset_hz: float, clip: float | None,
           blackout_notes: int, seed: int, watterson: str | None) -> dict:
    rng = np.random.default_rng(seed)
    correct = label_ok = 0
    start_errors, offset_errors, burn_errors = [], [], []
    scores, margins = [], []
    elapsed = 0.0
    for _ in range(trials):
        label = int(rng.integers(0, len(lead2.ALPHABET)))
        audio, true_start, surviving = _capture(
            geometry, label, burn_repeats, blackout_notes, snr_db, offset_hz,
            clip, seed=int(rng.integers(1, 2 ** 31)), watterson=watterson)
        began = time.perf_counter()
        found = lead2.detect(audio, geometry=geometry)
        elapsed += time.perf_counter() - began
        if found is None:
            continue
        scores.append(found.score)
        margins.append(found.margin)
        if found.label != label:
            continue
        label_ok += 1
        # A start within half a note is what a mode's own header refines
        # from; anything worse is a miss even with the right label.
        if abs(found.start - true_start) > geometry.note_samples // 2:
            continue
        correct += 1
        start_errors.append(found.start - true_start)
        burn_errors.append(found.burns_observed - surviving)
        offset_errors.append(found.offset_hz - offset_hz)

    def stat(values):
        return {"median": float(np.median(values)) if values else None,
                "p95": (float(np.percentile(np.abs(values), 95))
                        if values else None)}

    return {
        "note_samples": geometry.note_samples,
        "figure_notes": geometry.figure_notes,
        "lead_seconds": geometry.figure_seconds,
        "snr_db": snr_db, "trials": trials, "correct": correct,
        "label_and_timing_rate": correct / trials,
        "label_rate": label_ok / trials,
        "score_median": float(np.median(scores)) if scores else None,
        "score_p05": float(np.percentile(scores, 5)) if scores else None,
        "margin_median": float(np.median(margins)) if margins else None,
        "start_error_samples": stat(start_errors),
        "burn_count_error": stat(burn_errors),
        "burn_count_early": (float(np.mean(np.array(burn_errors) < 0))
                             if burn_errors else None),
        "burn_count_late": (float(np.mean(np.array(burn_errors) > 0))
                            if burn_errors else None),
        "offset_error_hz": stat(offset_errors),
        "detect_ms": 1000.0 * elapsed / max(trials, 1),
        "offset_hz": offset_hz, "clip": clip, "burn_repeats": burn_repeats,
        "blackout_notes": blackout_notes, "watterson": watterson,
    }


def false_alarm(geometry: lead2.Geometry, *, trials: int, seed: int,
                snr_db: float) -> dict:
    """What the detector reports when there is no lead at all.

    A label is always returned -- the statistic is an argmax, not a test --
    so what has to be measured is the score a lead-free capture produces,
    since that is where a threshold would sit.  Anything at or below this is
    indistinguishable from noise plus an HC0 frame.
    """
    rng = np.random.default_rng(seed)
    scores, margins = [], []
    for _ in range(trials):
        audio, _, _ = _capture(geometry, 0, -1, 0, snr_db, 0.0, None,
                               seed=int(rng.integers(1, 2 ** 31)),
                               watterson=None)
        found = lead2.detect(audio, geometry=geometry)
        if found is not None:
            scores.append(found.score)
            margins.append(found.margin)
    return {"note_samples": geometry.note_samples,
            "figure_notes": geometry.figure_notes, "trials": trials,
            "lead_seconds": geometry.figure_seconds,
            "score_p99": float(np.percentile(scores, 99)),
            "score_max": float(np.max(scores)),
            "margin_p99": float(np.percentile(margins, 99))}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=200)
    parser.add_argument("--note-samples", type=int, nargs="+",
                        default=[lead2.DEFAULT.note_samples],
                        help="12 kHz note length; the geometry sweep")
    parser.add_argument("--figure-notes", type=int, nargs="+",
                        default=[lead2.DEFAULT.figure_notes],
                        help="notes in a figure; the length sweep")
    parser.add_argument("--burn-repeats", type=int, default=8,
                        help="burn repeats transmitted, before the blackout")
    parser.add_argument("--blackout-notes", type=int, default=0)
    parser.add_argument("--offset-hz", type=float, default=0.0)
    parser.add_argument("--clip", type=float, default=None,
                        help="hard clip at this multiple of the lead amplitude")
    parser.add_argument("--snr", type=float, nargs="+",
                        default=[-22, -20, -18, -16, -14])
    parser.add_argument("--seed", type=int, default=20260830)
    parser.add_argument("--watterson", type=str, default=None)
    parser.add_argument("--min-hop", type=int, default=None,
                        help="override the alphabet's frequency-diversity "
                             "constraint, to measure what it is worth")
    parser.add_argument("--false-alarm", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if args.min_hop is not None:
        # The alphabet is built from this at import; rebuilding it is the
        # only way to ask what the frequency-diversity constraint is worth.
        lead2.MIN_HOP_STEPS = args.min_hop
        lead2.alphabet.cache_clear()

    rows = []
    for note_samples in args.note_samples:
      for figure_notes in args.figure_notes:
        geometry = lead2.Geometry(note_samples, figure_notes)
        if args.false_alarm:
            row = false_alarm(geometry, trials=args.trials, seed=args.seed,
                              snr_db=args.snr[0])
            rows.append(row)
            print(f"{note_samples:4d}x{figure_notes:<3d} "
                  f"({row['lead_seconds']:.3f}s)  no lead  "
                  f"score p99 {row['score_p99']:.3f}  "
                  f"max {row['score_max']:.3f}  "
                  f"margin p99 {row['margin_p99']:.3f}", flush=True)
            continue
        for snr_db in args.snr:
            row = screen(geometry, snr_db, burn_repeats=args.burn_repeats,
                         trials=args.trials, offset_hz=args.offset_hz,
                         clip=args.clip, blackout_notes=args.blackout_notes,
                         seed=args.seed, watterson=args.watterson)
            rows.append(row)
            print(f"{note_samples:4d}x{figure_notes:<3d} "
                  f"({row['lead_seconds']:.3f}s)  SNR {snr_db:+.0f} dB  "
                  f"label+timing {row['label_and_timing_rate']:6.1%}  "
                  f"label {row['label_rate']:6.1%}  "
                  f"score {row['score_median']:.3f}  "
                  f"start p95 {row['start_error_samples']['p95']}  "
                  f"burn err {row['burn_count_error']['median']}  "
                  f"offset p95 {row['offset_error_hz']['p95']}  "
                  f"{row['detect_ms']:.1f} ms", flush=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
