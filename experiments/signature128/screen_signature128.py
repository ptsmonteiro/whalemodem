"""Paired screen for a sub-171 ms repeated MFSK frame signature.

The signal construction is new; Lead2's spectral correlator is reused so the
comparison does not get a detector implementation advantage.  Its module
constants are configured once, before any alphabet is built.
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.lead2 import lead2
from whale import rx_audio
from whale.channel import (AwgnChannel, ChannelChain, ClippingChannel,
                           FrequencyOffsetChannel, SnrKind, SnrSpec,
                           WattersonChannel)
from whale.modes import hc0


TONE_SPACING_HZ = 93.75
FIRST_TONE_HZ = 750.0
TONE_COUNT = 16
NOTE_SAMPLES = 128                 # HC0's 10.67 ms receive-rate symbol
MIN_HOP_STEPS = 2                  # 187.5 Hz minimum adjacent hop
TX_AMPLITUDE = hc0.TX_AMPLITUDE
LABEL_COUNT = 2                    # HC0 and HC1; FM gets a separate format
BLOCK_REPEATS = 2                  # the frame signature is the repeated block
CONSTRUCTION_SEED = 0x1285F       # codebook construction only; not evaluation
SELECTION_SEED = 0x51EC7100       # detector selection/tuning trials
VALIDATION_SEED = 0xA11DA710      # held-out qualification pilot trials
SCORERS = ("sum", "pair-max")


def configure_detector() -> None:
    """Install this experiment's geometry in the reusable correlator."""
    lead2.TONE_SPACING_HZ = TONE_SPACING_HZ
    lead2.FIRST_TONE_HZ = FIRST_TONE_HZ
    lead2.TONE_COUNT = TONE_COUNT
    lead2.TONE_HZ = tuple(FIRST_TONE_HZ + i * TONE_SPACING_HZ
                          for i in range(TONE_COUNT))
    lead2.TONE_COLUMNS = np.round(
        np.asarray(lead2.TONE_HZ) / lead2.PAD_HZ).astype(int)
    lead2.MIN_HOP_STEPS = MIN_HOP_STEPS


