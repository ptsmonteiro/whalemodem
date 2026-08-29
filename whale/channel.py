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
from typing import Mapping, Protocol, Sequence, runtime_checkable

import numpy as np
from scipy.signal import butter, hilbert, iirnotch, lfilter, resample_poly, sosfilt


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


@dataclass
class GainChannel:
    """Apply an explicit voltage gain, expressed linearly or in decibels."""

    sample_rate: int
    gain: float | None = None
    gain_db: float | None = None

    def __post_init__(self):
        _positive_rate(self.sample_rate)
        if (self.gain is None) == (self.gain_db is None):
            raise ValueError("specify exactly one of gain or gain_db")
        if self.gain is not None:
            if not np.isfinite(self.gain) or self.gain <= 0:
                raise ValueError("gain must be finite and positive")
            self._gain = float(self.gain)
            self.gain_db = 20.0 * np.log10(self._gain)
        else:
            if not np.isfinite(self.gain_db):
                raise ValueError("gain_db must be finite")
            self._gain = float(10.0 ** (self.gain_db / 20.0))
            self.gain = self._gain

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = _mono(audio)
        return ChannelResult((samples * self._gain).astype(np.float32), {
            "voltage_gain": self._gain, "gain_db": self.gain_db,
            "input_power": _mean_power(samples),
            "output_power": _mean_power(samples * self._gain),
        })

    def reset(self) -> None:
        pass

    def describe(self) -> Mapping[str, object]:
        return {"type": "gain", "sample_rate": self.sample_rate,
                "gain": self._gain, "gain_db": self.gain_db}


class ImpulseNoiseChannel:
    """Add a seeded Poisson stream of finite-duration noise impulses.

    ``amplitude`` is peak amplitude for ``fixed`` and ``uniform`` events and
    standard deviation for ``normal`` events. Fixed events receive a seeded
    random sign. Shapes are ``rectangular``, ``hann``, or ``exponential``.
    """

    def __init__(self, sample_rate: int, event_rate_hz: float,
                 duration_seconds: float, amplitude: float, seed: int, *,
                 amplitude_distribution: str = "fixed",
                 burst_shape: str = "rectangular"):
        _positive_rate(sample_rate)
        values = (event_rate_hz, duration_seconds, amplitude)
        if not all(np.isfinite(value) for value in values):
            raise ValueError("impulse parameters must be finite")
        if event_rate_hz < 0 or duration_seconds <= 0 or amplitude < 0:
            raise ValueError("event rate/amplitude must be non-negative and duration positive")
        if amplitude_distribution not in {"fixed", "uniform", "normal"}:
            raise ValueError("unknown amplitude distribution")
        if burst_shape not in {"rectangular", "hann", "exponential"}:
            raise ValueError("unknown burst shape")
        self.sample_rate, self.event_rate_hz = sample_rate, float(event_rate_hz)
        self.duration_seconds, self.amplitude = float(duration_seconds), float(amplitude)
        self.seed, self.amplitude_distribution = int(seed), amplitude_distribution
        self.burst_shape = burst_shape
        self._duration_samples = max(1, round(duration_seconds * sample_rate))
        self.reset()

    def reset(self) -> None:
        self._rng = np.random.default_rng(self.seed)
        self._position = 0
        self._events: list[tuple[int, float]] = []
        self._next_event = self._draw_interval()

    def _draw_interval(self) -> int | None:
        if self.event_rate_hz == 0:
            return None
        return max(1, int(np.ceil(self._rng.exponential(
            self.sample_rate / self.event_rate_hz))))

    def _draw_amplitude(self) -> float:
        if self.amplitude_distribution == "fixed":
            return self.amplitude * (-1.0 if self._rng.random() < .5 else 1.0)
        if self.amplitude_distribution == "uniform":
            return float(self._rng.uniform(-self.amplitude, self.amplitude))
        return float(self._rng.normal(0.0, self.amplitude))

    def _shape(self) -> np.ndarray:
        if self.burst_shape == "rectangular":
            return np.ones(self._duration_samples)
        if self.burst_shape == "hann":
            return np.hanning(self._duration_samples + 2)[1:-1]
        return np.exp(-5.0 * np.arange(self._duration_samples)
                      / self._duration_samples)

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = _mono(audio).astype(np.float64)
        start, stop = self._position, self._position + len(samples)
        new_events = 0
        while self._next_event is not None and self._position + self._next_event < stop:
            event_at = self._position + self._next_event
            self._events.append((event_at, self._draw_amplitude()))
            new_events += 1
            interval = self._draw_interval()
            self._next_event = None if interval is None else event_at + interval - self._position
        if self._next_event is not None:
            self._next_event -= len(samples)
        injected = np.zeros(len(samples), dtype=np.float64)
        shape = self._shape()
        for event_at, amplitude in self._events:
            left, right = max(start, event_at), min(stop, event_at + self._duration_samples)
            if left < right:
                injected[left - start:right - start] += amplitude * shape[
                    left - event_at:right - event_at]
        self._events = [(at, amp) for at, amp in self._events
                        if at + self._duration_samples > stop]
        self._position = stop
        return ChannelResult((samples + injected).astype(np.float32), {
            "events_started": new_events,
            "active_samples": int(np.count_nonzero(injected)),
            "injected_power": _mean_power(injected),
            "peak_injected_amplitude": (float(np.max(np.abs(injected)))
                                         if len(injected) else 0.0),
        })

    def describe(self) -> Mapping[str, object]:
        return {"type": "impulse_noise", "sample_rate": self.sample_rate,
                "event_rate_hz": self.event_rate_hz,
                "duration_seconds": self.duration_seconds,
                "duration_samples": self._duration_samples,
                "amplitude": self.amplitude,
                "amplitude_distribution": self.amplitude_distribution,
                "burst_shape": self.burst_shape, "seed": self.seed}


