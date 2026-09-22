"""Coherent-QPSK SC-FDE with rate-3/4 LDPC for analog FM handhelds.

The high-rate coherent rung of the FM SC-FDE family: the one for a
full-quieting signal. It is VFS2's waveform at a weaker code rate and
nothing else -- same 64-bin DFT-spread QPSK at 3,765 bit/s raw, same block
geometry, same preamble, same scattered-pilot equalizer, all from
`whale.phy.scfde`. Rate 3/4 instead of 1/2 carries 1.5x the payload in the
same airtime, and theory puts the cost at roughly 4 dB of required SNR.

Simulated flat_nbfm C/N floor: 2 dB.
"""
from __future__ import annotations

from dataclasses import dataclass

from whale import waveform
from whale.phy.scfde import ScFdeMode

MODE_ID = 26


@dataclass(frozen=True)
class Vfs3Mode(ScFdeMode):
    """VFS2's air format at rate 3/4.

    `n_codewords` matches VFS2's deliberately: an LDPC codeword is 648 coded
    bits at every rate, so the same count occupies the same blocks and the
    same airtime, and the two rungs differ only in delivered payload.
    """

    name: str = "vfs3"
    mode_id: int = MODE_ID
    bits_per_symbol_order: int = 2  # QPSK
    fec_rate: str = "3/4"
    n_codewords: int = 24
    pilot_block_stride: int = 10


VFS3 = Vfs3Mode()
MODES = (VFS3,)
LADDER = (waveform.LadderEntry(VFS3, "fm", rank=7),)
