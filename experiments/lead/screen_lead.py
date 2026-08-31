"""AWGN + clipping screen for the musical lead.

The question this answers is the one that decides whether a single
mode-independent lead is affordable: **how long does the arpeggio have to
be to name the frame at the SNR where the weakest mode still decodes?**
HC0 decodes to -16 dB waveform SNR (`FRAMING.md`), so a uniform lead has
to be read correctly there, and whatever duration that takes becomes the
floor `ADAPTIVE_TIMING.md`'s feedback may never shorten the head below.

Every trial goes through the production receive path -- transmit at 48 kHz,
impair at 48 kHz, `rx_audio.downsample` to 12 kHz -- because the lead's
frequency resolution is set by the decode rate, not the transmit rate.

The lead is followed by an HC0-shaped payload waveform rather than by
silence, so a false lock inside the frame counts against the detector, and
preceded by zeros standing in for the squelch blackout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from whale import rx_audio                                    # noqa: E402
from whale.channel import (AwgnChannel, ChannelChain,          # noqa: E402
                           ClippingChannel, FrequencyOffsetChannel,
                           SnrSpec, WattersonChannel)
from whale.dsp import mfsk as _mfsk                            # noqa: E402
from whale.modes import hc0                                    # noqa: E402

from experiments.lead import lead                              # noqa: E402

TX_AMPLITUDE = 0.5
#: HC0's payload length, as the thing the lead has to not be confused with.
FRAME_SYMBOLS = 307


def _frame_audio(rng: np.random.Generator) -> np.ndarray:
    tones = rng.integers(0, hc0.TONE_COUNT, FRAME_SYMBOLS)
    return _mfsk.modulate(hc0.BANK, tones, TX_AMPLITUDE)


def _capture(label: int, cycles: int, blackout_notes: int, snr_db: float,
             offset_hz: float, clip: float | None, seed: int,
             watterson: str | None = None) -> tuple[np.ndarray, int, int]:
    """One 12 kHz capture, plus the true frame start and surviving cycles."""
    rng = np.random.default_rng(seed)
    full = (lead.modulate(label, cycles, amplitude=TX_AMPLITUDE) if cycles >= 0
            else np.zeros(0))   # cycles < 0 means "no lead at all"

    # The blackout eats the *front* of the lead; what is left is what the
    # receiver may use, and what it must be able to count.
    eaten = blackout_notes * lead.NOTE_SAMPLES * lead.DECIMATION
    kept = full[eaten:]
    transmitted = np.concatenate((
        np.zeros(eaten, np.float64), kept, _frame_audio(rng)))
    frame_start_tx = len(transmitted) - FRAME_SYMBOLS * hc0.SYMBOL_SAMPLES

    stages = []
    if watterson:
        stages.append(WattersonChannel.from_preset(
            lead.TX_SAMPLE_RATE, watterson, seed=seed ^ 0x5A5A))
    if offset_hz:
        stages.append(FrequencyOffsetChannel(lead.TX_SAMPLE_RATE, offset_hz))
    if clip is not None:
        stages.append(ClippingChannel(lead.TX_SAMPLE_RATE, clip * TX_AMPLITUDE))
    # SNR is referenced to the lead itself, so the sweep's x-axis means
    # "what the receiver sees of the lead".  With no lead at all -- the
    # false-alarm case -- it falls back to the frame, which has the same
    # amplitude and is also constant-envelope, so the noise level is the
    # same either way.
    reference = ((eaten, frame_start_tx) if frame_start_tx > eaten
                 else (frame_start_tx, len(transmitted)))
    stages.append(AwgnChannel(lead.TX_SAMPLE_RATE,
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
    # `cycles` counts vamp cycles; the lead is one cycle longer than that
    # because of the cadence, and only whole *vamp* cycles are what the
    # receiver's leading-loss count reports.
    kept_notes = len(kept) // (lead.DECIMATION * lead.NOTE_SAMPLES)
    surviving = max(0, (kept_notes - lead.CYCLE_NOTES) // lead.CYCLE_NOTES)
    return captured, frame_start_tx // lead.DECIMATION, surviving


def screen(snr_db: float, decision_cycles: int, *, cycles: int, trials: int,
           offset_hz: float, clip: float | None, blackout_notes: int,
           seed: int, watterson: str | None = None) -> dict:
    rng = np.random.default_rng(seed)
    correct = 0
    start_errors = []
    offset_errors = []
    cycle_errors = []
    margins = []
    cadence_scores = []
    for trial in range(trials):
        label = int(rng.integers(0, len(lead.ALPHABET)))
        audio, true_start, surviving = _capture(
            label, cycles, blackout_notes, snr_db, offset_hz, clip,
            seed=int(rng.integers(1, 2 ** 31)), watterson=watterson)
        found = lead.detect(audio, decision_cycles=decision_cycles)
        if found is None:
            continue
        margins.append(found.margin)
        cadence_scores.append(found.cadence_score)
        if found.label != label:
            continue
        # A start within half a note is what a mode's own header needs to
        # refine from; anything worse is a miss even with the right label.
        if abs(found.start - true_start) > lead.NOTE_SAMPLES // 2:
            continue
        correct += 1
        start_errors.append(found.start - true_start)
        cycle_errors.append(found.cycles_observed - surviving)
        offset_errors.append(found.offset_hz - offset_hz)

    def stat(values):
        return {"median": float(np.median(values)) if values else None,
                "p95": float(np.percentile(np.abs(values), 95)) if values else None}

    return {
        "snr_db": snr_db, "decision_cycles": decision_cycles,
        "lead_seconds": ((decision_cycles + 1) * lead.CYCLE_NOTES
                         * lead.NOTE_SECONDS),
        "trials": trials, "correct": correct,
        "label_and_timing_rate": correct / trials,
        "margin_median": float(np.median(margins)) if margins else None,
        "cadence_score_median": (float(np.median(cadence_scores))
                                 if cadence_scores else None),
        "start_error_samples": stat(start_errors),
        "cycle_count_error": stat(cycle_errors),
        "offset_error_hz": stat(offset_errors),
        "offset_hz": offset_hz, "clip": clip,
        "blackout_notes": blackout_notes, "watterson": watterson,
    }


def false_alarm(decision_cycles: int, *, trials: int, seed: int) -> dict:
    """What the detector reports when there is no lead at all.

    A label is always returned -- the statistic is an argmax, not a test --
    so what has to be measured is the `margin` a lead-free capture produces,
    since that is the quantity a threshold would be set on.  Anything at or
    below this is indistinguishable from noise plus an HC0 frame.
    """
    rng = np.random.default_rng(seed)
    margins, scores = [], []
    for _ in range(trials):
        audio, _, _ = _capture(0, -1, 0, -16.0, 0.0, None,
                               seed=int(rng.integers(1, 2 ** 31)))
        found = lead.detect(audio, decision_cycles=decision_cycles)
        if found is not None:
            margins.append(found.margin)
            scores.append(found.cadence_score)
    return {"decision_cycles": decision_cycles, "trials": trials,
            "margin_p99": float(np.percentile(margins, 99)),
            "margin_max": float(np.max(margins)),
            "cadence_p99": float(np.percentile(scores, 99))}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=200)
    parser.add_argument("--cycles", type=int, default=8,
                        help="vamp cycles transmitted, before the blackout "
                             "eats any and before the cadence is appended")
    parser.add_argument("--blackout-notes", type=int, default=0)
    parser.add_argument("--offset-hz", type=float, default=0.0)
    parser.add_argument("--clip", type=float, default=None,
                        help="hard clip at this multiple of the lead amplitude")
    parser.add_argument("--snr", type=float, nargs="+",
                        default=[-22, -20, -18, -16, -14, -12, -10])
    parser.add_argument("--decision-cycles", type=int, nargs="+",
                        default=[1, 2, 3, 4])
    parser.add_argument("--seed", type=int, default=20260830)
    parser.add_argument("--watterson", type=str, default=None)
    parser.add_argument("--false-alarm", action="store_true",
                        help="measure the lead-free margin instead of sweeping")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    rows = []
    if args.false_alarm:
        for decision_cycles in args.decision_cycles:
            row = false_alarm(decision_cycles, trials=args.trials,
                              seed=args.seed)
            rows.append(row)
            print(f"{decision_cycles} cyc  no lead  "
                  f"margin p99 {row['margin_p99']:.3f}  "
                  f"max {row['margin_max']:.3f}  "
                  f"cadence p99 {row['cadence_p99']:.3f}", flush=True)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(rows, indent=2))
        return

    for decision_cycles in args.decision_cycles:
        for snr_db in args.snr:
            row = screen(snr_db, decision_cycles, cycles=args.cycles,
                         trials=args.trials, offset_hz=args.offset_hz,
                         clip=args.clip, blackout_notes=args.blackout_notes,
                         seed=args.seed, watterson=args.watterson)
            rows.append(row)
            print(f"{decision_cycles} cyc ({row['lead_seconds']:.2f}s)  "
                  f"SNR {snr_db:+.0f} dB  "
                  f"label+timing {row['label_and_timing_rate']:6.1%}  "
                  f"margin {row['margin_median']:.3f}  "
                  f"cadence {row['cadence_score_median']:.3f}  "
                  f"start p95 {row['start_error_samples']['p95']}  "
                  f"offset p95 {row['offset_error_hz']['p95']}", flush=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
