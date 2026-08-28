"""HC1 as a negotiable `WaveformMode`, and the HF ladder it anchors.

`vf3_mode` adapts a waveform that only ever carries DATA.  This one adapts
the waveform that carries *everything*: on an HF link HC1 is what
`ModeRegistry.control` resolves to, so every CONNECT, CONNECT_ACK, DISC,
DATA_ACK, floor-control and timing frame rides it, exactly as
`afsk.PROFILE_300` does on VHF FM.  That difference is the whole reason
this adapter exists separately, and it is what the three notes below are
about.

  - **Framing is HC1's own**, as VF3's is: a length field, CRC32 and
    rate-1/2 convolutional code inside the OFDM payload grid, and an OFDM
    header rather than a PN correlation for acquisition.  `whale/framing.py`
    is bypassed.  The link's 10-byte air header is simply the first ten
    bytes of the HC1 payload.
  - **The frame is fixed-length**, as VF3's is -- 695 ms whether it carries
    a 12-byte ACK or a 64-byte chunk.  For VF3 that was a reason to keep the
    mode off the control plane.  Here it is accepted, because the
    alternative is worse: a variable-length OFDM frame needs the receiver to
    learn the length before it can decode, which means a separately coded
    header, and the airtime that would save is small next to what an HF
    keying already spends on PTT, ALC settling and turnaround.  What it buys
    instead is that *every* frame on the link, control included, gets the
    full FEC, CRC32 and frequency correction -- which is the property the
    control plane most needs when the channel is bad.
  - **Head feedback is in seconds**, measured in whole 512-sample sync
    cores.  Same shape as VF3's; the tolerance is one core, the resolution
    of the measurement.

`hf_registry()` at the bottom is the ladder a station on HF runs.  It has
one rung deliberately: HC1 is both the control mode and the only data mode,
so `_maybe_adapt` has nowhere to step and the link is exercised end to end
without a second waveform having to be validated on air first.  Faster HF
rungs stack on top of this the way `registry_with_vf3` stacks on the CPFSK
ladder.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .. import framing
from . import hc1

#: On-air identifier.  0, 1 and 2 are the CPFSK profiles in whale/afsk.py
#: and 3 is VF3; this must stay stable once anything has shipped with it.
#: The number is global across channels even though no registry offers HC1
#: and a CPFSK profile at once -- a mode id means one waveform, everywhere.
HC1_MODE_ID = 4

#: Largest DATA body one HC1 frame can carry, once the link's air header is
#: taken out of the HC1 payload.
CHUNK_SIZE = hc1.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES

#: `hc1.ACQUISITION_THRESHOLD` under the name the link reads it by.  The
#: link uses it for two decisions: whether a decode attempt counts as a real
#: sync lock worth waiting on, and whether the buffer is stale enough to
#: prune.
CONFIDENCE_THRESHOLD = hc1.ACQUISITION_THRESHOLD


class Hc1Codec:
    """Bridges the link's codec calls onto `whale.modes.hc1`."""

    tx_sample_rate = hc1.SAMPLE_RATE
    rx_sample_rate = hc1.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hc1Mode", *, include_head=True,
               head_seconds=hc1.DEFAULT_HEAD_SECONDS) -> np.ndarray:
        if len(payload) > hc1.MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{hc1.MAX_PAYLOAD_BYTES}")
        # include_head=False means "no adaptive guard", not "no lead-in":
        # hc1.lead_in_samples floors at the 48 ms that ramps the transmitter
        # and the sound card up, and there is nothing to gain by trying to
        # go below it.
        if not include_head:
            head_seconds = hc1.DEFAULT_HEAD_SECONDS
        return hc1.modulate(bytes(payload), head_seconds=head_seconds)

    def decode(self, audio, mode: "Hc1Mode", *,
               head_seconds=hc1.DEFAULT_HEAD_SECONDS, **kwargs) -> dict:
        result = hc1.demodulate(audio, head_seconds=head_seconds, **kwargs)
        observed = result.pop("head_cores_received", None)
        if observed is not None:
            # The core count is the diagnostic; the seconds are what the
            # link's head feedback reads.
            result["head_cores_observed"] = observed
            result["head_seconds_received"] = (
                observed * hc1.CORE_SAMPLES / hc1.SAMPLE_RATE)
        return result

    def airtime(self, payload_len: int, mode: "Hc1Mode") -> float:
        del payload_len  # an HC1 frame is the same length whatever it carries
        return hc1.frame_seconds()


HC1_CODEC = Hc1Codec()


@dataclass(frozen=True)
class Hc1Mode:
    """One negotiable HC1 setting, shaped like `afsk.Profile`.

    A separate type rather than a `Profile` with a different codec for
    `Vf3Mode`'s reason: `Profile`'s `baud`/`freq0`/`freq1` describe a
    two-tone CPFSK signal and mean nothing here.  What the link reads is
    this attribute surface, and `baud` is on it because the link reports a
    mode's symbol rate in its logs.
    """

    name: str = "hc1"
    mode_id: int = HC1_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    codec: Hc1Codec = field(default=HC1_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        """HC1's OFDM symbol rate, 75 symbol/s."""
        return hc1.SAMPLE_RATE / hc1.SYMBOL_SAMPLES

    @property
    def head_match_allowance_seconds(self) -> float:
        """One core (10.67 ms) -- the resolution of `hc1._measure_head`.

        The head measurement counts whole cores and the transmitted head is
        deliberately half a core longer than a whole number of them (see
        `hc1.HEAD_PHASE_SAMPLES`), so an observation is short by up to one
        core even on a perfectly received head.  A deficit inside that is
        measurement quantization, not a head that needs lengthening.
        """
        return hc1.CORE_SAMPLES / hc1.SAMPLE_RATE

    def encode(self, payload: bytes, *, include_head=True,
               head_seconds=hc1.DEFAULT_HEAD_SECONDS):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HC1 = Hc1Mode()


def hf_registry():
    """The mode ladder a station on an HF SSB channel runs.

    One rung, which is the honest state of HF support: HC1 is the control
    mode and the data mode both, so a session negotiates it, keys every
    frame with it, and never steps.  Adding a faster HF waveform later means
    appending it here and leaving `control` where it is -- the same shape
    `registry_with_vf3` has on the VHF ladder.

    The CPFSK profiles are deliberately absent rather than kept as a
    fallback.  They have no frequency estimate in them at all, so on SSB
    they are not a more robust rung to fall back to; they are a rung that
    stops working as soon as the two radios disagree about frequency, which
    is the normal condition rather than the exceptional one.
    """
    from ..waveform import ModeRegistry

    return ModeRegistry((HC1,), HC1)