@dataclass(frozen=True)
class NarrowbandInterference:
    """One tone or narrow Gaussian-noise interferer."""

    frequency_hz: float
    power_db: float
    kind: str = "tone"
    width_hz: float = 0.0
    power_reference: str = "absolute"
    drift_hz_per_second: float = 0.0
    duty_cycle: float = 1.0

    def __post_init__(self):
        if not all(np.isfinite(value) for value in (
                self.frequency_hz, self.power_db, self.width_hz,
                self.drift_hz_per_second, self.duty_cycle)):
            raise ValueError("interference parameters must be finite")
        if self.kind not in {"tone", "noise"}:
            raise ValueError("interference kind must be tone or noise")
        if self.power_reference not in {"absolute", "relative"}:
            raise ValueError("power_reference must be absolute or relative")
        if not 0 <= self.duty_cycle <= 1:
            raise ValueError("duty_cycle must lie between zero and one")
        if self.kind == "noise" and self.width_hz <= 0:
            raise ValueError("narrow noise requires a positive width_hz")
        if self.kind == "tone" and self.width_hz != 0:
            raise ValueError("tone width_hz must be zero")


class NarrowbandInterferenceChannel:
    """Add independently specified tones or narrow noise bands."""

    def __init__(self, sample_rate: int,
                 sources: Sequence[NarrowbandInterference], seed: int,
                 *, duty_period_seconds: float = 1.0):
        _positive_rate(sample_rate)
        self.sample_rate, self.sources, self.seed = sample_rate, tuple(sources), int(seed)
        if not self.sources:
            raise ValueError("at least one interference source is required")
        if not np.isfinite(duty_period_seconds) or duty_period_seconds <= 0:
            raise ValueError("duty_period_seconds must be finite and positive")
        self.duty_period_seconds = float(duty_period_seconds)
        nyquist = sample_rate / 2
        for source in self.sources:
            if not 0 < source.frequency_hz < nyquist:
                raise ValueError("interference frequency must lie between zero and Nyquist")
            if source.kind == "noise" and source.width_hz >= 2 * min(
                    source.frequency_hz, nyquist - source.frequency_hz):
                raise ValueError("noise band must remain within Nyquist")
        self.reset()

    def reset(self) -> None:
        self._rng = np.random.default_rng(self.seed)
        self._elapsed_samples = 0
        self._noise_zi = [np.zeros(1) for _ in self.sources]

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = _mono(audio).astype(np.float64)
        count = len(samples)
        t = (self._elapsed_samples + np.arange(count)) / self.sample_rate
        signal_power = _mean_power(samples)
        total = np.zeros(count)
        realized = []
        for index, source in enumerate(self.sources):
            target = 10.0 ** (source.power_db / 10.0)
            if source.power_reference == "relative":
                target *= signal_power
            gate = ((t % self.duty_period_seconds) <
                    source.duty_cycle * self.duty_period_seconds)
            phase = 2 * np.pi * (source.frequency_hz * t
                    + .5 * source.drift_hz_per_second * t * t)
            if source.kind == "tone":
                component = np.sqrt(2 * target) * np.cos(phase)
            else:
                cutoff = source.width_hz / 2
                b, a = butter(1, cutoff, fs=self.sample_rate)
                if count:
                    base, self._noise_zi[index] = lfilter(
                        b, a, self._rng.normal(size=count), zi=self._noise_zi[index])
                else:
                    base = np.zeros(0)
                base_power = _mean_power(base)
                component = (base * np.sqrt(target / base_power) * np.sqrt(2)
                             * np.cos(phase)) if base_power else base
            component *= gate
            total += component
            realized.append({"kind": source.kind,
                             "injected_power": _mean_power(component),
                             "active_fraction": float(np.mean(gate)) if count else 0.0,
                             "frequency_start_hz": (source.frequency_hz
                                 + source.drift_hz_per_second * (t[0] if count else
                                   self._elapsed_samples / self.sample_rate)),
                             "frequency_stop_hz": (source.frequency_hz
                                 + source.drift_hz_per_second * ((self._elapsed_samples + count)
                                                                / self.sample_rate))})
        self._elapsed_samples += count
        return ChannelResult((samples + total).astype(np.float32), {
            "signal_power": signal_power, "injected_power": _mean_power(total),
            "sources": realized})

    def describe(self) -> Mapping[str, object]:
        return {"type": "narrowband_interference", "sample_rate": self.sample_rate,
                "seed": self.seed, "duty_period_seconds": self.duty_period_seconds,
                "sources": [{"kind": s.kind, "frequency_hz": s.frequency_hz,
                    "width_hz": s.width_hz, "power_db": s.power_db,
                    "power_reference": s.power_reference,
                    "drift_hz_per_second": s.drift_hz_per_second,
                    "duty_cycle": s.duty_cycle} for s in self.sources]}


