"""Configurable 50 Hz OFDM with an 8-PSK constellation for analog FM."""
from __future__ import annotations

from dataclasses import dataclass

from .vf12 import Vf12Mode


MODE_ID = 22


@dataclass(frozen=True)
class Vf16Mode(Vf12Mode):
    """VF12's signal processing with a fixed 8-PSK/rate-2/3 air format."""

    name: str = "vf16"
    mode_id: int = MODE_ID
    bits_per_carrier: int = 3
    fec_rate: str = "2/3"
    band_lo_hz: float = 500.0
    band_hi_hz: float = 3000.0
    fft_size: int = 240
    cp_len: int = 36
    lead_in_seconds: float = 0.5
    n_codewords: int = 36
    drive_scale: float = 1.0
    pilot_comb_stride: int = 8
    pilot_time_span: int = 3


def mode_for(**kwargs):
    for key in ("carrier_offset_hz", "carrier_spacing_hz"):
        if key in kwargs and kwargs.pop(key) != 50:
            raise ValueError("carrier spacing is fixed at 50 Hz")
    if "bits_per_symbol" in kwargs:
        order = kwargs.pop("bits_per_symbol")
        if "bits_per_carrier" in kwargs and kwargs["bits_per_carrier"] != order:
            raise ValueError("conflicting constellation orders")
        kwargs["bits_per_carrier"] = order
    return Vf16Mode(**kwargs)


VF16 = Vf16Mode()
MODES = (VF16,)
