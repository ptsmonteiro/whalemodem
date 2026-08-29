"""Versioned, JSON-compatible records shared by mode qualification tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Mapping, Sequence

import numpy as np


SCHEMA_VERSION = 2


class TrialOutcome(StrEnum):
    DECODED = "decoded"
    ACQUISITION_FAILED = "acquisition_failed"
    PAYLOAD_FAILED = "payload_failed"
    ERROR = "error"


@dataclass(frozen=True)
class TrialResult:
    """One transmitted frame, independent of a particular waveform decoder."""

    trial: int
    direction: str
    mode_id: int
    mode_name: str
    payload_bytes: int
    outcome: TrialOutcome
    tx_samples: int
    tx_sample_rate: int
    rx_samples: int
    rx_sample_rate: int
    keyed_seconds: float
    channel_measurements: Mapping[str, object] = field(default_factory=dict)
    decoder_metrics: Mapping[str, object] = field(default_factory=dict)
    capture: str | None = None
    error: str | None = None

    @property
    def decoded(self) -> bool:
        return self.outcome is TrialOutcome.DECODED


@dataclass(frozen=True)
class TrialRun:
    """Self-describing output document for a collection of frame trials."""

    channel: Mapping[str, object]
    trials: Sequence[TrialResult]
    seed: int
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        document = asdict(self)
        document["trials"] = [
            {**asdict(trial), "outcome": trial.outcome.value,
             "decoded": trial.decoded}
            for trial in self.trials
        ]
        document["summary"] = {
            "passed": sum(trial.decoded for trial in self.trials),
            "total": len(self.trials),
        }
        return _jsonable(document)


def classify_decode(result: Mapping[str, object], expected_payload: bytes,
                    confidence_threshold: float) -> TrialOutcome:
    """Classify the common waveform decode result without mode-specific keys."""

    if result.get("payload") == expected_payload:
        return TrialOutcome.DECODED
    confidence = result.get("confidence")
    if confidence is None or float(confidence) < confidence_threshold:
        return TrialOutcome.ACQUISITION_FAILED
    return TrialOutcome.PAYLOAD_FAILED


def common_decoder_metrics(result: Mapping[str, object], audio) -> dict:
    """Bounded cross-waveform diagnostics suitable for a trial document."""

    keys = (
        "confidence", "failure", "cfo_hz", "clock_offset_ppm", "ber",
        "raw_errors", "total_bit_errors", "compared_bits", "missing_bits",
        "bit_error_positions", "bit_error_positions_truncated", "crc_ok",
        "present_carriers",
        "carrier_snr_db", "tone_snr_db", "snr_db", "symbol_evm_db",
        "start_index", "end_index", "head_seconds_received",
        "head_cores_observed", "head_blocks_observed",
    )
    metrics = {key: result[key] for key in keys if key in result}
    samples = np.asarray(audio, dtype=np.float64)
    metrics["capture_rms"] = (float(np.sqrt(np.mean(samples ** 2)))
                              if len(samples) else 0.0)
    metrics["capture_peak"] = (float(np.max(np.abs(samples)))
                               if len(samples) else 0.0)
    return metrics


def _jsonable(value):
    """Return strict-JSON data; unavailable non-finite diagnostics become null."""

    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value
