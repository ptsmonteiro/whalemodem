"""OFDM symbol build and analyze, parameterized on the frame geometry.

A mode declares its geometry once -- sample rate, core length, cyclic
prefix, which FFT bins carry data, and the time-domain amplitude scale --
and everything downstream (acquisition, timing, equalization) is written
against that object rather than against module-level constants.

The transforms themselves are VF3's, unchanged in order of operations:
`build_symbol` scales after the inverse FFT and `symbol_carriers` divides
after the forward one, so the two remain exact inverses and the recorded
captures keep replaying bit-for-bit.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np


@dataclass(frozen=True)
class Geometry:
    """One OFDM symbol's shape.

    `time_scale` is the factor `build_symbol` applies after the inverse
    FFT, chosen by the mode so the transmitted frame lands at its target
    RMS; `symbol_carriers` divides it back out so the analyzed carriers
    come back at unit energy regardless.  `unscaled_rms` computes the
    conventional choice for a mode that wants a particular TX RMS.
    """

    sample_rate: int
    core_samples: int
    guard_samples: int
    carrier_bins: np.ndarray
    time_scale: float = 1.0

    def __post_init__(self) -> None:
        bins = np.asarray(self.carrier_bins, dtype=np.int32)
        bins.flags.writeable = False
        object.__setattr__(self, "carrier_bins", bins)
        if bins.ndim != 1 or not len(bins):
            raise ValueError("carrier_bins must be a non-empty 1-D array")
        if np.any(bins < 1) or np.any(bins >= self.core_samples // 2):
            # Bin 0 is DC and bin N/2 is Nyquist; neither has a conjugate
            # partner, so a real symbol cannot carry information there.
            raise ValueError("carrier bins must lie in [1, core_samples/2)")
        if len(np.unique(bins)) != len(bins):
            raise ValueError("carrier bins must be distinct")
        if not 0 <= self.guard_samples <= self.core_samples:
            raise ValueError("guard must be within [0, core_samples]")

    @property
    def symbol_samples(self) -> int:
        return self.guard_samples + self.core_samples

    @property
    def carrier_count(self) -> int:
        return len(self.carrier_bins)

    @property
    def carrier_spacing_hz(self) -> float:
        return self.sample_rate / self.core_samples

    @cached_property
    def carrier_hz(self) -> np.ndarray:
        return self.carrier_bins.astype(np.float64) * self.carrier_spacing_hz

    @property
    def symbol_seconds(self) -> float:
        return self.symbol_samples / self.sample_rate

    @property
    def unscaled_rms(self) -> float:
        """RMS of a unit-energy QPSK symbol built at `time_scale` 1.0."""
        return np.sqrt(2.0 * self.carrier_count) / self.core_samples

    def scaled_to_rms(self, target_rms: float) -> "Geometry":
        """This geometry with the `time_scale` that hits `target_rms`."""
        from dataclasses import replace

        return replace(self, time_scale=target_rms / self.unscaled_rms)


def build_symbol(geometry: Geometry, values: np.ndarray) -> np.ndarray:
    """One cyclic-prefixed OFDM symbol carrying `values`, as real audio."""
    values = np.asarray(values, dtype=np.complex128).reshape(-1)
    if len(values) != geometry.carrier_count:
        raise ValueError(
            f"expected {geometry.carrier_count} carriers, got {len(values)}")
    spectrum = np.zeros(geometry.core_samples, dtype=np.complex128)
    spectrum[geometry.carrier_bins] = values
    spectrum[-geometry.carrier_bins] = np.conj(values)
    core = np.fft.ifft(spectrum).real * geometry.time_scale
    if not geometry.guard_samples:
        # `core[-0:]` is the whole core, not an empty slice, which would
        # silently emit a doubled symbol for a guard-less geometry.
        return core
    return np.concatenate((core[-geometry.guard_samples:], core))


def symbol_carriers(geometry: Geometry, symbol_audio: np.ndarray,
                    offset: int | None = None) -> np.ndarray:
    """Recover the carriers of one symbol, FFT window starting at `offset`.

    `offset` is measured from the start of the cyclic prefix and defaults
    to the end of it.  Anywhere inside the guard is valid: the phase ramp
    the early window introduces is divided back out, which is what lets
    symbol timing wander within the prefix without touching the
    constellation.
    """
    if offset is None:
        offset = geometry.guard_samples
    audio = np.asarray(symbol_audio)
    if len(audio) < geometry.symbol_samples:
        raise ValueError(
            f"a complete {geometry.symbol_samples}-sample symbol is required")
    if not 0 <= offset <= geometry.guard_samples:
        raise ValueError(f"FFT offset must be in [0, {geometry.guard_samples}]")
    spectrum = np.fft.fft(
        audio[offset:offset + geometry.core_samples])[geometry.carrier_bins]
    core_index = (offset - geometry.guard_samples) % geometry.core_samples
    undo_shift = np.exp(-2j * np.pi * geometry.carrier_bins * core_index
                        / geometry.core_samples)
    return spectrum * undo_shift / geometry.time_scale


def carrier_bank(geometry: Geometry, analytic: np.ndarray, start: int,
                 symbol_count: int, intercept: float = 0.0,
                 slope: float = 0.0, offset: int | None = None
                 ) -> np.ndarray | None:
    """Analyze `symbol_count` consecutive symbols from `start`.

    `intercept` and `slope` are the sample-clock fit from
    `whale.dsp.timing`: symbol *i* is read at a shift of
    `intercept + slope * i`, which tracks a drifting clock across the
    frame.  Returns None -- rather than a partial bank -- when the frame
    runs off the end of the capture, so a caller can tell "still arriving"
    from "decoded badly".

    Note this deliberately does *not* undo the prefix phase ramp the way
    `symbol_carriers` does: acquisition hands it a `start` already at the
    prefix boundary and the per-carrier equalizer absorbs what is left.
    """
    carriers = np.empty((symbol_count, geometry.carrier_count),
                        dtype=np.complex128)
    if offset is None:
        offset = geometry.guard_samples
    for i in range(symbol_count):
        shift = int(round(intercept + slope * i))
        at = start + i * geometry.symbol_samples + shift + offset
        if at < 0 or at + geometry.core_samples > len(analytic):
            return None
        carriers[i] = np.fft.fft(
            analytic[at:at + geometry.core_samples])[geometry.carrier_bins]
    return carriers
