"""HC0 as a negotiable `WaveformMode`, and the HF ladder it anchors.

HC0 is the bottom rung and the control mode of the HF SSB ladder: every
CONNECT, CONNECT_ACK, DISC, DATA_ACK, floor-control and timing frame rides
it, and a DATA transfer falls back to it when nothing faster holds.  HC1
sits above it for a channel that can carry an OFDM frame.

    rung   waveform                        payload  keying  works to
    hc0    16-FSK, non-coherent               64 B  3.38 s    -16 dB
    hc1    19-carrier differential-QPSK OFDM  74 B  0.69 s   +3.5 dB

Those two numbers are the reason the ladder is this way round rather than
HC1 being the only HF mode.  19.5 dB is not a refinement; it is the
difference between a link that exists on a weak path and one that does not.

The adapter is shaped exactly like `vf3_mode` and `hc1_mode`, and the three
things it has to reconcile are the same three:

  - **Framing is HC0's own.**  A length field, CRC32 and rate-1/2
    convolutional code inside the tone grid, and a known tone pattern for
    acquisition rather than a PN correlation.  `whale/framing.py` is
    bypassed; the link's 10-byte air header is the first ten bytes of the
    HC0 payload.
  - **The frame is fixed-length** -- 3.38 s whether it carries a 12-byte
    ACK or a 54-byte chunk.  Accepted for the reason `hc1_mode` sets out:
    a variable-length frame needs the receiver to learn the length before
    it can decode, and what a fixed one buys is that every frame on the
    link gets the full coding.  The cost is real and it is the price of
    the rung working at all.
  - **Head feedback is in seconds**, measured in whole four-symbol head
    blocks.  The tolerance is one block, the resolution of the
    measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .. import framing
from . import hc0

#: On-air identifier.  0, 1 and 2 are the CPFSK profiles in whale/afsk.py,
#: 3 is VF3 and 4 is HC1.  The *name* orders the HF ladder -- hc0 is the
#: rung below hc1 -- and the id is simply the next free number, which
#: carries no ordering at all; `ModeRegistry` takes the order from the
#: sequence it is built with.
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
        return hc0.modulate(bytes(payload), head_seconds=head_seconds)

    def decode(self, audio, mode: "Hc0Mode", *,
               head_seconds=hc0.DEFAULT_HEAD_SECONDS, **kwargs) -> dict:
        result = hc0.demodulate(audio, head_seconds=head_seconds, **kwargs)
        observed = result.pop("head_blocks_received", None)
        if observed is not None:
            # The block count is the diagnostic; the seconds are what the
            # link's head feedback and connect-time calibration read.
            result["head_blocks_observed"] = observed
            result["head_seconds_received"] = (
                observed * hc0.HEAD_BLOCK_SAMPLES / hc0.SAMPLE_RATE)
        return result

    def airtime(self, payload_len: int, mode: "Hc0Mode") -> float:
        del payload_len  # an HC0 frame is the same length whatever it carries
        return hc0.frame_seconds()


HC0_CODEC = Hc0Codec()


@dataclass(frozen=True)
class Hc0Mode:
    """One negotiable HC0 setting, shaped like `afsk.Profile`."""

    name: str = "hc0"
    mode_id: int = HC0_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
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
        """One head block (42.7 ms) -- the resolution of the measurement."""
        return hc0.HEAD_BLOCK_SAMPLES / hc0.SAMPLE_RATE

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

    HC0 is the control mode and the bottom rung; HC1 is appended above it
    for a channel that can carry it, and `_maybe_adapt` climbs to it after
    a clean streak and falls back after silence, exactly as the VHF ladder
    climbs to VF3.

    `fast=False` drops HC1, leaving the robust rung alone.  That is for
    measuring HC0 on its own on the bench, not for ordinary operation --
    there is no reason to refuse the faster rung on a path that supports
    it, and a peer that does not advertise mode 4 simply never has it
    selected.

    The CPFSK profiles are absent from both, and not as an oversight: they
    carry no carrier-frequency estimate at all, so on SSB they are not a
    more robust rung to fall back to but one that stops working the moment
    two stations disagree about frequency.
    """
    from ..waveform import ModeRegistry
    from .hc1_mode import HC1

    modes = (HC0, HC1) if fast else (HC0,)
    return ModeRegistry(modes, HC0)