class NotchChannel:
    """Apply a finite-depth IIR notch with an optionally drifting center."""

    def __init__(self, sample_rate: int, center_hz: float, width_hz: float,
                 depth_db: float, drift_hz_per_second: float = 0.0, *,
                 update_samples: int = 128):
        _positive_rate(sample_rate)
        if not all(np.isfinite(v) for v in
                   (center_hz, width_hz, depth_db, drift_hz_per_second)):
            raise ValueError("notch parameters must be finite")
        if not 0 < center_hz < sample_rate / 2 or width_hz <= 0 or depth_db < 0:
            raise ValueError("notch center/width/depth are outside their valid range")
        if update_samples < 1:
            raise ValueError("update_samples must be positive")
        self.sample_rate, self.center_hz = sample_rate, float(center_hz)
        self.width_hz, self.depth_db = float(width_hz), float(depth_db)
        self.drift_hz_per_second = float(drift_hz_per_second)
        self.update_samples = int(update_samples)
        self.reset()

    def reset(self) -> None:
        self._elapsed_samples = 0
        self._zi = np.zeros(2)

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = _mono(audio).astype(np.float64)
        wet = np.empty_like(samples)
        for start in range(0, len(samples), self.update_samples):
            stop = min(len(samples), start + self.update_samples)
            absolute_mid = self._elapsed_samples + (start + stop) / 2
            center = self.center_hz + self.drift_hz_per_second * absolute_mid / self.sample_rate
            if not 0 < center < self.sample_rate / 2:
                raise ValueError("drifting notch center moved outside Nyquist")
            b, a = iirnotch(center, center / self.width_hz, fs=self.sample_rate)
            wet[start:stop], self._zi = lfilter(b, a, samples[start:stop], zi=self._zi)
        mix = 1.0 - 10.0 ** (-self.depth_db / 20.0)
        output = samples + mix * (wet - samples)
        start_hz = self.center_hz + self.drift_hz_per_second * self._elapsed_samples / self.sample_rate
        self._elapsed_samples += len(samples)
        stop_hz = self.center_hz + self.drift_hz_per_second * self._elapsed_samples / self.sample_rate
        return ChannelResult(output.astype(np.float32), {
            "center_start_hz": start_hz, "center_stop_hz": stop_hz,
            "width_hz": self.width_hz, "depth_db": self.depth_db,
            "input_power": _mean_power(samples), "output_power": _mean_power(output),
        })

    def describe(self) -> Mapping[str, object]:
        return {"type": "notch", "sample_rate": self.sample_rate,
                "center_hz": self.center_hz, "width_hz": self.width_hz,
                "depth_db": self.depth_db,
                "drift_hz_per_second": self.drift_hz_per_second,
                "update_samples": self.update_samples}


