"""Contracts and common definitions for deterministic simulated audio channels.

A channel operates at the modem's audio boundary.  It receives one finite,
mono waveform and returns the samples presented to the peer's receive path.
Implementations may change the number of samples (delay and clock error do),
and may retain state between calls (continuous fading does).  One instance is
therefore one direction of one path and must not be shared between A->B and
B->A.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Mapping, Protocol, runtime_checkable

import numpy as np
from scipy.signal import butter, hilbert, resample_poly, sosfilt


class SnrKind(StrEnum):
    """The power ratio represented by an SNR value."""

    WAVEFORM = "waveform"
    IN_BAND = "in_band"
    EB_N0 = "eb_n0"


@dataclass(frozen=True)
class SnrSpec:
    """An unambiguous SNR request or measurement.

    ``WAVEFORM`` is the canonical simulated-channel convention: mean power of
    the samples in ``reference_start:reference_stop`` divided by mean power of
    real AWGN across the complete 0..sample_rate/2 Nyquist band.  The interval
    is half-open.  Omitting both bounds means the complete supplied waveform.

    ``IN_BAND`` compares signal and noise power integrated over ``band_hz``.
    ``EB_N0`` is reserved for coded-waveform analysis and requires
    ``bit_rate``.  Channel models should not silently convert among kinds.
    """

    db: float
    kind: SnrKind = SnrKind.WAVEFORM
    band_hz: tuple[float, float] | None = None
    bit_rate: float | None = None
    reference_start: int | None = None
    reference_stop: int | None = None

    def __post_init__(self):
        if not np.isfinite(self.db):
            raise ValueError("SNR must be finite")
        if (self.reference_start is not None and self.reference_start < 0):
            raise ValueError("reference_start must be non-negative")
        if (self.reference_stop is not None and self.reference_stop < 0):
            raise ValueError("reference_stop must be non-negative")
        if (self.reference_start is not None and self.reference_stop is not None
                and self.reference_stop <= self.reference_start):
            raise ValueError("SNR reference interval must not be empty")
        if self.kind is SnrKind.IN_BAND:
            if self.band_hz is None or not 0 <= self.band_hz[0] < self.band_hz[1]:
                raise ValueError("in-band SNR requires an increasing band_hz")
        elif self.band_hz is not None:
            raise ValueError("band_hz is only valid for in-band SNR")
        if self.kind is SnrKind.EB_N0:
            if self.bit_rate is None or self.bit_rate <= 0:
                raise ValueError("Eb/N0 requires a positive bit_rate")
        elif self.bit_rate is not None:
            raise ValueError("bit_rate is only valid for Eb/N0")

    def reference_slice(self, length: int) -> slice:
        start = 0 if self.reference_start is None else self.reference_start
        stop = length if self.reference_stop is None else self.reference_stop
        if start >= stop or stop > length:
            raise ValueError("SNR reference interval is outside the waveform")
        return slice(start, stop)


@dataclass(frozen=True)
class ChannelResult:
    """Samples returned by a channel plus JSON-compatible measured metadata."""

    audio: np.ndarray
    measurements: Mapping[str, object]


@runtime_checkable
class AudioChannel(Protocol):
    """One stateful, directional audio-channel realization.

    Random implementations own a seeded generator; reproducibility must not
    depend on module-global random state.  ``describe`` returns JSON-compatible
    configuration, including the seed.  ``reset`` returns the realization to
    the start of that seeded sequence.
    """

    sample_rate: int

    def process(self, audio: np.ndarray) -> ChannelResult: ...

    def reset(self) -> None: ...

    def describe(self) -> Mapping[str, object]: ...


@dataclass
class IdentityChannel:
    """The lossless reference implementation of :class:`AudioChannel`."""

    sample_rate: int

    def __post_init__(self):
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = np.asarray(audio)
        if samples.ndim != 1:
            raise ValueError("channel audio must be a mono one-dimensional array")
        return ChannelResult(samples.astype(np.float32, copy=True), {})

    def reset(self) -> None:
        pass

    def describe(self) -> Mapping[str, object]:
        return {"type": "identity", "sample_rate": self.sample_rate}


class ChannelChain:
    """Apply channel stages in the stated, physically significant order."""

    def __init__(self, stages):
        self.stages = tuple(stages)
        if not self.stages:
            raise ValueError("a channel chain requires at least one stage")
        rates = {stage.sample_rate for stage in self.stages}
        if len(rates) != 1:
            raise ValueError("all channel stages must use the same sample rate")
        self.sample_rate = self.stages[0].sample_rate

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = _mono(audio)
        measurements = {}
        for index, stage in enumerate(self.stages):
            result = stage.process(samples)
            samples = _mono(result.audio)
            measurements[f"stage_{index}"] = dict(result.measurements)
        return ChannelResult(samples.astype(np.float32, copy=False), measurements)

    def reset(self) -> None:
        for stage in self.stages:
            stage.reset()

    def describe(self) -> Mapping[str, object]:
        return {"type": "chain", "sample_rate": self.sample_rate,
                "stages": [dict(stage.describe()) for stage in self.stages]}


class AwgnChannel:
    """Real, full-Nyquist-band AWGN at waveform-referenced SNR."""

    def __init__(self, sample_rate: int, snr: SnrSpec, seed: int):
        _positive_rate(sample_rate)
        if snr.kind is not SnrKind.WAVEFORM:
            raise ValueError("AWGN currently requires waveform-referenced SNR")
        self.sample_rate, self.snr, self.seed = sample_rate, snr, int(seed)
        self.reset()

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = _mono(audio).astype(np.float64)
        power = waveform_power(samples, self.snr)
        noise_power = power / 10.0 ** (self.snr.db / 10.0)
        noise = self._rng.normal(0.0, np.sqrt(noise_power), len(samples))
        reference = self.snr.reference_slice(len(samples))
        realized_noise_power = float(np.mean(noise[reference] ** 2))
        realized_snr = (float("inf") if realized_noise_power == 0 else
                        10.0 * np.log10(power / realized_noise_power))
        return ChannelResult((samples + noise).astype(np.float32), {
            "waveform_snr_db": self.snr.db,
            "realized_waveform_snr_db": float(realized_snr),
            "signal_power": power,
            "noise_power": realized_noise_power,
            "reference_samples": [reference.start, reference.stop],
        })

    def reset(self) -> None:
        self._rng = np.random.default_rng(self.seed)

    def describe(self) -> Mapping[str, object]:
        return {"type": "awgn", "sample_rate": self.sample_rate,
                "seed": self.seed, "snr": _snr_description(self.snr)}


class FrequencyOffsetChannel:
    """Shift real audio by an initial offset with optional linear drift.

    Drift is Hz per second. Phase and elapsed time continue across calls so a
    sequence of frames experiences one oscillator realization.
    """

    def __init__(self, sample_rate: int, offset_hz: float,
                 drift_hz_per_second: float = 0.0):
        _positive_rate(sample_rate)
        if not np.isfinite(offset_hz) or not np.isfinite(drift_hz_per_second):
            raise ValueError("frequency offset and drift must be finite")
        self.sample_rate = sample_rate
        self.offset_hz = float(offset_hz)
        self.drift_hz_per_second = float(drift_hz_per_second)
        self.reset()

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = _mono(audio).astype(np.float64)
        local_time = np.arange(len(samples), dtype=np.float64) / self.sample_rate
        absolute_time = self._elapsed_seconds + local_time
        phase = 2.0 * np.pi * (
            self.offset_hz * absolute_time
            + 0.5 * self.drift_hz_per_second * absolute_time ** 2)
        shifted = np.real(hilbert(samples) * np.exp(1j * phase))
        start_hz = self.offset_hz + self.drift_hz_per_second * self._elapsed_seconds
        self._elapsed_seconds += len(samples) / self.sample_rate
        stop_hz = self.offset_hz + self.drift_hz_per_second * self._elapsed_seconds
        return ChannelResult(shifted.astype(np.float32), {
            "frequency_offset_start_hz": start_hz,
            "frequency_offset_stop_hz": stop_hz,
            "drift_hz_per_second": self.drift_hz_per_second,
        })

    def reset(self) -> None:
        self._elapsed_seconds = 0.0

    def describe(self) -> Mapping[str, object]:
        return {"type": "frequency_offset", "sample_rate": self.sample_rate,
                "offset_hz": self.offset_hz,
                "drift_hz_per_second": self.drift_hz_per_second}


@dataclass
class DelayChannel:
    """Prepend a fixed propagation/radio delay to every supplied waveform."""

    sample_rate: int
    delay_seconds: float

    def __post_init__(self):
        _positive_rate(self.sample_rate)
        if not np.isfinite(self.delay_seconds) or self.delay_seconds < 0:
            raise ValueError("delay_seconds must be finite and non-negative")
        self.delay_samples = round(self.delay_seconds * self.sample_rate)

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = _mono(audio)
        delayed = np.concatenate((np.zeros(self.delay_samples, np.float32), samples))
        return ChannelResult(delayed.astype(np.float32, copy=False), {
            "delay_samples": self.delay_samples,
            "delay_seconds": self.delay_samples / self.sample_rate,
        })

    def reset(self) -> None:
        pass

    def describe(self) -> Mapping[str, object]:
        return {"type": "delay", "sample_rate": self.sample_rate,
                "delay_samples": self.delay_samples,
                "delay_seconds": self.delay_samples / self.sample_rate}


class FilterChannel:
    """Stateful Butterworth low-, high-, or band-pass audio filtering."""

    def __init__(self, sample_rate: int, *, low_hz: float | None = None,
                 high_hz: float | None = None, order: int = 6):
        _positive_rate(sample_rate)
        nyquist = sample_rate / 2.0
        if low_hz is None and high_hz is None:
            raise ValueError("filter requires low_hz, high_hz, or both")
        if low_hz is not None and not 0 < low_hz < nyquist:
            raise ValueError("low_hz must lie between zero and Nyquist")
        if high_hz is not None and not 0 < high_hz < nyquist:
            raise ValueError("high_hz must lie between zero and Nyquist")
        if low_hz is not None and high_hz is not None and low_hz >= high_hz:
            raise ValueError("filter band must be increasing")
        if order < 1:
            raise ValueError("filter order must be positive")
        self.sample_rate, self.low_hz = sample_rate, low_hz
        self.high_hz, self.order = high_hz, int(order)
        if low_hz is not None and high_hz is not None:
            cutoff, kind = (low_hz, high_hz), "bandpass"
        elif low_hz is not None:
            cutoff, kind = low_hz, "highpass"
        else:
            cutoff, kind = high_hz, "lowpass"
        self._kind = kind
        self._sos = butter(self.order, cutoff, btype=kind, fs=sample_rate,
                           output="sos")
        self.reset()

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = _mono(audio).astype(np.float64)
        filtered, self._zi = sosfilt(self._sos, samples, zi=self._zi)
        return ChannelResult(filtered.astype(np.float32), {
            "filter": self._kind, "low_hz": self.low_hz,
            "high_hz": self.high_hz, "order": self.order,
        })

    def reset(self) -> None:
        self._zi = np.zeros((self._sos.shape[0], 2), dtype=np.float64)

    def describe(self) -> Mapping[str, object]:
        return {"type": "filter", "sample_rate": self.sample_rate,
                "filter": self._kind, "low_hz": self.low_hz,
                "high_hz": self.high_hz, "order": self.order}


@dataclass
class ClippingChannel:
    """Symmetric hard clipping at an absolute audio amplitude."""

    sample_rate: int
    limit: float

    def __post_init__(self):
        _positive_rate(self.sample_rate)
        if not np.isfinite(self.limit) or self.limit <= 0:
            raise ValueError("clipping limit must be finite and positive")

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = _mono(audio)
        clipped_count = int(np.count_nonzero(np.abs(samples) > self.limit))
        clipped = np.clip(samples, -self.limit, self.limit)
        return ChannelResult(clipped.astype(np.float32, copy=False), {
            "clip_limit": self.limit,
            "clipped_samples": clipped_count,
            "clipped_fraction": clipped_count / len(samples) if len(samples) else 0.0,
        })

    def reset(self) -> None:
        pass

    def describe(self) -> Mapping[str, object]:
        return {"type": "clipping", "sample_rate": self.sample_rate,
                "limit": self.limit}


class SampleClockChannel:
    """Resample audio to simulate receiver sample-clock error.

    Positive ppm means the receiver takes more samples during the same physical
    interval; output length is therefore approximately ``1 + ppm/1e6`` times
    input length. The rational approximation is reported by ``describe``.
    """

    def __init__(self, sample_rate: int, error_ppm: float,
                 max_denominator: int = 1_000_000):
        _positive_rate(sample_rate)
        if not np.isfinite(error_ppm) or error_ppm <= -1_000_000:
            raise ValueError("sample-clock error must be finite and above -1e6 ppm")
        if max_denominator < 1:
            raise ValueError("max_denominator must be positive")
        self.sample_rate, self.error_ppm = sample_rate, float(error_ppm)
        ratio = Fraction(1.0 + self.error_ppm / 1_000_000.0).limit_denominator(
            max_denominator)
        self.up, self.down = ratio.numerator, ratio.denominator

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = _mono(audio)
        resampled = resample_poly(samples, self.up, self.down)
        actual_ppm = (self.up / self.down - 1.0) * 1_000_000.0
        return ChannelResult(resampled.astype(np.float32), {
            "sample_clock_error_ppm": self.error_ppm,
            "actual_sample_clock_error_ppm": actual_ppm,
            "resample_ratio": [self.up, self.down],
        })

    def reset(self) -> None:
        pass

    def describe(self) -> Mapping[str, object]:
        return {"type": "sample_clock", "sample_rate": self.sample_rate,
                "error_ppm": self.error_ppm,
                "resample_ratio": [self.up, self.down]}


def waveform_power(audio: np.ndarray, spec: SnrSpec) -> float:
    """Mean-square power for a ``WAVEFORM`` SNR reference interval."""

    if spec.kind is not SnrKind.WAVEFORM:
        raise ValueError("waveform_power requires waveform SNR")
    samples = np.asarray(audio, dtype=np.float64)
    if samples.ndim != 1:
        raise ValueError("channel audio must be a mono one-dimensional array")
    view = samples[spec.reference_slice(len(samples))]
    return float(np.mean(view * view))


def _mono(audio: np.ndarray) -> np.ndarray:
    samples = np.asarray(audio)
    if samples.ndim != 1:
        raise ValueError("channel audio must be a mono one-dimensional array")
    return samples


def _positive_rate(sample_rate: int) -> None:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")


def _snr_description(spec: SnrSpec) -> dict:
    return {"db": spec.db, "kind": spec.kind.value,
            "band_hz": spec.band_hz, "bit_rate": spec.bit_rate,
            "reference_start": spec.reference_start,
            "reference_stop": spec.reference_stop}
