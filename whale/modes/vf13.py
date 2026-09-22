"""VF13: combinatorial noncoherent MFSK for analog FM.

The waveform -- codebook, geometry, framing and demodulator -- lives in
`whale.phy.vf13`, whose geometry comes from HF16
(`experiments/hf16_mfsk_lowsnr`, retained there, not here) transplanted to
the FM passband. This module fixes the shipped configuration: 16 tones, 6
active per symbol (12 bits/symbol, C(16,6)=8008 truncated to a 4,096-entry
codebook), 150 Bd (320 samples at 48 kHz, 150 Hz spacing), tones from
600-3,000 Hz, punctured K=7 convolutional rate 7/8, interleaved, a 7.983 s
fixed frame: 1,430.0 net application bit/s.

Measured on radios (IC-705 <-> Wouxun KG-UV9D Plus FM, 2026-09-14): 60/60
exact-payload frames (30/30 ic705->ht, 30/30 ht->ic705). Installed as
DEFAULT below the faster OFDM data rungs.

Simulated flat_nbfm C/N floor: 2 dB (20/20 full-capacity frames, 1 dB did
not pass at 2/20).
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import dsp, waveform
from ..phy.vf13 import TX_SAMPLE_RATE, Vf13Waveform

VF13_MODE_ID = 19


@dataclass(frozen=True)
class Vf13Mode(Vf13Waveform):
    name: str = "vf13"
    mode_id: int = VF13_MODE_ID


def mode_for(tone_count=16, *, subbands=1, frame_seconds=None,
             payload_symbols=None, repeat=1, constraint=7, sync_seconds=0.5,
             symbol_samples=320, **kwargs) -> Vf13Mode:
    """Build a mode by target frame duration instead of symbol count."""
    probe_kwargs = {k: v for k, v in kwargs.items() if k != "fec_rate"}
    probe = Vf13Mode(tone_count=tone_count, subbands=subbands,
                     symbol_samples=symbol_samples, payload_symbols=8,
                     repeat=1, constraint=constraint,
                     sync_seconds=sync_seconds, **probe_kwargs)
    if payload_symbols is None:
        if frame_seconds is None:
            raise ValueError("give frame_seconds or payload_symbols")
        overhead = (probe.head_symbols + probe.sync_symbols) * probe.symbol_seconds
        overhead += probe.tail_samples / TX_SAMPLE_RATE
        payload_symbols = int((frame_seconds - overhead)
                              / (probe.symbol_seconds + probe.guard_seconds))
    bps = probe.bits_per_symbol
    fec_rate = kwargs.get("fec_rate", "1/2")
    kept = (None if fec_rate == "1/2"
            else int(dsp.fec.PUNCTURE_PATTERNS[fec_rate].sum()))
    quantum = repeat * 2

    def _bad(n):
        coded = n * bps
        if coded % quantum:
            return True
        return kept is not None and (coded // repeat) % kept
    while payload_symbols > 0 and _bad(payload_symbols):
        payload_symbols -= 1
    if payload_symbols <= 0:
        raise ValueError("frame_seconds is too short for this geometry")
    return Vf13Mode(tone_count=tone_count, subbands=subbands,
                    symbol_samples=symbol_samples,
                    payload_symbols=payload_symbols, repeat=repeat,
                    constraint=constraint, sync_seconds=sync_seconds,
                    **kwargs)


#: The shipped instance: config A (see module docstring), 1,438.8 net bit/s.
VF13 = Vf13Mode()

LADDER = (waveform.LadderEntry(VF13, "fm", rank=4),)
