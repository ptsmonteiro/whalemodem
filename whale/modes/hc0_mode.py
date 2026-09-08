"""HC0 adapter and its position below HC1W in the HF SSB ladder.

HC0 carries the control plane and provides the robust data fallback. Its
fixed-length frames use the common HF lead, length and CRC framing, and
terminated rate-1/2 convolutional coding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .. import framing
from . import hc0, hf_lead

#: On-air identifier; mode IDs identify one immutable waveform globally.
HC0_MODE_ID = 5

#: Largest DATA body one HC0 frame can carry, once the link's air header is
#: taken out of the HC0 payload.
CHUNK_SIZE = hc0.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES

#: `hc0.ACQUISITION_THRESHOLD` under the name the link reads it by.
CONFIDENCE_THRESHOLD = hc0.ACQUISITION_THRESHOLD


class Hc0Codec:
    """Bridges the link's codec calls onto `whale.modes.hc0`."""

    tx_sample_rate = hc0.SAMPLE_RATE
    rx_sample_rate = hc0.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hc0Mode", *, include_head=True,
               head_seconds=hc0.DEFAULT_HEAD_SECONDS) -> np.ndarray:
        if len(payload) > hc0.MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{hc0.MAX_PAYLOAD_BYTES}")
        if not include_head:
            head_seconds = hc0.DEFAULT_HEAD_SECONDS
        body = hc0.modulate(bytes(payload))[hc0.lead_in_samples():]
        return np.concatenate((hf_lead.modulate(hf_lead.HC0_LABEL,
                                                head_seconds), body))

    def decode(self, audio, mode: "Hc0Mode", *,
               head_seconds=hc0.DEFAULT_HEAD_SECONDS, **kwargs) -> dict:
        result = hc0.demodulate(audio, head_seconds=head_seconds, **kwargs)
        result.pop("head_blocks_received", None)
        if (result.get("payload") is not None
                and result.get("start_index") is not None):
            observed, score = hf_lead.measure(
                audio, result["start_index"], hf_lead.HC0_LABEL, head_seconds)
            # The block count is the diagnostic; the seconds are what the
            # link's head feedback and connect-time calibration read.
            result["head_blocks_observed"] = observed
            result["head_seconds_received"] = (
                hf_lead.seconds_received(observed))
            result["head_match"] = score
        return result

    def airtime(self, payload_len: int, mode: "Hc0Mode") -> float:
        del payload_len  # an HC0 frame is the same length whatever it carries
        return ((hf_lead.MIN_SAMPLES + hc0.TOTAL_SYMBOLS * hc0.SYMBOL_SAMPLES
                 + hc0.TAIL_SAMPLES) / hc0.SAMPLE_RATE)


HC0_CODEC = Hc0Codec()


@dataclass(frozen=True)
class Hc0Mode:
    """One negotiable HC0 setting, shaped like `afsk.Profile`."""

    name: str = "hc0"
    mode_id: int = HC0_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    lead_label: int = hf_lead.HC0_LABEL
    codec: Hc0Codec = field(default=HC0_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        """HC0's symbol rate, 93.75 baud -- also its tone spacing."""
        return hc0.BANK.symbol_rate

    @property
    def head_match_allowance_seconds(self) -> float:
        """One common HF lead block (64 ms), the measurement resolution."""
        return hf_lead.BLOCK_SAMPLES / hc0.SAMPLE_RATE

    def encode(self, payload: bytes, *, include_head=True,
               head_seconds=hc0.DEFAULT_HEAD_SECONDS):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HC0 = Hc0Mode()


def hf_registry(fast=True):
    """The mode ladder a station on an HF SSB channel runs.

    HC0 is the control mode and the bottom rung; HC1W is appended above it
    for a channel that can carry it, and `_maybe_adapt` climbs to it after
    a clean streak and falls back after silence, exactly as the VHF ladder
    climbs to VF3.

    `fast=False` drops HC1W, leaving the robust rung alone.  That is for
    measuring HC0 on its own on the bench, not for ordinary operation --
    there is no reason to refuse the faster rung on a path that supports
    it, and a peer that does not advertise mode 16 simply never has it
    selected.

    The CPFSK profiles are absent from both, and not as an oversight: they
    carry no carrier-frequency estimate at all, so on SSB they are not a
    more robust rung to fall back to but one that stops working the moment
    two stations disagree about frequency.
    """
    from ..waveform import ModeRegistry
    from .hc1w_mode import HC1W

    modes = (HC0, HC1W) if fast else (HC0,)
    return ModeRegistry(modes, HC0)
