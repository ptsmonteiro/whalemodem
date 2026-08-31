"""Watterson boundary sweep for the HC2 coherent-32QAM top-rung candidate.

This is milestone 4 of the isolated HC2 experiment.  Milestone 3 fixed the
AWGN thermal-noise floor; this one asks the different question a *top* ladder
rung has to answer: **how favourable must the propagation be**, and when the
answer is "more favourable than this", does the mode fail cleanly enough that
a link controller can notice and demote?

HC2 is deliberately speed-first.  It is not required to survive moderate or
disturbed HF; the lower rungs supply that.  A failure under those presets is
therefore a boundary measurement, not a defect, and this harness is built to
*characterize* the boundary rather than to pass a gate.

Why a sibling script instead of extending ``benchmark_hc2_snr``
--------------------------------------------------------------
The AWGN sweep's point space is one dimensional (a list of SNRs) and
``trial_seed`` keys on the index of a point within that list, which is what
makes the committed milestone-3 and post-fix campaigns paired and comparable.
This campaign's point space is three dimensional -- differential delay,
frequency spread, waveform SNR -- so folding it into the same ``--points``
list would have had to renumber the AWGN point indices and silently break
that pairing.  The artifact schema differs for the same reason.

What is *not* duplicated: ``frame_metrics``, ``wilson``, ``_quantiles`` and
``evm_separation`` are imported from ``benchmark_hc2_snr``, so the EVM figure
quoted here has exactly the same definition as the one the AWGN campaign
calibrated the 10% fallback trigger against, and the AWGN curve remains a
valid control.

Channel construction
--------------------
Each trial chains ``WattersonChannel`` into ``AwgnChannel`` in that order,
matching ``whale.qualification.channel_factory("watterson", ...)`` including
its ``seed ^ 0x5A5A`` noise seed.  Preset points build their paths through
``WATTERSON_PRESETS``; parametric points construct the same two-path,
equal-power geometry directly with ``WattersonPath`` so delay and spread can
be moved one at a time.

The SNR reference stays the signal-bearing span of the *transmitted* capture,
as in the AWGN sweep, so a requested SNR means frame power (after fading)
over full-Nyquist-band noise power.  Under fading that reference is a
per-realization average: a frame that spends most of its 2.928 s in a deep
fade still gets noise scaled to its own mean power, which is the honest
convention but means the instantaneous SNR inside a fade is much worse than
the label.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import signal

from whale.channel import (WATTERSON_PRESETS, AwgnChannel, ChannelChain,
                           SnrSpec, WattersonChannel, WattersonPath)
from whale.qualification import channel_point_label, trial_seed
from whale.trials import TrialOutcome, TrialResult, TrialRun

from . import hc2_32qam as hc2
from .benchmark_hc2_snr import (FREQUENCY_TOLERANCE_HZ, MODE_NAME,
                                SEED_NAMESPACE, START_TOLERANCE_SAMPLES,
                                _quantiles, evm_separation, frame_metrics,
                                wilson)


# The retired identical-training mis-acquisition landed exactly one OFDM
# symbol late.  It is counted by name at every point so a regression would be
# visible rather than buried in a start-error distribution.
MIS_ACQUISITION_START_ERROR = hc2.SYMBOL_SAMPLES

# The AWGN campaign's recommended fallback trigger.  Reported, not tuned here.
EVM_TRIGGER_PERCENT = 10.0

# Bins used to summarize how stale the frame-start channel estimate becomes.
DRIFT_HEAD_SYMBOLS = 30
DRIFT_TAIL_SYMBOLS = 30


@dataclass(frozen=True)
class ChannelPoint:
    """One (differential delay, frequency spread, SNR) operating point."""

    delay_seconds: float
    spread_hz: float
    snr_db: float
    preset: str | None = None

    @classmethod
    def from_preset(cls, preset: str, snr_db: float) -> "ChannelPoint":
        try:
            definition = WATTERSON_PRESETS[preset]
        except KeyError:
            raise ValueError(
                f"unknown Watterson preset {preset!r}; "
                f"have {sorted(WATTERSON_PRESETS)}") from None
        return cls(definition.differential_delay_seconds,
                   definition.frequency_spread_hz, snr_db, definition.name)

    @property
    def label(self) -> str:
        if self.preset is not None:
            return channel_point_label("watterson", self.snr_db,
                                       watterson_preset=self.preset)
        return (f"delay {self.delay_seconds * 1e3:g} ms, "
                f"spread {self.spread_hz:g} Hz, "
                f"waveform SNR {self.snr_db:g} dB")

    def paths(self) -> tuple[WattersonPath, WattersonPath]:
        """The F.1487 two-path, equal-power geometry the presets also use."""
        return (WattersonPath(0.0, self.spread_hz),
                WattersonPath(self.delay_seconds, self.spread_hz))

    def describe(self) -> dict:
        return {"preset": self.preset,
                "differential_delay_seconds": self.delay_seconds,
                "differential_delay_ms": self.delay_seconds * 1e3,
                "frequency_spread_hz": self.spread_hz,
                "snr_db": self.snr_db,
                "label": self.label,
                "cyclic_prefix_ms": hc2.GUARD_SAMPLES / hc2.SAMPLE_RATE * 1e3,
                "frame_seconds": hc2.FRAME_SECONDS}

    def channel(self, seed: int, reference: tuple[int, int]):
        start, stop = reference
        return ChannelChain((
            WattersonChannel(hc2.SAMPLE_RATE, self.paths(), seed,
                             preset_name=self.preset),
            AwgnChannel(hc2.SAMPLE_RATE,
                        SnrSpec(self.snr_db, reference_start=start,
                                reference_stop=stop),
                        seed ^ 0x5A5A),
        ))


def channel_evolution(rx: np.ndarray, diagnostics: dict,
                      payload: bytes) -> dict:
    """Oracle measurement of how stale the frame-start channel estimate goes.

    HC2 estimates one complex gain per carrier from the two training symbols
    at the head of the frame and then corrects only a scalar common phase for
    the remaining 2.9 s.  This recovers the *true* per-carrier channel at
    every payload symbol by dividing the received grid by the known
    transmitted constellation, removes the common phase the receiver would
    have tracked, and reports how far the true channel has moved away from
    the frame-start estimate early in the frame versus late in it.

    It is an oracle diagnostic -- it uses the transmitted payload -- and
    exists only to attribute failures to channel coherence time rather than
    to noise.  It is expensive (a second Hilbert transform over the capture),
    so it is gated behind ``--channel-diagnostics``.
    """

    samples = np.asarray(rx, dtype=float).reshape(-1)
    analytic = signal.hilbert(samples)
    index = np.arange(len(samples), dtype=float)
    corrected = analytic * np.exp(
        -2j * np.pi * float(diagnostics["frequency_offset_hz"])
        * index / hc2.SAMPLE_RATE)
    grid = hc2._analytic_carriers(corrected, int(diagnostics["start_sample"]))
    estimate = np.mean(grid[:hc2.TRAINING_SYMBOLS] / hc2._TRAINING, axis=0)
    if not np.all(np.abs(estimate) >= 1e-8):
        return {"channel_estimate_degenerate": True}

    reference = hc2.qam32_from_bits(hc2._encode_packet(payload)).reshape(
        hc2.PAYLOAD_SYMBOLS, hc2.N_CARRIERS)
    truth = grid[hc2.TRAINING_SYMBOLS:] / reference
    # Remove the scalar the decision-directed tracker would have removed, so
    # what remains is the part of the drift HC2 genuinely cannot correct.
    common = np.sum(truth * np.conj(estimate), axis=1)
    common /= np.maximum(np.abs(common), 1e-30)
    residual = truth - estimate * common[:, None]
    per_symbol = (np.sqrt(np.mean(np.abs(residual) ** 2, axis=1))
                  / np.sqrt(np.mean(np.abs(estimate) ** 2)))
    gains = np.abs(estimate)
    return {
        "channel_estimate_degenerate": False,
        "residual_head_percent": 100.0 * float(
            np.mean(per_symbol[:DRIFT_HEAD_SYMBOLS])),
        "residual_tail_percent": 100.0 * float(
            np.mean(per_symbol[-DRIFT_TAIL_SYMBOLS:])),
        "residual_frame_percent": 100.0 * float(np.mean(per_symbol)),
        "carrier_gain_spread_db": 20.0 * math.log10(
            float(np.max(gains)) / max(float(np.min(gains)), 1e-30)),
        "common_phase_swing_deg": float(np.rad2deg(np.max(
            np.abs(np.unwrap(np.angle(common)))))),
    }


def frame_trial(*, point: ChannelPoint, seed: int, trial: int,
                payload_bytes: int, lead_samples: int, tail_samples: int,
                max_frequency_offset_hz: float, acquisition_step_hz: float,
                channel_diagnostics: bool) -> TrialResult:
    """One independent HC2 frame through one seeded Watterson+AWGN chain."""

    rng = np.random.default_rng(seed)
    payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
    frame = hc2.modulate(payload)
    capture = np.concatenate((np.zeros(lead_samples, np.float32), frame,
                              np.zeros(tail_samples, np.float32)))
    channel = point.channel(seed, (lead_samples, lead_samples + len(frame)))
    received = channel.process(capture)
    rx = np.asarray(received.audio, dtype=float)

    try:
        decoded, diag = hc2.demodulate(
            rx, max_frequency_offset_hz=max_frequency_offset_hz,
            acquisition_step_hz=acquisition_step_hz, return_diagnostics=True)
        metrics = frame_metrics(rx, diag, payload)
        if channel_diagnostics:
            metrics.update(channel_evolution(rx, diag, payload))
    except Exception as exception:  # pragma: no cover - defensive
        return TrialResult(
            trial=trial, direction=point.label, mode_id=SEED_NAMESPACE,
            mode_name=MODE_NAME, payload_bytes=payload_bytes,
            outcome=TrialOutcome.ERROR, tx_samples=len(capture),
            tx_sample_rate=hc2.SAMPLE_RATE, rx_samples=len(rx),
            rx_sample_rate=hc2.SAMPLE_RATE, keyed_seconds=hc2.FRAME_SECONDS,
            channel_measurements=dict(received.measurements),
            decoder_metrics={}, error=repr(exception))

    start_error = int(diag["start_sample"]) - lead_samples
    acquired = (abs(start_error) <= START_TOLERANCE_SAMPLES
                and abs(float(diag["frequency_offset_hz"]))
                <= FREQUENCY_TOLERANCE_HZ)
    delivered = decoded == payload
    if delivered:
        outcome = TrialOutcome.DECODED
    elif acquired:
        outcome = TrialOutcome.PAYLOAD_FAILED
    else:
        outcome = TrialOutcome.ACQUISITION_FAILED

    decoder_metrics = {
        "start_index": int(diag["start_sample"]),
        "start_error_samples": start_error,
        "cfo_hz": float(diag["frequency_offset_hz"]),
        "acquisition_metric": float(diag["acquisition_metric"]),
        "acquired": bool(acquired),
        # ``crc_ok and not payload_matched`` is a silent false accept: the
        # receiver would hand corrupt bytes up as good.  Counted at every
        # point precisely because it must never happen.
        "crc_ok": bool(decoded is not None),
        "payload_matched": bool(delivered),
        **metrics,
    }
    return TrialResult(
        trial=trial, direction=point.label, mode_id=SEED_NAMESPACE,
        mode_name=MODE_NAME, payload_bytes=payload_bytes, outcome=outcome,
        tx_samples=len(capture), tx_sample_rate=hc2.SAMPLE_RATE,
        rx_samples=len(rx), rx_sample_rate=hc2.SAMPLE_RATE,
        keyed_seconds=hc2.FRAME_SECONDS,
        channel_measurements=dict(received.measurements),
        decoder_metrics=decoder_metrics)


def _trigger_fired(trial: TrialResult, threshold: float) -> bool:
    """Would the in-frame health check have flagged this frame as bad?

    Either the receiver's own equalizer guard rejected the frame, or the
    decision-directed EVM exceeded the trigger.  A frame with no EVM at all
    counts as flagged, because a receiver that cannot measure its own
    constellation has already noticed something.
    """

    if trial.decoder_metrics.get("equalizer_rejected"):
        return True
    evm = trial.decoder_metrics.get("evm_percent")
    if evm is None or not math.isfinite(evm):
        return True
    return evm > threshold


def summarize_point(point: ChannelPoint, trials, threshold: float) -> dict:
    total = len(trials)
    delivered = sum(trial.decoded for trial in trials)
    failed = [trial for trial in trials if not trial.decoded]
    good = [trial for trial in trials if trial.decoded]
    delivery = wilson(delivered, total)
    false_accepts = [trial for trial in trials
                     if trial.decoder_metrics.get("crc_ok")
                     and not trial.decoder_metrics.get("payload_matched")]
    detected = sum(_trigger_fired(trial, threshold) for trial in failed)
    false_alarms = sum(_trigger_fired(trial, threshold) for trial in good)
    start_errors = [int(trial.decoder_metrics.get("start_error_samples", 0))
                    for trial in trials
                    if "start_error_samples" in trial.decoder_metrics]
    evm = lambda group: [t.decoder_metrics.get("evm_percent") for t in group]
    row = {
        **point.describe(),
        "trials": total,
        "delivered": delivered,
        "errors": sum(t.outcome is TrialOutcome.ERROR for t in trials),
        "fer": 1.0 - delivered / total if total else 1.0,
        "delivery_wilson_95": delivery,
        "fer_wilson_95": [1.0 - delivery[1], 1.0 - delivery[0]],
        "nominal_payload_bps": hc2.SUSTAINED_USER_BIT_RATE,
        "realized_payload_bps": (hc2.SUSTAINED_USER_BIT_RATE * delivered
                                 / total if total else 0.0),
        # Integrity.  Anything but zero here is a genuine defect.
        "crc_false_accepts": len(false_accepts),
        "crc_false_accept_wilson_95": wilson(len(false_accepts), total),
        # Detectability of the frames the channel broke.
        "evm_trigger_percent": threshold,
        "failed_frames": len(failed),
        "failed_frames_flagged": detected,
        "failed_frames_silent": len(failed) - detected,
        "detection_rate": detected / len(failed) if failed else None,
        "detection_wilson_95": (wilson(detected, len(failed)) if failed
                                else None),
        "delivered_frames_flagged": false_alarms,
        "false_alarm_rate": false_alarms / delivered if delivered else None,
        # Acquisition health.
        "acquired": sum(bool(t.decoder_metrics.get("acquired"))
                        for t in trials),
        "acquisition_wilson_95": wilson(
            sum(bool(t.decoder_metrics.get("acquired")) for t in trials),
            total),
        "start_error_mis_acquisitions": sum(
            error == MIS_ACQUISITION_START_ERROR for error in start_errors),
        "start_error_beyond_cyclic_prefix": sum(
            abs(error) > START_TOLERANCE_SAMPLES for error in start_errors),
        "start_error_abs_max": (max(abs(error) for error in start_errors)
                                if start_errors else None),
        "cfo_abs_max_hz": max((abs(float(t.decoder_metrics.get("cfo_hz", 0.0)))
                               for t in trials), default=None),
        # Constellation health.
        "evm_percent_decoded": _quantiles(evm(good)),
        "evm_percent_failed": _quantiles(evm(failed)),
        "true_evm_percent_all": _quantiles(
            [t.decoder_metrics.get("true_evm_percent") for t in trials]),
        "raw_ber_all": _quantiles(
            [t.decoder_metrics.get("raw_ber") for t in trials]),
        "channel_gain_min_all": _quantiles(
            [t.decoder_metrics.get("channel_gain_min") for t in trials]),
    }
    for key in ("residual_head_percent", "residual_tail_percent",
                "residual_frame_percent", "carrier_gain_spread_db",
                "common_phase_swing_deg"):
        values = [t.decoder_metrics.get(key) for t in trials]
        if any(value is not None for value in values):
            row[key] = _quantiles(values)
    return row


def build_points(args) -> list[ChannelPoint]:
    """Enumerate the campaign's points in a stable, documented order.

    Preset points come first, in ``--presets`` order then ``--points`` order;
    parametric points follow, in ``--delay-ms`` order then ``--spread-hz``
    order then ``--points`` order.  ``trial_seed`` keys on this index, so the
    order is part of the reproducibility contract: reproduce the argument
    lists verbatim to reproduce a run.
    """

    points = [ChannelPoint.from_preset(preset, snr)
              for preset in args.presets for snr in args.points]
    points += [ChannelPoint(delay_ms / 1e3, spread, snr)
               for delay_ms in args.delay_ms
               for spread in args.spread_hz
               for snr in args.points]
    if not points:
        raise ValueError("no channel points selected")
    return points


def run(args) -> dict:
    started = datetime.now(timezone.utc)
    clock = time.monotonic()
    points = build_points(args)
    records, summaries = [], []
    for point_index, point in enumerate(points):
        trials = []
        for trial in range(1, args.trials + 1):
            seed = trial_seed(args.seed, SEED_NAMESPACE, point_index, trial)
            trials.append(frame_trial(
                point=point, seed=seed, trial=trial,
                payload_bytes=args.payload_bytes,
                lead_samples=args.lead_samples,
                tail_samples=args.tail_samples,
                max_frequency_offset_hz=args.max_frequency_offset_hz,
                acquisition_step_hz=args.acquisition_step_hz,
                channel_diagnostics=args.channel_diagnostics))
        records.extend(trials)
        row = summarize_point(point, trials, args.evm_trigger_percent)
        summaries.append(row)
        if not args.quiet:
            evm = row["evm_percent_decoded"] or row["evm_percent_failed"] or {}
            print(f"{point.label}: {row['delivered']:4d}/{row['trials']:<4d} "
                  f"FER {row['fer']:.3f} "
                  f"[{row['fer_wilson_95'][0]:.3f},{row['fer_wilson_95'][1]:.3f}] "
                  f"{row['realized_payload_bps']:8.1f} bit/s "
                  f"EVM~{evm.get('median', float('nan')):.2f}% "
                  f"falseacc {row['crc_false_accepts']} "
                  f"({time.monotonic() - clock:.0f}s)", flush=True)

    artifact = {
        "schema": "whalemodem.hc2-watterson-boundary.v1",
        "qualification_evidence": False,
        "experiment": "hc2_32qam Watterson boundary sweep (milestone 4)",
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
            "snr_reference": "transmitted signal-bearing span only",
            "padding": "silence before the channel, i.e. noise-only at the receiver",
        },
        "channel": {
            "type": "watterson+awgn chain",
            "order": "watterson then awgn, as channel_factory('watterson')",
            "noise_seed": "trial seed ^ 0x5A5A",
            "spread_convention": "ITU-R F.1487 2-sigma",
            "paths": "two independent equal-power paths",
        },
        "acquisition_search": {
            "max_frequency_offset_hz": args.max_frequency_offset_hz,
            "acquisition_step_hz": args.acquisition_step_hz,
            "start_tolerance_samples": START_TOLERANCE_SAMPLES,
            "frequency_tolerance_hz": FREQUENCY_TOLERANCE_HZ,
            "mis_acquisition_start_error": MIS_ACQUISITION_START_ERROR,
        },
        "rate_accounting": hc2.rate_accounting(),
        "points": [point.describe() for point in points],
        "summaries": summaries,
        "evm_separation": evm_separation(records),
        "integrity": {
            "trials": len(records),
            "crc_false_accepts": sum(
                1 for trial in records
                if trial.decoder_metrics.get("crc_ok")
                and not trial.decoder_metrics.get("payload_matched")),
        },
        "trials": TrialRun(
            channel={"type": "watterson+awgn",
                     "sample_rate": hc2.SAMPLE_RATE,
                     "snr_kind": "waveform",
                     "points": [point.describe() for point in points]},
            trials=records, seed=args.seed,
            metadata={"benchmark": "hc2_watterson_boundary"},
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
                        default=Path("logs/scratch/hc2_watterson_sweep.json"))
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260830)
    parser.add_argument("--points", type=float, nargs="+", default=[18.0],
                        help="waveform SNR points, in dB")
    parser.add_argument("--presets", nargs="*", default=[],
                        help="WATTERSON_PRESETS names to sweep")
    parser.add_argument("--delay-ms", type=float, nargs="*", default=[],
                        help="differential delays for parametric points")
    parser.add_argument("--spread-hz", type=float, nargs="*", default=[],
                        help="frequency spreads for parametric points")
    parser.add_argument("--payload-bytes", type=int,
                        default=hc2.MAX_PAYLOAD_BYTES)
    parser.add_argument("--lead-samples", type=int, default=12_000)
    parser.add_argument("--tail-samples", type=int, default=12_000)
    parser.add_argument("--max-frequency-offset-hz", type=float, default=20.0)
    parser.add_argument("--acquisition-step-hz", type=float, default=1.0)
    parser.add_argument("--evm-trigger-percent", type=float,
                        default=EVM_TRIGGER_PERCENT)
    parser.add_argument("--channel-diagnostics", action="store_true",
                        help="add the oracle channel-staleness measurement")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main():
    args = build_parser().parse_args()
    if not 0 <= args.payload_bytes <= hc2.MAX_PAYLOAD_BYTES:
        raise SystemExit(
            f"--payload-bytes must be 0..{hc2.MAX_PAYLOAD_BYTES}")
    if bool(args.delay_ms) != bool(args.spread_hz):
        raise SystemExit(
            "--delay-ms and --spread-hz must be given together")
    run(args)


if __name__ == "__main__":
    main()