@dataclass(frozen=True)
class WattersonPath:
    """One Gaussian-scatter propagation mode in a Watterson channel.

    ``frequency_spread_hz`` is the ITU-R F.1487 ``2 sigma`` width of the
    Gaussian Doppler power spectrum, not its standard deviation.
    """

    delay_seconds: float
    frequency_spread_hz: float
    power: float = 1.0
    doppler_shift_hz: float = 0.0

    def __post_init__(self):
        values = (self.delay_seconds, self.frequency_spread_hz, self.power,
                  self.doppler_shift_hz)
        if not all(np.isfinite(value) for value in values):
            raise ValueError("Watterson path parameters must be finite")
        if self.delay_seconds < 0:
            raise ValueError("Watterson path delay must be non-negative")
        if self.frequency_spread_hz <= 0:
            raise ValueError("Watterson frequency spread must be positive")
        if self.power <= 0:
            raise ValueError("Watterson path power must be positive")


@dataclass(frozen=True)
class WattersonPreset:
    name: str
    differential_delay_seconds: float
    frequency_spread_hz: float

    def paths(self) -> tuple[WattersonPath, WattersonPath]:
        return (WattersonPath(0.0, self.frequency_spread_hz),
                WattersonPath(self.differential_delay_seconds,
                              self.frequency_spread_hz))


# Representative two-path, equal-power cases from ITU-R F.1487 Annex 3.
WATTERSON_PRESETS = {
    "low_latitude_quiet": WattersonPreset("low_latitude_quiet", 0.0005, 0.5),
    "low_latitude_moderate": WattersonPreset("low_latitude_moderate", 0.002, 1.5),
    "low_latitude_disturbed": WattersonPreset("low_latitude_disturbed", 0.006, 10.0),
    "mid_latitude_quiet": WattersonPreset("mid_latitude_quiet", 0.0005, 0.1),
    "mid_latitude_moderate": WattersonPreset("mid_latitude_moderate", 0.001, 0.5),
    "mid_latitude_disturbed": WattersonPreset("mid_latitude_disturbed", 0.002, 1.0),
    "mid_latitude_disturbed_nvis": WattersonPreset(
        "mid_latitude_disturbed_nvis", 0.007, 1.0),
    "high_latitude_quiet": WattersonPreset("high_latitude_quiet", 0.001, 0.5),
    "high_latitude_moderate": WattersonPreset("high_latitude_moderate", 0.003, 10.0),
    "high_latitude_disturbed": WattersonPreset("high_latitude_disturbed", 0.007, 30.0),
}


