"""Fixed non-coherent 2400 bit/s 4-FSK experimental mode.

This is intentionally a candidate rather than a negotiated ``whale`` mode.
It uses the framing, continuous-phase modulator, synchroniser, four-filter
energy detector, and Gray mapping implemented by :mod:`mfsk`.

The requested 400 Hz tone spacing is only one third of the 1200 baud symbol
rate.  It is therefore not non-coherently orthogonal over one symbol (that
would require 1200 Hz spacing), but the exact requested waveform is useful
as an explicitly experimental packed-tone profile.
"""

from mfsk import MfskProfile, demodulate, modulate


PROFILE = MfskProfile(
    name="4fsk_2400",
    m=4,
    symbol_rate=1200.0,
    freq_base=800.0,
    spacing=400.0,
)

SAMPLE_RATES = (9600, 48000)

__all__ = ["PROFILE", "SAMPLE_RATES", "modulate", "demodulate"]
