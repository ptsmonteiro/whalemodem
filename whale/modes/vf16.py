"""VF16: 50 Hz OFDM with an 8-PSK/rate-2/3 air format for analog FM.

VF12's waveform (`whale.phy.vf12`) at a more robust constellation and code
rate, with the frame budget fixed in codewords rather than seconds.

Simulated flat_nbfm C/N floor: not measured.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import waveform
from ..phy.vf12 import Vf12Waveform, mode_for as _mode_for

MODE_ID = 22


@dataclass(frozen=True)
class Vf16Mode(Vf12Waveform):
    name: str = "vf16"
    mode_id: int = MODE_ID
    bits_per_carrier: int = 3
    fec_rate: str = "2/3"
    n_codewords: int = 36


def mode_for(**kwargs):
    return _mode_for(Vf16Mode, **kwargs)


VF16 = Vf16Mode()
MODES = (VF16,)
LADDER = (waveform.LadderEntry(VF16, "fm", rank=8),)
