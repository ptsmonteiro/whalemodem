"""HF2 as a negotiable, experimental `WaveformMode`.

HF2 is a pilot-assisted coherent 16-QAM OFDM data mode targeting Level 2 of
the HF SSB speed ladder in `SPEED_LADDERS.md` (general-purpose data, quiet
Watterson fading at +5 dB and above, moderate at +10 dB and above). It
carries frequency-diversity carrier grouping -- each 16-QAM value is sent on
2-3 physical carriers spread across the band -- to survive the persistent
local fades that a plain comb-pilot equalizer alone could not absorb. See
`experiments/hf2/DESIGN.md` for the concrete geometry, constellation, pilot
layout and coding choices and why each was picked, and
`experiments/hf2/RESULTS.md` for the Monte Carlo qualification evidence
(>=300-trial confirmed boundary at both required Level 2 envelope points).

This module only wires the already-designed and already-qualified
`experiments/hf2/hf2.py` waveform into the link's `WaveformMode` contract,
mirroring `whale/modes/hc1_mode.py`'s pattern; it does not re-derive or
re-run any of that design or evidence. HF2 is registered as EXPERIMENTAL
only (see `whale/mode_qualification.py`) -- it is not a default or optional
mode on any ladder, and remains a step beyond `hf-ssb`'s HC0/HC1 rungs for a
station that opts into the experimental registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from experiments.hf2 import hf2

from .. import framing
from . import hf_lead

#: On-air identifier. 0, 1, 2 are the CPFSK profiles in whale/afsk.py, 3 is
#: VF3, 4 is HC1, 5 is HC0, 6 is VF6; this must stay stable once anything
#: has shipped with it. The number is global across channels even though no
#: registry offers HF2 and a CPFSK profile at once -- a mode id means one
#: waveform, everywhere.
HF2_MODE_ID = 7

#: Largest DATA body one HF2 frame can carry, once the link's air header is
#: taken out of the HF2 payload.
CHUNK_SIZE = hf2.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES

#: `hf2.ACQUISITION_THRESHOLD` under the name the link reads it by.
CONFIDENCE_THRESHOLD = hf2.ACQUISITION_THRESHOLD


class Hf2Codec:
    """Bridges the link's codec calls onto `experiments.hf2.hf2`."""

    tx_sample_rate = hf2.SAMPLE_RATE
    rx_sample_rate = hf2.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hf2Mode", *, include_head=True,
               head_seconds=hf2.DEFAULT_HEAD_SECONDS) -> np.ndarray:
        if len(payload) > hf2.MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{hf2.MAX_PAYLOAD_BYTES}")
        # include_head=False means "no adaptive guard", not "no lead-in":
        # hf2.lead_in_samples floors at the minimum lead that ramps the
        # transmitter and the sound card up, and there is nothing to gain by
        # trying to go below it.
        if not include_head:
            head_seconds = hf2.DEFAULT_HEAD_SECONDS
        body = hf2.modulate(bytes(payload))[hf2.lead_in_samples():]
        return np.concatenate((hf_lead.modulate(hf_lead.HF2_LABEL,
                                                head_seconds), body))

    def decode(self, audio, mode: "Hf2Mode", *,
               head_seconds=hf2.DEFAULT_HEAD_SECONDS, **kwargs) -> dict:
        result = hf2.demodulate(audio, head_seconds=head_seconds, **kwargs)
        if (result.get("payload") is not None
                and result.get("start_index") is not None):
            observed, score = hf_lead.measure(
                audio, result["start_index"], hf_lead.HF2_LABEL, head_seconds)
            # The block count is the diagnostic; the seconds are what the
            # link's head feedback reads.
            result["head_blocks_observed"] = observed
            result["head_seconds_received"] = (
                hf_lead.seconds_received(observed))
            result["head_match"] = score
        return result

    def airtime(self, payload_len: int, mode: "Hf2Mode") -> float:
        del payload_len  # an HF2 frame is the same length whatever it carries
        return ((hf_lead.MIN_SAMPLES + hf2.TOTAL_SYMBOLS * hf2.SYMBOL_SAMPLES
                 + hf2.TAIL_SAMPLES) / hf2.SAMPLE_RATE)


HF2_CODEC = Hf2Codec()


@dataclass(frozen=True)
class Hf2Mode:
    """One negotiable HF2 setting, shaped like `hc1_mode.Hc1Mode`."""

    name: str = "hf2"
    mode_id: int = HF2_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    lead_label: int = hf_lead.HF2_LABEL
    codec: Hf2Codec = field(default=HF2_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        """HF2's OFDM symbol rate."""
        return hf2.SAMPLE_RATE / hf2.SYMBOL_SAMPLES

    @property
    def head_match_allowance_seconds(self) -> float:
        """One common HF lead block, the measurement resolution."""
        return hf_lead.BLOCK_SAMPLES / hf2.SAMPLE_RATE

    def encode(self, payload: bytes, *, include_head=True,
               head_seconds=hf2.DEFAULT_HEAD_SECONDS):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HF2 = Hf2Mode()
