"""HF2 as a negotiable `WaveformMode`.

HF2 is a pilot-assisted coherent 16-QAM OFDM data mode targeting Level 2 of
the HF speed ladder in `SPEED_LADDERS.md` (general-purpose data, quiet
Watterson fading at +14 dB and above, moderate at +19 dB and above under the
current SNR/3 kHz convention). It
carries frequency-diversity carrier grouping -- each 16-QAM value is sent on
2-3 physical carriers spread across the band -- to survive the persistent
local fades that a plain comb-pilot equalizer alone could not absorb. See
`experiments/hf2/DESIGN.md` for the concrete geometry, constellation, pilot
layout and coding choices and why each was picked, and
`experiments/hf2/RESULTS.md` for the original Monte Carlo evidence and the
retained paired comparison for current-convention measurements.

This module only wires the already-designed `whale/phy/hf2.py` waveform
(developed in the retired `experiments/hf2/`) into the link's `WaveformMode`
contract,
mirroring `whale/modes/hc1w_mode.py`'s pattern; it does not re-derive or
re-run any of that design or evidence. The owner made HF2 a DEFAULT product
mode despite its still-failing occupied-bandwidth gate; see
`whale/mode_qualification.py` and `MODE_QUALIFICATION.md`. Default availability
is not a qualification claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from whale.phy import hf2

from .. import framing

#: On-air identifier; mode IDs identify one immutable waveform globally.
HF2_MODE_ID = 7

#: Largest DATA body one HF2 frame can carry, once the link's air header is
#: taken out of the HF2 payload.
CHUNK_SIZE = hf2.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES

#: `hf2.ACQUISITION_THRESHOLD` under the name the link reads it by.
CONFIDENCE_THRESHOLD = hf2.ACQUISITION_THRESHOLD


class Hf2Codec:
    """Bridges the link's codec calls onto `whale.phy.hf2`."""

    tx_sample_rate = hf2.SAMPLE_RATE
    rx_sample_rate = hf2.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hf2Mode") -> np.ndarray:
        if len(payload) > hf2.MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{hf2.MAX_PAYLOAD_BYTES}")
        del mode
        return hf2.modulate(bytes(payload))

    def decode(self, audio, mode: "Hf2Mode", **kwargs) -> dict:
        return hf2.demodulate(audio, **kwargs)

    def airtime(self, payload_len: int, mode: "Hf2Mode") -> float:
        del payload_len  # an HF2 frame is the same length whatever it carries
        return hf2.frame_seconds()


HF2_CODEC = Hf2Codec()


@dataclass(frozen=True)
class Hf2Mode:
    """One negotiable HF2 setting, shaped like `hc1w_mode.Hc1wMode`."""

    name: str = "hf2"
    mode_id: int = HF2_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
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

    def encode(self, payload: bytes):
        return self.codec.encode(payload, self)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HF2 = Hf2Mode()
