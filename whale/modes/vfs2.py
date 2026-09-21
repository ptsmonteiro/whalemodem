"""Coherent-QPSK SC-FDE with rate-1/2 LDPC for analog FM handhelds.

The workhorse rung of the FM SC-FDE family: the rate most HT-to-HT paths
will actually hold. Geometry, coherent detection and the scattered-pilot
equalizer all live in `whale.phy.scfde`; this module only fixes the
constellation, the code rate and the frame budget.

Simulated flat_nbfm C/N floor: 0 dB (20/20 full-capacity frames; -1 dB did
not pass, 12/20).
"""
from __future__ import annotations

from dataclasses import dataclass

from whale.phy.scfde import ScFdeMode

MODE_ID = 25


@dataclass(frozen=True)
class Vfs2Mode(ScFdeMode):
    name: str = "vfs2"
    mode_id: int = MODE_ID
    bits_per_symbol_order: int = 2  # QPSK
    fec_rate: str = "1/2"
    n_codewords: int = 24
    pilot_block_stride: int = 10


VFS2 = Vfs2Mode()
MODES = (VFS2,)
