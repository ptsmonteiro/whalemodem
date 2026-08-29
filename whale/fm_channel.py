"""Complex-baseband narrow-FM channel and measured VHF bench presets.

The public boundary remains real 48 kHz modem audio. Internally this module
FM-modulates it to complex IQ, applies RF noise/filtering/multipath, limits and
discriminates it, then applies the measured receive-audio response and clock.
That ordering produces the nonlinear FM threshold which audio-domain AWGN
cannot represent.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Sequence

import numpy as np
from scipy.signal import (butter, firwin2, minimum_phase,
                          fftconvolve, resample_poly, sosfilt)

from .channel import ChannelResult


@dataclass(frozen=True)
class FmRfPath:
    """One static complex RF path, relative to the first arrival."""

    delay_seconds: float = 0.0
    amplitude: float = 1.0
    phase_radians: float = 0.0

    def __post_init__(self):
        if not all(np.isfinite(value) for value in (
                self.delay_seconds, self.amplitude, self.phase_radians)):
            raise ValueError("FM RF path parameters must be finite")
        if self.delay_seconds < 0 or self.amplitude <= 0:
            raise ValueError("FM RF path delay must be non-negative and amplitude positive")


@dataclass(frozen=True)
class FmRadioPreset:
    """Measured end-to-end audio/timing approximation for one bench leg."""

    name: str
    audio_band_6db_hz: tuple[float, float]
    audio_band_10db_hz: tuple[float, float]
    sample_clock_error_ppm: float
    leading_mute_seconds: float
    measured_delay_spread_ms: float
    measurement_source: str

    def __post_init__(self):
        low10, high10 = self.audio_band_10db_hz
        low6, high6 = self.audio_band_6db_hz
        if not 0 < low10 < low6 < high6 < high10:
            raise ValueError("FM preset audio bands must nest in frequency order")
        if self.leading_mute_seconds < 0 or self.measured_delay_spread_ms < 0:
            raise ValueError("FM preset timing measurements must be non-negative")


_BANDWIDTH_SOURCE = "experiments/ofdm/results/measurements/bandwidth.json"
_CLOCK_SOURCE = "scripts/measure_clock_offset.py (measurements in module docstring)"

FM_RADIO_PRESETS = {
    "ic705_to_kg_uv9d": FmRadioPreset(
        name="ic705_to_kg_uv9d",
        audio_band_6db_hz=(430.9, 1905.5),
        audio_band_10db_hz=(384.8, 2453.2),
        sample_clock_error_ppm=-3.7,
        leading_mute_seconds=0.110,
        measured_delay_spread_ms=0.505,
        measurement_source=f"{_BANDWIDTH_SOURCE}; {_CLOCK_SOURCE}; README.md squelch measurement",
    ),
    "kg_uv9d_to_ic705": FmRadioPreset(
        name="kg_uv9d_to_ic705",
        audio_band_6db_hz=(425.1, 1746.4),
        audio_band_10db_hz=(363.0, 2372.3),
        sample_clock_error_ppm=+3.1,
        leading_mute_seconds=0.0,
        measured_delay_spread_ms=0.815,
        measurement_source=f"{_BANDWIDTH_SOURCE}; {_CLOCK_SOURCE}",
    ),
    "vhf_bench_conservative": FmRadioPreset(
        name="vhf_bench_conservative",
        audio_band_6db_hz=(430.9, 1746.4),
        audio_band_10db_hz=(384.8, 2372.3),
        sample_clock_error_ppm=-3.7,
        leading_mute_seconds=0.110,
        measured_delay_spread_ms=0.815,
        measurement_source="worst directional values from both VHF bench presets",
    ),
}


class ComplexFmChannel:
    """Narrow-FM RF path exposed as a real-audio ``AudioChannel``.

    ``carrier_to_noise_db`` is complex-IQ carrier power divided by complex
    white-noise power across the complete simulated Nyquist band. It is not
    audio SNR. ``deviation_hz`` is reached at ``full_scale_audio``.
    """

    def __init__(self, sample_rate: int, carrier_to_noise_db: float, seed: int,
                 *, deviation_hz: float = 2_500.0, full_scale_audio: float = 0.6,
                 rf_bandwidth_hz: float = 7_500.0, rf_frequency_error_hz: float = 0.0,
                 rf_paths: Sequence[FmRfPath] = (FmRfPath(),),
                 preset: FmRadioPreset | None = None,
                 audio_band_6db_hz: tuple[float, float] | None = None,
                 audio_band_10db_hz: tuple[float, float] | None = None,
                 sample_clock_error_ppm: float = 0.0,
                 leading_mute_seconds: float = 0.0,
                 audio_clip_limit: float | None = None):
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        numeric = (carrier_to_noise_db, deviation_hz, full_scale_audio,
                   rf_bandwidth_hz, rf_frequency_error_hz,
                   sample_clock_error_ppm, leading_mute_seconds)
        if not all(np.isfinite(value) for value in numeric):
            raise ValueError("FM channel parameters must be finite")
        if deviation_hz <= 0 or full_scale_audio <= 0:
            raise ValueError("FM deviation and full-scale audio must be positive")
        if not 0 < rf_bandwidth_hz < sample_rate / 2:
            raise ValueError("RF bandwidth must lie between zero and Nyquist")
        if leading_mute_seconds < 0:
            raise ValueError("leading mute must be non-negative")
        if not rf_paths:
            raise ValueError("FM channel requires at least one RF path")
        if audio_clip_limit is not None and audio_clip_limit <= 0:
            raise ValueError("audio clip limit must be positive")
        if preset is not None:
            if audio_band_6db_hz is not None or audio_band_10db_hz is not None:
                raise ValueError("preset and explicit measured audio bands are exclusive")
            audio_band_6db_hz = preset.audio_band_6db_hz
            audio_band_10db_hz = preset.audio_band_10db_hz
            sample_clock_error_ppm = preset.sample_clock_error_ppm
            leading_mute_seconds = preset.leading_mute_seconds
        if (audio_band_6db_hz is None) != (audio_band_10db_hz is None):
            raise ValueError("audio response requires both -6 and -10 dB bands")

        self.sample_rate = sample_rate
        self.carrier_to_noise_db = float(carrier_to_noise_db)
        self.seed, self.deviation_hz = int(seed), float(deviation_hz)
        self.full_scale_audio = float(full_scale_audio)
        self.rf_bandwidth_hz = float(rf_bandwidth_hz)
        self.rf_frequency_error_hz = float(rf_frequency_error_hz)
        self.rf_paths = tuple(rf_paths)
        self.preset = preset
        self.audio_band_6db_hz = audio_band_6db_hz
        self.audio_band_10db_hz = audio_band_10db_hz
        self.sample_clock_error_ppm = float(sample_clock_error_ppm)
        self.leading_mute_seconds = float(leading_mute_seconds)
        self.audio_clip_limit = audio_clip_limit
        self._rf_sos = butter(6, self.rf_bandwidth_hz, btype="lowpass",
                              fs=sample_rate, output="sos")
        self._audio_fir = self._design_audio_response()
        ratio = Fraction(1 + self.sample_clock_error_ppm / 1_000_000).limit_denominator(
            1_000_000)
        self._clock_up, self._clock_down = ratio.numerator, ratio.denominator
        self._max_path_delay = max(round(path.delay_seconds * sample_rate)
                                   for path in self.rf_paths)
        self.reset()

    @classmethod
    def from_preset(cls, sample_rate: int, preset: str, carrier_to_noise_db: float,
                    seed: int, **kwargs):
        try:
            definition = FM_RADIO_PRESETS[preset]
        except KeyError:
            raise ValueError(
                f"unknown FM radio preset {preset!r}; have {sorted(FM_RADIO_PRESETS)}"
            ) from None
        return cls(sample_rate, carrier_to_noise_db, seed,
                   preset=definition, **kwargs)

    def _design_audio_response(self):
        if self.audio_band_6db_hz is None:
            return np.array([1.0])
        low10, high10 = self.audio_band_10db_hz
        low6, high6 = self.audio_band_6db_hz
        centre_low = min(max(low6 * 1.5, low6 + 100), high6 - 100)
        centre_high = max(min(high6 * 0.75, high6 - 100), centre_low + 50)
        frequencies = [0, low10, low6, centre_low, centre_high,
                       high6, high10, self.sample_rate / 2]
        gains = [0, 10 ** (-10 / 20), 10 ** (-6 / 20), 1, 1,
                 10 ** (-6 / 20), 10 ** (-10 / 20), 0]
        # The measured end-to-end response does not provide phase. Start from
        # its magnitude anchors, then choose the minimum-phase realization:
        # a long linear-phase FIR would invent ~21 ms of pure group delay and
        # ringing that the bench measurement never observed.
        linear_phase = firwin2(2_049, frequencies, gains, fs=self.sample_rate)
        return minimum_phase(linear_phase, method="homomorphic", half=False)

    def reset(self) -> None:
        self._rng = np.random.default_rng(self.seed)
        self._phase = 0.0
        self._rf_zi = np.zeros((self._rf_sos.shape[0], 2), dtype=np.complex128)
        self._audio_history = np.zeros(len(self._audio_fir) - 1, dtype=np.float64)
        self._previous_iq = 1.0 + 0.0j

    def _modulate(self, audio):
        instantaneous = (self.rf_frequency_error_hz
                         + self.deviation_hz * audio / self.full_scale_audio)
        phase = self._phase + 2 * np.pi * np.cumsum(instantaneous) / self.sample_rate
        if len(phase):
            self._phase = float(np.remainder(phase[-1], 2 * np.pi))
        return np.exp(1j * phase)

    def process(self, audio: np.ndarray) -> ChannelResult:
        samples = np.asarray(audio, dtype=np.float64)
        if samples.ndim != 1:
            raise ValueError("channel audio must be a mono one-dimensional array")
        if not len(samples):
            return ChannelResult(np.zeros(0, np.float32), {})
        transmitted = self._modulate(samples)
        rf = np.zeros(len(transmitted) + self._max_path_delay, dtype=np.complex128)
        path_power = sum(path.amplitude ** 2 for path in self.rf_paths)
        for path in self.rf_paths:
            delay = round(path.delay_seconds * self.sample_rate)
            gain = path.amplitude / np.sqrt(path_power) * np.exp(1j * path.phase_radians)
            rf[delay:delay + len(transmitted)] += gain * transmitted

        carrier_power = float(np.mean(np.abs(rf) ** 2))
        requested_noise_power = carrier_power / 10 ** (self.carrier_to_noise_db / 10)
        sigma = np.sqrt(requested_noise_power / 2)
        noise = (self._rng.normal(0, sigma, len(rf))
                 + 1j * self._rng.normal(0, sigma, len(rf)))
        realized_noise_power = float(np.mean(np.abs(noise) ** 2))
        filtered, self._rf_zi = sosfilt(self._rf_sos, rf + noise, zi=self._rf_zi)
        magnitude = np.abs(filtered)
        limited = filtered / np.maximum(magnitude, 1e-12)
        previous = np.concatenate(([self._previous_iq], limited[:-1]))
        discriminator_hz = np.angle(limited * np.conj(previous)) * self.sample_rate / (2 * np.pi)
        self._previous_iq = limited[-1]
        recovered = discriminator_hz / self.deviation_hz * self.full_scale_audio
        if len(self._audio_fir) > 1:
            extended = np.concatenate((self._audio_history, recovered))
            filtered = fftconvolve(extended, self._audio_fir, mode="full")
            history = len(self._audio_fir) - 1
            recovered = filtered[history:history + len(recovered)]
            self._audio_history = extended[-history:]
        clipped = 0
        if self.audio_clip_limit is not None:
            clipped = int(np.count_nonzero(np.abs(recovered) > self.audio_clip_limit))
            recovered = np.clip(recovered, -self.audio_clip_limit, self.audio_clip_limit)
        if self._clock_up != self._clock_down:
            recovered = resample_poly(recovered, self._clock_up, self._clock_down)
        # Squelch is a receiver-time effect. Apply it after the ADC clock so
        # rational resampling cannot ring backwards into the measured mute.
        muted = min(round(self.leading_mute_seconds * self.sample_rate
                          * self._clock_up / self._clock_down), len(recovered))
        recovered[:muted] = 0.0
        realized_cn = (float("inf") if realized_noise_power == 0 else
                       10 * np.log10(carrier_power / realized_noise_power))
        return ChannelResult(recovered.astype(np.float32), {
            "rf_carrier_to_noise_db": self.carrier_to_noise_db,
            "realized_rf_carrier_to_noise_db": float(realized_cn),
            "rf_carrier_power": carrier_power,
            "rf_noise_power": realized_noise_power,
            "rf_frequency_error_hz": self.rf_frequency_error_hz,
            "deviation_hz": self.deviation_hz,
            "muted_samples": muted,
            "sample_clock_error_ppm": self.sample_clock_error_ppm,
            "clipped_audio_samples": clipped,
        })

    def describe(self) -> Mapping[str, object]:
        return {
            "type": "complex_fm", "sample_rate": self.sample_rate,
            "seed": self.seed, "preset": None if self.preset is None else self.preset.name,
            "carrier_to_noise_db": self.carrier_to_noise_db,
            "carrier_to_noise_reference": "complex_iq_full_nyquist",
            "deviation_hz": self.deviation_hz,
            "full_scale_audio": self.full_scale_audio,
            "rf_bandwidth_hz": self.rf_bandwidth_hz,
            "rf_frequency_error_hz": self.rf_frequency_error_hz,
            "audio_band_6db_hz": self.audio_band_6db_hz,
            "audio_band_10db_hz": self.audio_band_10db_hz,
            "sample_clock_error_ppm": self.sample_clock_error_ppm,
            "leading_mute_seconds": self.leading_mute_seconds,
            "measured_delay_spread_ms": (None if self.preset is None else
                                           self.preset.measured_delay_spread_ms),
            "measurement_source": (None if self.preset is None else
                                     self.preset.measurement_source),
            "rf_paths": [{"delay_seconds": path.delay_seconds,
                          "amplitude": path.amplitude,
                          "phase_radians": path.phase_radians}
                         for path in self.rf_paths],
        }