class WattersonChannel:
    """Stationary Gaussian-scatter HF channel at the real-audio boundary.

    Each path applies a zero-mean complex Gaussian fading gain with a Gaussian
    Doppler power spectrum, a frequency shift, and a delay. Independent paths
    are summed and normalized to preserve mean signal power. The fading is a
    deterministic sum-of-sinusoids realization sampled on a low-rate control
    grid and interpolated to audio rate; time and phase continue across calls.
    """

    def __init__(self, sample_rate: int, paths: Sequence[WattersonPath], seed: int,
                 *, oscillators: int = 256, fading_sample_rate: float | None = None,
                 preset_name: str | None = None):
        _positive_rate(sample_rate)
        self.paths = tuple(paths)
        if not self.paths:
            raise ValueError("Watterson channel requires at least one path")
        if oscillators < 32:
            raise ValueError("Watterson channel requires at least 32 oscillators")
        self.sample_rate, self.seed = sample_rate, int(seed)
        self.oscillators, self.preset_name = int(oscillators), preset_name
        fastest = max(abs(path.doppler_shift_hz) + 4 * path.frequency_spread_hz / 2
                      for path in self.paths)
        requested_rate = max(200.0, 4.0 * fastest)
        self.fading_sample_rate = (requested_rate if fading_sample_rate is None
                                   else float(fading_sample_rate))
        if not np.isfinite(self.fading_sample_rate) or self.fading_sample_rate <= 0:
            raise ValueError("fading_sample_rate must be finite and positive")
        if self.fading_sample_rate <= 2 * fastest:
            raise ValueError("fading_sample_rate is too low for the Doppler spectra")
        self._power_sum = sum(path.power for path in self.paths)
        self._max_delay = max(round(path.delay_seconds * sample_rate)
                              for path in self.paths)
        self.reset()

    @classmethod
    def from_preset(cls, sample_rate: int, preset: str, seed: int, **kwargs):
        try:
            definition = WATTERSON_PRESETS[preset]
        except KeyError:
            raise ValueError(
                f"unknown Watterson preset {preset!r}; have {sorted(WATTERSON_PRESETS)}"
            ) from None
        return cls(sample_rate, definition.paths(), seed,
                   preset_name=definition.name, **kwargs)

    def reset(self) -> None:
        rng = np.random.default_rng(self.seed)
        self._frequencies = []
        self._phases = []
        for path in self.paths:
            sigma = path.frequency_spread_hz / 2.0
            self._frequencies.append(rng.normal(
                path.doppler_shift_hz, sigma, self.oscillators))
            self._phases.append(rng.uniform(0.0, 2.0 * np.pi, self.oscillators))
        self._elapsed_samples = 0

    def _path_gain(self, path_index: int, sample_positions: np.ndarray) -> np.ndarray:
        time = sample_positions / self.sample_rate
        angles = (2.0 * np.pi * self._frequencies[path_index][:, None] * time
                  + self._phases[path_index][:, None])
        return np.sum(np.exp(1j * angles), axis=0) / np.sqrt(self.oscillators)

    def _gain_at_audio_samples(self, path_index: int, count: int) -> np.ndarray:
        if count == 0:
            return np.zeros(0, dtype=np.complex128)
        first, stop = self._elapsed_samples, self._elapsed_samples + count
        step = self.sample_rate / self.fading_sample_rate
        control = np.arange(first, stop + step, step)
        control = np.append(control, stop) if control[-1] < stop else control
        values = self._path_gain(path_index, control)
        positions = np.arange(first, stop)
        return (np.interp(positions, control, values.real)
                + 1j * np.interp(positions, control, values.imag))

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = _mono(audio).astype(np.float64)
        if not len(samples):
            return ChannelResult(np.zeros(self._max_delay, np.float32), {
                "paths": len(self.paths), "elapsed_seconds": self._elapsed_samples
                / self.sample_rate})
        analytic = hilbert(samples)
        output = np.zeros(len(samples) + self._max_delay, dtype=np.complex128)
        path_powers = []
        for index, path in enumerate(self.paths):
            delay = round(path.delay_seconds * self.sample_rate)
            gain = self._gain_at_audio_samples(index, len(samples))
            component = np.sqrt(path.power / self._power_sum) * gain * analytic
            output[delay:delay + len(samples)] += component
            path_powers.append(float(np.mean(np.abs(gain) ** 2) * path.power
                                     / self._power_sum))
        self._elapsed_samples += len(samples)
        return ChannelResult(np.real(output).astype(np.float32), {
            "paths": len(self.paths), "path_realized_mean_powers": path_powers,
            "elapsed_seconds": self._elapsed_samples / self.sample_rate,
            "max_delay_samples": self._max_delay,
        })

    def describe(self) -> Mapping[str, object]:
        return {"type": "watterson", "sample_rate": self.sample_rate,
                "seed": self.seed, "preset": self.preset_name,
                "oscillators": self.oscillators,
                "fading_sample_rate": self.fading_sample_rate,
                "frequency_spread_convention": "2_sigma",
                "paths": [{"delay_seconds": path.delay_seconds,
                           "frequency_spread_hz": path.frequency_spread_hz,
                           "power": path.power,
                           "doppler_shift_hz": path.doppler_shift_hz}
                          for path in self.paths]}


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


def _mean_power(samples: np.ndarray) -> float:
    values = np.asarray(samples, dtype=np.float64)
    return float(np.mean(values * values)) if len(values) else 0.0


def _snr_description(spec: SnrSpec) -> dict:
    return {"db": spec.db, "kind": spec.kind.value,
            "band_hz": spec.band_hz, "bit_rate": spec.bit_rate,
            "reference_start": spec.reference_start,
            "reference_stop": spec.reference_stop}
