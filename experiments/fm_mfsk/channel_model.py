"""Calibrated offline replay channel for the ht->ic705 FM MFSK leg.

Phase-1 hardware (10 frames/direction, IC-705 <-> Wouxun HT, see the sweep
notes carried in the task, not retained here per AGENTS.md) measured three
things on the weaker (ht->ic705) leg that this model reproduces:

  * A roughly linear-in-dB *tilt*: received band response falls off from
    550 Hz to 3000 Hz, measured at -10 to -13 dB end to end.
  * A per-tone SNR ceiling that gets worse the more tones are on the air
    at once (K-dependent self-noise): K=8 ran 5-7 dB worse per-tone SNR
    than K=4 at the same drive.
  * Calibration anchors -- three already-measured (mode, symbol-error-rate)
    points this model is fit against:

        G  (M=4, K=4, 150 Bd, 600-2850 Hz): pre-FEC SER ~0.00-0.02%
        K8 (M=2, K=8, 150 Bd, 600-2850 Hz): pre-FEC SER 0.03-0.36%
        H  (M=2, K=4, 300 Bd, 600-2700 Hz): pre-FEC SER 5.6-10.6% overall,
           concentrated in the upper subbands (subband 0 at 600 Hz:
           0.1-0.3%; subband 3 near 2400 Hz: 9-13%)

The model itself is simple on purpose: a fixed linear-in-dB tilt filter
applied to the 48 kHz transmit audio, plus additive white Gaussian noise
whose level is set from one free parameter (`base_snr_db`, the per-tone
SNR at the bottom of the band for a K=4 reference geometry) and one
K-scaling slope (`self_noise_db_per_k_double`). Both free parameters are
fit by `calibrate.py` in this directory against the three anchors above;
see its output for the resulting fit quality.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

BAND_LO_HZ = 500.0
BAND_HI_HZ = 3000.0

#: Fit by experiments/fm_mfsk/calibrate.py against the G/K8/H anchors above.
#: See that script's docstring and printed report for the fit quality.
DEFAULT_TILT_DB = -13.0
DEFAULT_BASE_SNR_DB = 26.0
DEFAULT_SELF_NOISE_DB_PER_K_DOUBLE = 6.0
DEFAULT_K_REFERENCE = 4
#: Extra noise penalty per doubling of symbol rate above `baud_reference`,
#: standing in for the inter-symbol/filter-group-delay smear the phase-1
#: notes flag as the likely extra impairment at 300 Bd that plain tilt+AWGN
#: does not reproduce (see calibrate.py's fit report).
DEFAULT_ISI_DB_PER_BAUD_DOUBLE = 0.0
DEFAULT_BAUD_REFERENCE = 150.0


def tilt_gain(freq_hz: np.ndarray, tilt_db: float,
              lo_hz: float = BAND_LO_HZ, hi_hz: float = BAND_HI_HZ
              ) -> np.ndarray:
    """Linear amplitude gain for a linear-in-dB tilt from 0 dB at `lo_hz`
    to `tilt_db` at `hi_hz`, clamped flat outside the band (a real radio's
    passband is well outside 500-3000 Hz on both sides, so no rolloff is
    modeled beyond it -- only the in-band tilt this data speaks to)."""
    span = max(hi_hz - lo_hz, 1e-9)
    frac = np.clip((np.asarray(freq_hz, dtype=np.float64) - lo_hz) / span,
                   0.0, 1.0)
    return 10.0 ** (tilt_db * frac / 20.0)


def apply_tilt(audio: np.ndarray, sample_rate: int, tilt_db: float,
               lo_hz: float = BAND_LO_HZ, hi_hz: float = BAND_HI_HZ
               ) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float64)
    n = len(audio)
    if n == 0 or tilt_db == 0.0:
        return audio.astype(np.float32)
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    gain = tilt_gain(freqs, tilt_db, lo_hz, hi_hz)
    spectrum = np.fft.rfft(audio) * gain
    return np.fft.irfft(spectrum, n).astype(np.float32)


@dataclass(frozen=True)
class FmHtIc705Channel:
    """Tilt + AWGN + K-dependent self-noise, applied to 48 kHz TX audio.

    `subbands` sets the self-noise penalty (more simultaneous tones, more
    IMD-like self-noise); pass the candidate mode's `subbands` so K=8
    candidates are evaluated under proportionally worse self-noise than
    K=4, and K=2 candidates get a bonus, matching the measured trend.
    """

    tilt_db: float = DEFAULT_TILT_DB
    base_snr_db: float = DEFAULT_BASE_SNR_DB
    self_noise_db_per_k_double: float = DEFAULT_SELF_NOISE_DB_PER_K_DOUBLE
    k_reference: int = DEFAULT_K_REFERENCE
    isi_db_per_baud_double: float = DEFAULT_ISI_DB_PER_BAUD_DOUBLE
    baud_reference: float = DEFAULT_BAUD_REFERENCE
    band_lo_hz: float = BAND_LO_HZ
    band_hi_hz: float = BAND_HI_HZ

    def noise_penalty_db(self, subbands: int, baud: float | None = None) -> float:
        k_ratio = max(subbands, 1) / self.k_reference
        penalty = self.self_noise_db_per_k_double * np.log2(k_ratio)
        if baud:
            baud_ratio = max(baud, 1e-9) / self.baud_reference
            penalty += self.isi_db_per_baud_double * np.log2(baud_ratio)
        return penalty

    def process(self, audio_48k: np.ndarray, sample_rate: int, *,
               subbands: int, seed: int, baud: float | None = None
               ) -> np.ndarray:
        audio = np.asarray(audio_48k, dtype=np.float64)
        tilted = apply_tilt(audio, sample_rate, self.tilt_db,
                            self.band_lo_hz, self.band_hi_hz)
        if not len(tilted):
            return tilted.astype(np.float32)
        # Reference signal power measured in-band on the flat (untilted)
        # transmit signal, so `base_snr_db` means what it says: the
        # per-tone SNR at the bottom of the band (0 dB tilt point) for a
        # K=k_reference, baud=baud_reference geometry.
        signal_power = float(np.mean(audio.astype(np.float64) ** 2))
        penalty_db = self.noise_penalty_db(subbands, baud)
        snr_linear = 10 ** ((self.base_snr_db - penalty_db) / 10.0)
        noise_power = signal_power / max(snr_linear, 1e-30)
        sigma = np.sqrt(max(noise_power, 0.0))
        rng = np.random.default_rng(seed)
        noise = rng.normal(0.0, sigma, len(tilted))
        return (tilted.astype(np.float64) + noise).astype(np.float32)

    def describe(self) -> dict:
        return {
            "type": "fm_ht_ic705_replay", "tilt_db": self.tilt_db,
            "base_snr_db": self.base_snr_db,
            "self_noise_db_per_k_double": self.self_noise_db_per_k_double,
            "k_reference": self.k_reference,
            "isi_db_per_baud_double": self.isi_db_per_baud_double,
            "baud_reference": self.baud_reference,
            "band_lo_hz": self.band_lo_hz, "band_hi_hz": self.band_hi_hz,
        }