@functools.lru_cache(maxsize=None)
def balanced_alphabet(hops: int, size: int = LABEL_COUNT):
    """Build balanced label blocks and repeat each block on air.

    Every label of a given length is a permutation of the same band-spanning
    tones. This prevents a selective fade from making
    one label intrinsically weaker merely because its random draw happened
    to occupy a spectral null. Candidates are admitted only when every
    cyclic cross-correlation has at most two coincident tones.
    """
    if hops % BLOCK_REPEATS:
        raise ValueError("signature length must contain complete repetitions")
    block_hops = hops // BLOCK_REPEATS
    tones = np.round(np.linspace(0, TONE_COUNT - 1, block_hops)).astype(int)
    if len(np.unique(tones)) != block_hops:
        raise ValueError("balanced block needs no more hops than tones")
    minimum_step = max(1, (TONE_COUNT - 1) // max(block_hops - 1, 1))
    coincidence_limit = max(2, block_hops // 3)
    rng = np.random.default_rng(CONSTRUCTION_SEED)
    chosen = []
    for _ in range(200_000):
        candidate = rng.permutation(tones)
        if np.min(np.abs(np.diff(candidate))) < minimum_step:
            continue
        patterns = chosen + [candidate]
        legal = True
        for left_index, left in enumerate(patterns):
            for right_index, right in enumerate(patterns):
                for shift in range(block_hops):
                    if left_index == right_index and shift == 0:
                        continue
                    if np.sum(left == np.roll(right, shift)) > coincidence_limit:
                        legal = False
                        break
                if not legal:
                    break
            if not legal:
                break
        if legal:
            chosen.append(candidate)
            if len(chosen) == size:
                blocks = np.asarray(chosen)
                labels = np.tile(blocks, (1, BLOCK_REPEATS))
                return labels[0].copy(), labels
    raise RuntimeError("could not construct balanced cyclic codebook")


configure_detector()
# The reusable detector asks this function for its patterns. The first return
# value is its legacy burn pattern and is not transmitted by this experiment.
lead2.alphabet = balanced_alphabet


def _repetition_scores(spectra: np.ndarray, quiet: np.ndarray, offset: float,
                       patterns: np.ndarray, per_note: int, count: int,
                       scorer: str) -> np.ndarray | None:
    """Score a twice-repeated block, optionally using repetition diversity.

    ``sum`` is Lead2's original whole-word statistic. ``pair-max`` treats the
    two observations of each block symbol as diversity branches: the stronger
    centred tone observation and its corresponding normalization win. This is
    deliberately evaluated as a selectable experiment, not silently made the
    production default.
    """
    if scorer == "sum":
        return lead2._pattern_scores(spectra, quiet, offset, patterns,
                                     per_note, count)
    if scorer != "pair-max":
        raise ValueError(f"unknown scorer: {scorer}")
    if patterns.shape[1] % BLOCK_REPEATS:
        raise ValueError("pair-max requires complete repetitions")
    columns = lead2.TONE_COLUMNS + int(round(offset / lead2.PAD_HZ))
    if columns.min() < 0 or columns.max() >= spectra.shape[1]:
        return None
    magnitudes = spectra[:, columns]
    centred = magnitudes - magnitudes.mean(axis=1, keepdims=True)
    absolute = np.sum(np.abs(centred), axis=1)
    block_hops = patterns.shape[1] // BLOCK_REPEATS
    scores = np.empty((len(patterns), count))
    for index, sequence in enumerate(patterns):
        hit = np.zeros(count)
        total = np.zeros(count)
        for i, tone in enumerate(sequence[:block_hops]):
            first = centred[i * per_note:i * per_note + count, tone]
            second_at = (i + block_hops) * per_note
            second = centred[second_at:second_at + count, tone]
            choose_second = second > first
            hit += np.where(choose_second, second, first)
            first_total = absolute[i * per_note:i * per_note + count]
            second_total = absolute[second_at:second_at + count]
            total += np.where(choose_second, second_total, first_total)
        scores[index] = hit / np.maximum(total, 1e-30)
    scores[:, quiet] = -np.inf
    return scores


def signature_candidates(audio: np.ndarray, hops: int,
                         limit: int = 32, scorer: str = "sum"
                         ) -> list[tuple[float, int, int]]:
    """Rank plausible boundaries, with both HF labels at each boundary.

    Frame CRC, not a naked correlation argmax, decides which tuple is real.
    Near-duplicate timing and offset hypotheses are collapsed here. Since HF
    has only two labels, trying both checked frame decoders at each boundary
    is more robust than spending the boundary budget on unvalidated labels.
    """
    geometry = lead2.Geometry(NOTE_SAMPLES, hops)
    _, labels = balanced_alphabet(hops)
    step = NOTE_SAMPLES // lead2.SEARCH_DIVISOR
    spectra, rms = lead2._spectra(audio, geometry, step)
    per_note = NOTE_SAMPLES // step
    count = len(spectra) - (hops - 1) * per_note
    if count <= 0:
        return []
    quiet = rms[:count] < np.max(rms) * 0.05
    raw = []
    score_sets = []
    for offset in lead2._hypotheses(lead2.OFFSET_SEARCH_HZ):
        scores = _repetition_scores(spectra, quiet, offset, labels,
                                    per_note, count, scorer)
        if scores is None:
            continue
        score_sets.append(scores)
        take = min(limit * LABEL_COUNT * 3, scores.size)
        indices = np.argpartition(scores.ravel(), -take)[-take:]
        for flat in indices:
            label, at = divmod(int(flat), count)
            raw.append((float(scores[label, at]), label,
                        at * step + geometry.figure_samples))
    boundaries = []
    for candidate in sorted(raw, reverse=True):
        _, _, start = candidate
        if any(abs(start - old_start) <= step for _, old_start in boundaries):
            continue
        boundaries.append((candidate[0], start))
        if len(boundaries) == limit:
            break
    candidates = []
    for _, start in boundaries:
        at = int(round((start - geometry.figure_samples) / step))
        label_scores = [scores[:, at] for scores in score_sets
                        if 0 <= at < scores.shape[1]]
        best = (np.max(np.stack(label_scores), axis=0)
                if label_scores else np.full(LABEL_COUNT, -np.inf))
        for label in np.argsort(best)[::-1]:
            candidates.append((float(best[label]), int(label), start))
    return candidates


def signature(label: int, hops: int, repeats: int = 1) -> np.ndarray:
    """The label itself is the repeated adaptive-head block."""
    geometry = lead2.Geometry(NOTE_SAMPLES, hops)
    _, labels = lead2.alphabet(hops)
    sequence = np.tile(labels[label], repeats)
    table = np.stack([
        lead2.note_audio(geometry, tone, lead2.TX_SAMPLE_RATE)
        for tone in range(TONE_COUNT)
    ])
    return (TX_AMPLITUDE * table[sequence]).reshape(-1).astype(np.float32)


def _channel(sample_rate: int, snr_db: float, seed: int,
             watterson: str | None, offset_hz: float,
             clip: float | None) -> ChannelChain:
    stages = []
    if watterson:
        stages.append(WattersonChannel.from_preset(
            sample_rate, watterson, seed=seed ^ 0x5A5A))
    if offset_hz:
        stages.append(FrequencyOffsetChannel(sample_rate, offset_hz))
    if clip is not None:
        stages.append(ClippingChannel(sample_rate, clip * TX_AMPLITUDE))
    stages.append(AwgnChannel(sample_rate, SnrSpec(db=snr_db, kind=SnrKind.WAVEFORM),
                              seed=seed ^ 0xA5A5))
    return ChannelChain(stages)


def trial(hops: int, snr_db: float, seed: int, watterson: str | None,
          offset_hz: float, clip: float | None, scorer: str = "sum",
          candidate_limit: int = 32) -> dict:
    rng = np.random.default_rng(seed)
    label = int(rng.integers(0, LABEL_COUNT))
    payload = bytes(rng.integers(0, 256, hc0.MAX_PAYLOAD_BYTES,
                                dtype=np.uint8))
    lead = signature(label, hops)
    # Remove HC0's old adaptive head. Its preamble, coded payload and tail
    # are retained unchanged and begin immediately after the candidate.
    encoded = hc0.modulate(payload)
    frame = encoded[hc0.lead_in_samples():]
    prefix = np.zeros(2400, np.float32)  # receiver already listening, 50 ms
    transmitted = np.concatenate((prefix, lead, frame))

    channel = _channel(hc0.SAMPLE_RATE, snr_db, seed, watterson,
                       offset_hz, clip)
    first = channel.process(transmitted)
    tail = channel.drain()
    capture48 = np.concatenate((first.audio, tail.audio,
                                np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES,
                                         np.float32)))
    capture = rx_audio.downsample(capture48)

    decoded = hc0.demodulate(capture)
    hc0_ok = decoded.get("payload") == payload

    geometry = lead2.Geometry(NOTE_SAMPLES, hops)
    found = lead2.detect(capture, geometry=geometry)
    candidates = signature_candidates(capture, hops, limit=candidate_limit,
                                      scorer=scorer)
    true_start = (len(prefix) + len(lead)) // lead2.DECIMATION
    label_ok = bool(found is not None and found.label == label)
    start_error = None if found is None else found.start - true_start
    timing_ok = bool(start_error is not None
                     and abs(start_error) <= NOTE_SAMPLES // 2)
    candidate_ok = any(candidate_label == label
                       and abs(candidate_start - true_start) <= NOTE_SAMPLES // 2
                       for _, candidate_label, candidate_start in candidates)
    # A checked decoder tries candidates in order. A higher-scoring false
    # boundary is harmless; its frame fails validation and the next is tried.
    signature_ok = candidate_ok
    return {
        "hc0_ok": hc0_ok,
        "signature_ok": signature_ok,
        "label_ok": label_ok,
        "timing_ok": timing_ok,
        "candidate_ok": candidate_ok,
        "start_error": start_error,
        "paired_miss": hc0_ok and not signature_ok,
        "score": None if found is None else found.score,
    }


def screen(hops: int, snr_db: float, trials: int, seed: int,
           watterson: str | None, offset_hz: float,
           clip: float | None, scorer: str = "sum",
           candidate_limit: int = 32) -> dict:
    rows = [trial(hops, snr_db, seed + i, watterson, offset_hz, clip, scorer,
                  candidate_limit)
            for i in range(trials)]
    decodable = sum(row["hc0_ok"] for row in rows)
    paired_misses = sum(row["paired_miss"] for row in rows)
    both = sum(row["hc0_ok"] and row["signature_ok"] for row in rows)
    label_misses = sum(row["hc0_ok"] and not row["label_ok"] for row in rows)
    timing_misses = sum(row["hc0_ok"] and row["label_ok"]
                        and not row["timing_ok"] for row in rows)
    paired_start_errors = [abs(row["start_error"]) for row in rows
                           if row["hc0_ok"] and row["label_ok"]
                           and row["start_error"] is not None]
    return {
        "hops": hops,
        "signature_ms": 1000.0 * hops * NOTE_SAMPLES / lead2.RX_SAMPLE_RATE,
        "snr_db": snr_db,
        "watterson": watterson,
        "offset_hz": offset_hz,
        "clip": clip,
        "scorer": scorer,
        "seed": seed,
        "candidate_limit": candidate_limit,
        "trials": trials,
        "hc0_decoded": decodable,
        "signature_and_hc0": both,
        "paired_misses": paired_misses,
        "label_misses": label_misses,
        "timing_misses_after_label": timing_misses,
        "start_error_p95_samples": (float(np.percentile(paired_start_errors, 95))
                                     if paired_start_errors else None),
        "start_error_max_samples": (max(paired_start_errors)
                                     if paired_start_errors else None),
        "paired_miss_rate": (paired_misses / decodable if decodable else None),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=200)
    parser.add_argument("--hops", type=int, nargs="+",
                        default=[8, 10, 12, 14, 16])
    parser.add_argument("--snr", type=float, nargs="+", default=[-12])
    parser.add_argument("--watterson", default=None)
    parser.add_argument("--offset-hz", type=float, default=0.0)
    parser.add_argument("--clip", type=float, default=None)
    parser.add_argument("--seed", type=int, default=VALIDATION_SEED,
                        help="first channel seed (default: held-out validation set)")
    parser.add_argument("--scorer", choices=SCORERS, nargs="+", default=["sum"])
    parser.add_argument("--candidate-limit", type=int, default=32)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    results = []
    for scorer in args.scorer:
      for hops in args.hops:
        for snr_db in args.snr:
            row = screen(hops, snr_db, args.trials, args.seed,
                         args.watterson, args.offset_hz, args.clip, scorer,
                         args.candidate_limit)
            results.append(row)
            rate = row["paired_miss_rate"]
            shown = "n/a" if rate is None else f"{rate:.2%}"
            print(f"{scorer:8s}  {row['signature_ms']:5.0f} ms  {snr_db:+5.1f} dB  "
                  f"HC0 {row['hc0_decoded']:4d}/{args.trials}  "
                  f"paired misses {row['paired_misses']:3d} ({shown}; "
                  f"label {row['label_misses']}, "
                  f"timing {row['timing_misses_after_label']})",
                  flush=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
