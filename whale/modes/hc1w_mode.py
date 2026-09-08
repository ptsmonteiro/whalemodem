"""HC1W's link-layer adapter.

HC1W is a fixed-length DATA mode with the shared HF lead. Mode ID 16 identifies
its 23-carrier waveform and K=9 convolutional code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .. import framing
from . import hc1w, hf_lead

#: On-air identifier; mode IDs identify one immutable waveform globally.
HC1W_MODE_ID = 16

#: Largest DATA body one HC1W frame can carry after the link air header.
CHUNK_SIZE = hc1w.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES

#: `hc1w.ACQUISITION_THRESHOLD` under the name the link reads it by.  The
#: link uses it for two decisions: whether a decode attempt counts as a real
#: sync lock worth waiting on, and whether the buffer is stale enough to
#: prune.
CONFIDENCE_THRESHOLD = hc1w.ACQUISITION_THRESHOLD


class Hc1wCodec:
    """Bridges the link's codec calls onto `whale.modes.hc1w`."""

    tx_sample_rate = hc1w.SAMPLE_RATE
    rx_sample_rate = hc1w.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hc1wMode", *, include_head=True,
               head_seconds=hc1w.DEFAULT_HEAD_SECONDS) -> np.ndarray:
        if len(payload) > hc1w.MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{hc1w.MAX_PAYLOAD_BYTES}")
        # include_head=False means "no adaptive guard", not "no lead-in":
        # hc1w.lead_in_samples floors at the 48 ms that ramps the transmitter
        # and the sound card up, and there is nothing to gain by trying to
        # go below it.
        if not include_head:
            head_seconds = hc1w.DEFAULT_HEAD_SECONDS
        body = hc1w.modulate(bytes(payload))[hc1w.lead_in_samples():]
        return np.concatenate((hf_lead.modulate(hf_lead.HC1W_LABEL,
                                                head_seconds), body))

    def decode(self, audio, mode: "Hc1wMode", *,
               head_seconds=hc1w.DEFAULT_HEAD_SECONDS, **kwargs) -> dict:
        result = hc1w.demodulate(audio, head_seconds=head_seconds, **kwargs)
        result.pop("head_cores_received", None)
        if (result.get("payload") is not None
                and result.get("start_index") is not None):
            observed, score = hf_lead.measure(
                audio, result["start_index"], hf_lead.HC1W_LABEL, head_seconds)
            # The block count is the diagnostic; the seconds are what the
            # link's head feedback reads.
            result["head_blocks_observed"] = observed
            result["head_seconds_received"] = (
                hf_lead.seconds_received(observed))
            result["head_match"] = score
        return result

    def airtime(self, payload_len: int, mode: "Hc1wMode") -> float:
        del payload_len  # an HC1W frame is fixed length
        return ((hf_lead.MIN_SAMPLES + hc1w.TOTAL_SYMBOLS * hc1w.SYMBOL_SAMPLES
                 + hc1w.TAIL_SAMPLES) / hc1w.SAMPLE_RATE)


HC1W_CODEC = Hc1wCodec()


@dataclass(frozen=True)
class Hc1wMode:
    """The default wideband HF differential-QPSK mode."""

    name: str = "hc1w"
    mode_id: int = HC1W_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    lead_label: int = hf_lead.HC1W_LABEL
    codec: Hc1wCodec = field(default=HC1W_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        """HC1W's OFDM symbol rate, 75 symbol/s."""
        return hc1w.SAMPLE_RATE / hc1w.SYMBOL_SAMPLES

    @property
    def head_match_allowance_seconds(self) -> float:
        """One common HF lead block (64 ms), the measurement resolution."""
        return hf_lead.BLOCK_SAMPLES / hc1w.SAMPLE_RATE

    def encode(self, payload: bytes, *, include_head=True,
               head_seconds=hc1w.DEFAULT_HEAD_SECONDS):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HC1W = Hc1wMode()
