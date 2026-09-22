"""VF12: 50 Hz OFDM with a 16-QAM/rate-3/4 air format for analog FM.

Geometry, the comb-pilot channel tracker and the framing live in
`whale.phy.vf12`; this module fixes the constellation, the code rate and
the frame budget.

Simulated flat_nbfm C/N floor: not measured.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import waveform
from ..phy.vf12 import Vf12Waveform, mode_for as _mode_for

MODE_ID = 18


@dataclass(frozen=True)
class Vf12Mode(Vf12Waveform):
    name: str = "vf12"
    mode_id: int = MODE_ID
    bits_per_carrier: int = 4
    fec_rate: str = "3/4"


def mode_for(**kwargs):
    return _mode_for(Vf12Mode, **kwargs)


VF12 = Vf12Mode()
MODES = (VF12,)
LADDER = (waveform.LadderEntry(VF12, "fm", rank=9),)
