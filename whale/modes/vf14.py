"""VF14: the FM control/fallback rungs of `whale.phy.vf14`'s M-FSK waveform.

Three profiles, three mode IDs (an ID is one immutable waveform):

  vf14-16  16 tones, 62.5 Hz spacing, 16 ms symbols, 562.5-1,500 Hz
  vf14-4    4 tones, 600 Hz spacing, 1.667 ms symbols, 600-2,400 Hz
  vf14-8    8 tones, 125 Hz spacing,   8 ms symbols, 625-1,500 Hz

Simulated flat_nbfm C/N floor: vf14-16 -7 dB (20/20 full-capacity frames,
-8 dB did not pass at 0/20); vf14-4 0 dB (20/20 full-capacity frames, -1 dB
did not pass at 14/20); vf14-8 not measured.
"""

from __future__ import annotations

from .. import waveform
from ..phy.vf14 import Vf14Waveform

VF14_16_MODE_ID = 20
VF14_4_MODE_ID = 23
VF14_8_MODE_ID = 21


#: Was the FM control mode; superseded by VF14_4 (below). Kept, and its
#: on-air ID (mode 20) kept unreassigned, as an experimental waveform --
#: see whale/mode_qualification.py's MANIFEST. Old peers that only know
#: mode 20 as control no longer interoperate.
VF14_16 = Vf14Waveform(
    name="vf14-16", mode_id=VF14_16_MODE_ID, tone_count=16,
    symbol_samples=768, first_bin=9,
    sync_symbols=24, payload_symbols=283, short_payload_symbols=71,
    medium_payload_symbols=171,
    confidence_threshold=0.12)

#: Faster DATA waveform: 274 packet bytes (264 application bytes) in 3.36 s.
#: At 600 Bd a VF14_8-length (48-symbol, 80 ms) preamble is too short to hold
#: down the noise-correlation floor; 96 symbols cleared the noise-floor test
#: and reached 40/40 full-capacity frames at 0 dB in isolation, but missed
#: 20/20 against this file's own fixed-seed discriminator-threshold draw
#: (19/20). 144 symbols (240 ms) is the shortest tried that holds 20/20 on
#: both that draw and 40/40 across two fresh 20-trial seed sets.
#: Reads its tones with `dsp.mfsk.fitted_soft_bits`.  Alone among the
#: profiles this one spans the whole FM audio band -- 600 to 2,400 Hz, four
#: tones 600 Hz apart -- so its bank straddles both edges of a radio's audio
#: response, and its bins do not share a noise floor: measured over 20
#: IC-705 <-> digirig captures the 600 Hz bin ran 9-15.5 dB above the
#: quietest, and the per-tone error rate climbed 0.0 / 1.2 / 3.3 / 6.5 %
#: across the bank.  Fitting each bin took those 20 captures from 19/20 to
#: 20/20 and the mean tone error from 1.85 % to 0.11 %.  Decoder-side only:
#: the waveform, and so mode ID 23, is unchanged.
VF14_4 = Vf14Waveform(
    name="vf14-4", mode_id=VF14_4_MODE_ID, tone_count=4,
    symbol_samples=80, first_bin=1,
    sync_symbols=144, payload_symbols=1500, short_payload_symbols=144,
    medium_payload_symbols=400, fec_rate="3/4",
    soft_metric="per_bin",
    confidence_threshold=0.145)

VF14_8 = Vf14Waveform(
    name="vf14-8", mode_id=VF14_8_MODE_ID, tone_count=8,
    symbol_samples=384, first_bin=5,
    sync_symbols=48, payload_symbols=378, short_payload_symbols=96,
    medium_payload_symbols=228,
    confidence_threshold=0.14)

#: VF14_4 is both the control mode and the lowest (most robust) rung of the
#: default ladder.
LADDER = (
    waveform.LadderEntry(VF14_16, "fm", rank=1),
    waveform.LadderEntry(VF14_8, "fm", rank=2),
    waveform.LadderEntry(VF14_4, "fm", rank=3, control=True),
)

PROFILES = {"16": VF14_16, "4": VF14_4, "8": VF14_8}
