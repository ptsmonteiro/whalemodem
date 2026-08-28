"""Per-carrier channel estimation and equalization.

Two estimators, for the two structures a frame can offer:

`fit_header` solves a per-carrier least squares against a block of known
symbols at the front of the frame.  It fits a gain *and* an additive
constant per carrier -- the constant absorbs a carrier-frequency leak or a
DC-ish interferer that would otherwise bias the gain -- and the residual
around the fit is a direct per-carrier SNR estimate.  This is VF3's, and
it is what the header is for.

`pilot_phase` tracks the channel *through* the frame from pilot symbols
scattered along it, interpolating each carrier's phase between them.  This
is VF5's `pilot_phase_correct`, which is the one that has been on the air
against a real HF path: a header-only fit assumes the channel holds still
for the whole frame, and over seconds of ionospheric propagation it does
not.  A coherent HF waveform wants this one.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ChannelFit:
    """A per-carrier complex gain, additive offset, and their SNR."""

    gain: np.ndarray
    offset: np.ndarray
    snr_db: np.ndarray

    @property
    def power(self) -> np.ndarray:
        return np.abs(self.gain) ** 2

    def present_carriers(self, floor_db: float = 35.0) -> int:
        """Carriers within `floor_db` of the strongest one.

        A frame arriving through a filter skirt, or with half its band
        notched out, is better rejected than decoded; counting this is how
        a mode decides.
        """
        power = self.power
        return int(np.count_nonzero(power >= np.max(power)
                                    * 10 ** (-floor_db / 10.0)))

    def equalize(self, carriers: np.ndarray) -> np.ndarray:
        return (np.asarray(carriers) - self.offset[None, :]) / self.gain[None, :]


def fit_header(observed: np.ndarray, reference: np.ndarray) -> ChannelFit:
    """Least-squares per-carrier fit of `observed` to known `reference`.

    Both are (symbols, carriers).  Solves `observed = gain * reference +
    offset` per carrier and reports the SNR of the fit.
    """
    observed = np.asarray(observed, dtype=np.complex128)
    reference = np.asarray(reference, dtype=np.complex128)
    if observed.shape != reference.shape:
        raise ValueError("observed and reference grids must match")
    symbols, carriers = observed.shape
    if symbols < 2:
        raise ValueError("a two-parameter fit needs at least two symbols")
    gain = np.empty(carriers, dtype=np.complex128)
    offset = np.empty(carriers, dtype=np.complex128)
    fitted = np.empty_like(observed)
    ones = np.ones(symbols)
    for k in range(carriers):
        design = np.column_stack((reference[:, k], ones))
        gain[k], offset[k] = np.linalg.lstsq(
            design, observed[:, k], rcond=None)[0]
        fitted[:, k] = gain[k] * reference[:, k] + offset[k]
    residual = observed - fitted
    noise = np.mean(np.abs(residual) ** 2, axis=0)
    power = np.abs(gain) ** 2
    snr_db = 10.0 * np.log10(np.maximum(power, 1e-30)
                             / np.maximum(noise, 1e-30))
    return ChannelFit(gain=gain, offset=offset, snr_db=snr_db)


def header_snr(observed: np.ndarray, reference: np.ndarray) -> float:
    """Median per-carrier SNR of a header fit.

    This is the scorer acquisition ranks its candidate starts by: a
    candidate at the true header fits the known symbols well, one on other
    periodic energy does not.
    """
    return float(np.median(fit_header(observed, reference).snr_db))


def carrier_weights(snr_db: np.ndarray, low: float = 0.5,
                    high: float = 2.0) -> np.ndarray:
    """Soft-bit weights from per-carrier SNR, normalized to the median.

    Clipped hard on both sides: the point is to discount a notched carrier
    without letting one strong carrier dominate the whole codeword.
    """
    linear = 10.0 ** (np.asarray(snr_db) / 10.0)
    linear = linear / max(float(np.median(linear)), 1e-30)
    return np.clip(linear, low, high)


def pilot_phase(payload_values: np.ndarray, pilot_positions: np.ndarray,
                pilot_values: np.ndarray, initial_values: np.ndarray,
                initial_reference: np.ndarray
                ) -> tuple[np.ndarray, np.ndarray]:
    """Interpolate per-carrier phase from an anchor plus payload pilots.

    `initial_values` / `initial_reference` are the last header symbol as
    received and as sent -- the anchor at payload position -1.  Phase is
    unwrapped across anchors before interpolation so a rotation passing
    through pi does not fold back on itself.

    Returns the phase-corrected payload and the phase track that was
    removed, the latter being worth reporting: it is a direct picture of
    what the channel did over the frame.
    """
    payload_values = np.asarray(payload_values, dtype=np.complex128)
    pilot_values = np.asarray(pilot_values, dtype=np.complex128)
    pilot_positions = np.asarray(pilot_positions, dtype=np.int64)
    symbols, carriers = payload_values.shape
    anchor_positions = np.concatenate((np.array([-1]), pilot_positions))
    ratios = np.vstack((
        np.asarray(initial_values)[None, :]
        / np.asarray(initial_reference)[None, :],
        payload_values[pilot_positions] / pilot_values,
    ))
    anchor_phases = np.unwrap(np.angle(ratios), axis=0)
    positions = np.arange(symbols)
    phase = np.empty((symbols, carriers), dtype=np.float64)
    for carrier in range(carriers):
        phase[:, carrier] = np.interp(
            positions, anchor_positions, anchor_phases[:, carrier])
    return payload_values * np.exp(-1j * phase), phase
