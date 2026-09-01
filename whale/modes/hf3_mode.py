"""HF3 as a negotiable, experimental `WaveformMode`.

HF3 is a sparse-pilot coherent 16-QAM OFDM data mode targeting Level 3 of
the HF SSB speed ladder in `SPEED_LADDERS.md` (fast data: benign/static
fading at +8 dB waveform SNR and above, quiet Watterson fading at +10 dB
and above). See `experiments/hf3/DESIGN.md` for the concrete geometry,
constellation, pilot layout and coding choices and why each was picked
(including the iteration record -- a denser 64-QAM candidate and several
pilot/frame-size combinations were tried and measured before this one),
and `experiments/hf3/RESULTS.md` for the Monte Carlo qualification
evidence.

This module only wires the already-designed `experiments/hf3/hf3.py`
waveform into the link's `WaveformMode` contract, mirroring
`whale/modes/hf2_mode.py`'s pattern; it does not re-derive or re-run any of
that design or evidence. HF3 is registered as EXPERIMENTAL only (see
`whale/mode_qualification.py`) -- it is not a default or optional mode on
any ladder.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from experiments.hf3 import hf3

from .. import framing
from . import hf_lead

#: On-air identifier. 0, 1, 2 are the CPFSK profiles in whale/afsk.py, 3 is
#: VF3, 4 is HC1, 5 is HC0, 6 is VF6, 7 is HF2, 8 is VF4; this must stay
#: stable once anything has shipped with it. The number is global across
#: channels even though no registry offers HF3 and a CPFSK profile at once
#: -- a mode id means one waveform, everywhere.
HF3_MODE_ID = 9

#: Largest DATA body one HF3 frame can carry, once the link's air header is
#: taken out of the HF3 payload.
CHUNK_SIZE = hf3.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES

#: `hf3.ACQUISITION_THRESHOLD` under the name the link reads it by.
CONFIDENCE_THRESHOLD = hf3.ACQUISITION_THRESHOLD


class Hf3Codec:
    """Bridges the link's codec calls onto `experiments.hf3.hf3`."""

    tx_sample_rate = hf3.SAMPLE_RATE
    rx_sample_rate = hf3.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hf3Mode", *, include_head=True,
               head_seconds=hf3.DEFAULT_HEAD_SECONDS) -> np.ndarray:
        if len(payload) > hf3.MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{hf3.MAX_PAYLOAD_BYTES}")
        # include_head=False means "no adaptive guard", not "no lead-in":
        # hf3.lead_in_samples floors at the minimum lead that ramps the
        # transmitter and the sound card up, and there is nothing to gain by
        # trying to go below it.
        if not include_head:
            head_seconds = hf3.DEFAULT_HEAD_SECONDS
        body = hf3.modulate(bytes(payload))[hf3.lead_in_samples():]
        return np.concatenate((hf_lead.modulate(hf_lead.HF3_LABEL,
                                                head_seconds), body))

    def decode(self, audio, mode: "Hf3Mode", *,
               head_seconds=hf3.DEFAULT_HEAD_SECONDS, **kwargs) -> dict:
        result = hf3.demodulate(audio, head_seconds=head_seconds, **kwargs)
        if (result.get("payload") is not None
                and result.get("start_index") is not None):
            observed, score = hf_lead.measure(
                audio, result["start_index"], hf_lead.HF3_LABEL, head_seconds)
            # The block count is the diagnostic; the seconds are what the
            # link's head feedback reads.
            result["head_blocks_observed"] = observed
            result["head_seconds_received"] = (
                hf_lead.seconds_received(observed))
            result["head_match"] = score
        return result

    def airtime(self, payload_len: int, mode: "Hf3Mode") -> float:
        del payload_len  # an HF3 frame is the same length whatever it carries
        return ((hf_lead.MIN_SAMPLES + hf3.TOTAL_SYMBOLS * hf3.SYMBOL_SAMPLES
                 + hf3.TAIL_SAMPLES) / hf3.SAMPLE_RATE)


HF3_CODEC = Hf3Codec()


@dataclass(frozen=True)
class Hf3Mode:
    """One negotiable HF3 setting, shaped like `hf2_mode.Hf2Mode`."""

    name: str = "hf3"
    mode_id: int = HF3_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    lead_label: int = hf_lead.HF3_LABEL
    codec: Hf3Codec = field(default=HF3_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        """HF3's OFDM symbol rate."""
        return hf3.SAMPLE_RATE / hf3.SYMBOL_SAMPLES

    @property
    def head_match_allowance_seconds(self) -> float:
        """One common HF lead block, the measurement resolution."""
        return hf_lead.BLOCK_SAMPLES / hf3.SAMPLE_RATE

    def encode(self, payload: bytes, *, include_head=True,
               head_seconds=hf3.DEFAULT_HEAD_SECONDS):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HF3 = Hf3Mode()
