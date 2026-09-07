"""AWGN FER/EVM sweep for the HC2 coherent-32QAM top-rung candidate.

This is milestone 3 of the isolated HC2 experiment: establish the AWGN SNR
``hc2_32qam.demodulate`` needs, and measure whether a cheap in-frame
error-vector magnitude reading separates decoding frames from failing ones
well enough to drive a future fallback trigger.

The milestone-3 campaign ran against the identical-training receiver and is
reported in ``RESULTS.md``; that sweep found that every failure above 12.5 dB
was the receiver acquiring the second of two identical training symbols.  The
waveform now sends two *distinct* training sequences and the receiver takes
the plain matched-filter maximum, and the sweep was re-run.  Both sets of
numbers are in ``RESULTS.md``, kept apart.

It is a candidate screen, not mode qualification.  HC2 is not a registered
``WaveformMode``, so ``whale.qualification.run_frame_trial`` cannot drive it;
the trial loop below calls ``modulate``/``demodulate`` directly while reusing
the canonical ``AwgnChannel``/``SnrSpec`` channel and ``trial_seed`` seeding.

Conventions worth stating explicitly:

* Each trial hands the receiver a padded capture: ``--lead-samples`` of
  silence, the frame, then ``--tail-samples`` of silence, all passed through
  one AWGN instance.  The padding therefore reaches the receiver as
  noise-only audio, which is what acquisition must search.
* The ``SnrSpec`` reference interval is the signal-bearing span only
  (``reference_start``/``reference_stop``), so the requested waveform SNR is
  frame power over full-Nyquist-band noise power and does not change when the
  padding length changes.
* EVM is recomputed outside the receiver from the receiver's own acquisition
  diagnostics, repeating its equalization and decision-directed phase track
  bit-for-bit, so measuring it cannot perturb what the receiver decided.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import signal

from whale.channel import AwgnChannel, SnrKind, SnrSpec
from whale.qualification import channel_point_label, trial_seed
from whale.trials import TrialOutcome, TrialResult, TrialRun

from . import hc2_32qam as hc2


# HC2 has no registry mode ID.  This constant only namespaces ``trial_seed``
# so an HC2 campaign is independent of every registered mode's campaign.
SEED_NAMESPACE = 32_999
MODE_NAME = "hc2_32qam"

# Wide enough to bracket the waterfall, dense enough through it to place the
# knee.  Refine or extend with --points; the milestone-3 campaign ran this
# grid at 100 trials and then re-ran subsets at 300 and 1,000.  Note that
# ``trial_seed`` keys on the *index* of a point within --points, so re-running
# a subset only stays paired with an earlier run when the --points list is
# reproduced verbatim.
DEFAULT_POINTS = (0.0, 4.0, 8.0, 9.0, 9.5, 10.0, 10.5, 11.0, 11.5,
                  12.0, 12.5, 13.0, 14.0, 16.0, 20.0)

# Acquisition is called successful when the receiver lands inside the cyclic
# prefix of the true frame start and resolves the (zero) carrier offset to
# within 2 Hz.  A start error of exactly +SYMBOL_SAMPLES is the retired
# identical-training mis-acquisition and is what this tolerance excludes.
START_TOLERANCE_SAMPLES = hc2.GUARD_SAMPLES
FREQUENCY_TOLERANCE_HZ = 2.0

Z_95 = 1.959963984540054


def wilson(passed, total, z=Z_95):
    if total == 0:
        return [0.0, 1.0]
    p = passed / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z / denominator * math.sqrt(
        p * (1 - p) / total + z * z / (4 * total * total))
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def _phase_tracked_grid(rx: np.ndarray, start: int, frequency_hz: float):
    """Repeat the receiver's equalize + decision-directed phase track.

    Returns ``(tracked, channel)`` where ``tracked`` is the payload
    constellation the decoder actually sliced, or ``(None, channel)`` when the
    receiver's own equalizer guard would have rejected the frame.
    """

    samples = np.asarray(rx, dtype=float).reshape(-1)
    analytic = signal.hilbert(samples)
    index = np.arange(len(samples), dtype=float)
    corrected = analytic * np.exp(
        -2j * np.pi * frequency_hz * index / hc2.SAMPLE_RATE)
    grid = hc2._analytic_carriers(corrected, start)
    channel = np.mean(grid[:hc2.TRAINING_SYMBOLS] / hc2._TRAINING, axis=0)
    if not np.all(np.abs(channel) >= 1e-8):
        return None, channel
    equalized = grid[hc2.TRAINING_SYMBOLS:] / channel
    tracked = np.empty_like(equalized)
    phase = 0.0
    for row_index, row in enumerate(equalized):
        provisional = row * np.exp(-1j * phase)
        decisions = hc2.qam32_from_bits(hc2.bits_from_qam32(provisional))
        phase += np.angle(np.sum(provisional * np.conj(decisions)))
        tracked[row_index] = row * np.exp(-1j * phase)
    return tracked, channel


def _rms_evm(error: np.ndarray, reference: np.ndarray) -> float:
    power = float(np.mean(np.abs(reference) ** 2))
    if power <= 0:
        return float("nan")
    return float(np.sqrt(np.mean(np.abs(error) ** 2) / power))


def frame_metrics(rx: np.ndarray, diagnostics: dict, payload: bytes) -> dict:
    """Post-equalization health metrics for one received frame.

    ``evm_percent`` is the decision-directed figure a real receiver could
    compute in-frame with no knowledge of the payload.  ``true_evm_percent``
    uses the transmitted constellation and is an oracle reference: the two
    diverge once slicing errors become common, which is exactly the regime a
    fallback trigger has to survive.
    """

    tracked, channel = _phase_tracked_grid(
        rx, int(diagnostics["start_sample"]),
        float(diagnostics["frequency_offset_hz"]))
    metrics = {
        "channel_gain_rms": float(np.sqrt(np.mean(np.abs(channel) ** 2))),
        "channel_gain_min": float(np.min(np.abs(channel))),
    }
    if tracked is None:
        metrics.update(evm_percent=None, evm_db=None, true_evm_percent=None,
                       raw_ber=None, equalizer_rejected=True)
        return metrics
    decisions = hc2.qam32_from_bits(hc2.bits_from_qam32(tracked)).reshape(
        tracked.shape)
    reference = hc2.qam32_from_bits(hc2._encode_packet(payload)).reshape(
        tracked.shape)
    hard_bits = hc2.bits_from_qam32(tracked)
    coded_bits = hc2._encode_packet(payload)
    evm = _rms_evm(tracked - decisions, decisions)
    metrics.update(
        evm_percent=100.0 * evm,
        evm_db=(20.0 * math.log10(evm) if evm > 0 else None),
        true_evm_percent=100.0 * _rms_evm(tracked - reference, reference),
        symbol_error_rate=float(np.mean(decisions != reference)),
        raw_ber=float(np.mean(hard_bits != coded_bits)),
        equalizer_rejected=False,
    )
    return metrics


def frame_trial(*, snr_db: float, seed: int, trial: int, label: str,
                payload_bytes: int, lead_samples: int, tail_samples: int,
                max_frequency_offset_hz: float, acquisition_step_hz: float):
    """One independent HC2 frame through one seeded AWGN realization."""

    rng = np.random.default_rng(seed)
    payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
    frame = hc2.modulate(payload)
    capture = np.concatenate((np.zeros(lead_samples, np.float32), frame,
                              np.zeros(tail_samples, np.float32)))
    spec = SnrSpec(snr_db, SnrKind.WAVEFORM, reference_start=lead_samples,
                   reference_stop=lead_samples + len(frame))
    channel = AwgnChannel(hc2.SAMPLE_RATE, spec, seed)
    received = channel.process(capture)
    rx = np.asarray(received.audio, dtype=float)

    error = None
    try:
        decoded, diagnostics = hc2.demodulate(
            rx, max_frequency_offset_hz=max_frequency_offset_hz,
            acquisition_step_hz=acquisition_step_hz, return_diagnostics=True)
        metrics = frame_metrics(rx, diagnostics, payload)
    except Exception as exception:  # pragma: no cover - defensive
        return TrialResult(
            trial=trial, direction=label, mode_id=SEED_NAMESPACE,
            mode_name=MODE_NAME, payload_bytes=payload_bytes,
            outcome=TrialOutcome.ERROR, tx_samples=len(capture),
            tx_sample_rate=hc2.SAMPLE_RATE, rx_samples=len(rx),
            rx_sample_rate=hc2.SAMPLE_RATE, keyed_seconds=hc2.FRAME_SECONDS,
            channel_measurements=dict(received.measurements),
            decoder_metrics={}, error=repr(exception))

    start_error = int(diagnostics["start_sample"]) - lead_samples
    acquired = (abs(start_error) <= START_TOLERANCE_SAMPLES
                and abs(float(diagnostics["frequency_offset_hz"]))
                <= FREQUENCY_TOLERANCE_HZ)
    delivered = decoded == payload
    if delivered:
        outcome = TrialOutcome.DECODED
    elif acquired:
        outcome = TrialOutcome.PAYLOAD_FAILED
    else:
        outcome = TrialOutcome.ACQUISITION_FAILED

    decoder_metrics = {
        "start_index": int(diagnostics["start_sample"]),
        "start_error_samples": start_error,
        "cfo_hz": float(diagnostics["frequency_offset_hz"]),
        "acquisition_metric": float(diagnostics["acquisition_metric"]),
        "acquired": bool(acquired),
        "crc_ok": bool(decoded is not None),
        "payload_matched": bool(delivered),
        **metrics,
    }
    return TrialResult(
        trial=trial, direction=label, mode_id=SEED_NAMESPACE,
        mode_name=MODE_NAME, payload_bytes=payload_bytes, outcome=outcome,
        tx_samples=len(capture), tx_sample_rate=hc2.SAMPLE_RATE,
        rx_samples=len(rx), rx_sample_rate=hc2.SAMPLE_RATE,
        keyed_seconds=hc2.FRAME_SECONDS,
        channel_measurements=dict(received.measurements),
        decoder_metrics=decoder_metrics, error=error)


def _quantiles(values):
    finite = [value for value in values if value is not None
              and math.isfinite(value)]
    if not finite:
        return None
    array = np.asarray(finite, dtype=float)
    return {
        "count": int(array.size),
        "min": float(array.min()),
        "p05": float(np.quantile(array, 0.05)),
        "median": float(np.median(array)),
        "mean": float(array.mean()),
        "p95": float(np.quantile(array, 0.95)),
        "max": float(array.max()),
    }


def summarize_point(snr_db: float, trials) -> dict:
    total = len(trials)
    delivered = sum(trial.decoded for trial in trials)
    acquired = sum(bool(trial.decoder_metrics.get("acquired"))
                   for trial in trials)
    errors = sum(trial.outcome is TrialOutcome.ERROR for trial in trials)
    fer = 1.0 - delivered / total if total else 1.0
    delivery = wilson(delivered, total)
    good = [t for t in trials if t.decoded]
    bad = [t for t in trials if not t.decoded]
    evm = lambda group: [t.decoder_metrics.get("evm_percent") for t in group]
    return {
        "snr_db": snr_db,
        "trials": total,
        "delivered": delivered,
        "acquired": acquired,
        "errors": errors,
        "fer": fer,
        "delivery_wilson_95": delivery,
        "fer_wilson_95": [1.0 - delivery[1], 1.0 - delivery[0]],
        "acquisition_wilson_95": wilson(acquired, total),
        "nominal_payload_bps": hc2.SUSTAINED_USER_BIT_RATE,
        "realized_payload_bps": (hc2.SUSTAINED_USER_BIT_RATE * delivered
                                 / total if total else 0.0),
        "evm_percent_decoded": _quantiles(evm(good)),
        "evm_percent_failed": _quantiles(evm(bad)),
        "true_evm_percent_all": _quantiles(
            [t.decoder_metrics.get("true_evm_percent") for t in trials]),
        "raw_ber_decoded": _quantiles(
            [t.decoder_metrics.get("raw_ber") for t in good]),
        "raw_ber_failed": _quantiles(
            [t.decoder_metrics.get("raw_ber") for t in bad]),
    }


def evm_separation(trials) -> dict:
    """Best single decision-directed EVM threshold over all pooled trials.

    A frame is predicted deliverable when ``evm_percent <= threshold``.  The
    reported threshold maximizes agreement with the actual decode outcome;
    the overlap region between the worst decoding frame and the best failing
    frame is reported separately because inside it no threshold can be right.
    """

    pairs = [(trial.decoder_metrics.get("evm_percent"), trial.decoded)
             for trial in trials
             if trial.decoder_metrics.get("evm_percent") is not None]
    if not pairs:
        return {"usable_trials": 0}
    pairs.sort()
    values = np.asarray([value for value, _ in pairs])
    decoded = np.asarray([flag for _, flag in pairs], dtype=bool)
    candidates = np.unique(values)
    best = None
    for threshold in candidates:
        predicted = values <= threshold
        correct = int(np.sum(predicted == decoded))
        if best is None or correct > best[1]:
            best = (float(threshold), correct)
    threshold, correct = best
    predicted = values <= threshold
    good = values[decoded]
    bad = values[~decoded]
    overlap = None
    if good.size and bad.size and float(good.max()) > float(bad.min()):
        overlap = [float(bad.min()), float(good.max())]
    return {
        "usable_trials": len(pairs),
        "threshold_evm_percent": threshold,
        "accuracy": correct / len(pairs),
        "false_accept": int(np.sum(predicted & ~decoded)),
        "false_reject": int(np.sum(~predicted & decoded)),
        "decoded_evm_max": float(good.max()) if good.size else None,
        "failed_evm_min": float(bad.min()) if bad.size else None,
        "overlap_evm_percent": overlap,
        "overlap_trials": (
            int(np.sum((values >= overlap[0]) & (values <= overlap[1])))
            if overlap else 0),
    }


def run(args) -> dict:
    started = datetime.now(timezone.utc)
    clock = time.monotonic()
    records, summaries = [], []
    for point_index, snr_db in enumerate(args.points):
        label = channel_point_label("awgn", snr_db)
        trials = []
        for trial in range(1, args.trials + 1):
            seed = trial_seed(args.seed, SEED_NAMESPACE, point_index, trial)
            trials.append(frame_trial(
                snr_db=snr_db, seed=seed, trial=trial, label=label,
                payload_bytes=args.payload_bytes,
                lead_samples=args.lead_samples,
                tail_samples=args.tail_samples,
                max_frequency_offset_hz=args.max_frequency_offset_hz,
                acquisition_step_hz=args.acquisition_step_hz))
        records.extend(trials)
        row = summarize_point(snr_db, trials)
        summaries.append(row)
        if not args.quiet:
            evm = row["evm_percent_decoded"] or row["evm_percent_failed"] or {}
            print(f"{snr_db:6.2f} dB: {row['delivered']:4d}/{row['trials']:<4d} "
                  f"FER {row['fer']:.3f} "
                  f"[{row['fer_wilson_95'][0]:.3f},{row['fer_wilson_95'][1]:.3f}] "
                  f"{row['realized_payload_bps']:8.1f} bit/s "
                  f"EVM~{evm.get('median', float('nan')):.2f}% "
                  f"({time.monotonic() - clock:.0f}s)", flush=True)

    artifact = {
        "schema": "whalemodem.hc2-awgn-snr-evm.v1",
        "qualification_evidence": False,
        "experiment": "hc2_32qam AWGN SNR/EVM sweep",
        "receiver": "distinct training symbols, matched-filter argmax",
        "started_utc": started.isoformat(),
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": time.monotonic() - clock,
        "seed": args.seed,
        "seed_namespace": SEED_NAMESPACE,
        "trials_per_point": args.trials,
        "capture": {
            "lead_samples": args.lead_samples,
            "tail_samples": args.tail_samples,
            "frame_samples": hc2.FRAME_SAMPLES,
            "snr_reference": "signal-bearing span only (padding excluded)",
            "padding": "silence before AWGN, i.e. noise-only at the receiver",
        },
        "acquisition_search": {
            "max_frequency_offset_hz": args.max_frequency_offset_hz,
            "acquisition_step_hz": args.acquisition_step_hz,
            "start_tolerance_samples": START_TOLERANCE_SAMPLES,
            "frequency_tolerance_hz": FREQUENCY_TOLERANCE_HZ,
        },
        "rate_accounting": hc2.rate_accounting(),
        "summaries": summaries,
        "evm_separation": evm_separation(records),
        "trials": TrialRun(
            channel={"type": "awgn", "sample_rate": hc2.SAMPLE_RATE,
                     "snr_kind": "waveform", "points_db": list(args.points)},
            trials=records, seed=args.seed,
            metadata={"benchmark": "hc2_awgn_snr_evm"},
        ).to_dict()["trials"],
    }
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(artifact, indent=2, allow_nan=False) + "\n")
    return artifact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path,
                        default=Path("logs/scratch/hc2_snr_sweep.json"))
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260830)
    parser.add_argument("--points", type=float, nargs="+",
                        default=list(DEFAULT_POINTS))
    parser.add_argument("--payload-bytes", type=int,
                        default=hc2.MAX_PAYLOAD_BYTES)
    parser.add_argument("--lead-samples", type=int, default=12_000)
    parser.add_argument("--tail-samples", type=int, default=12_000)
    parser.add_argument("--max-frequency-offset-hz", type=float, default=20.0)
    parser.add_argument("--acquisition-step-hz", type=float, default=1.0)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main():
    args = build_parser().parse_args()
    if not 0 <= args.payload_bytes <= hc2.MAX_PAYLOAD_BYTES:
        raise SystemExit(
            f"--payload-bytes must be 0..{hc2.MAX_PAYLOAD_BYTES}")
    run(args)


if __name__ == "__main__":
    main()
