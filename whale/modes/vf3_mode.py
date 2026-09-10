"""VF3 as a negotiable `WaveformMode`.

VF3 is the 58-carrier differential-QPSK OFDM frame validated on the bench in
`experiments/vf3/` -- 6/6 full-capacity frames byte-for-byte in both
directions -- at about 2,200 net user bit/s against `PROFILE_1200`'s 947.
This module is the adapter that lets `whale/link.py` negotiate and drive it
without knowing any of that: the link deals in `encode` / `decode` /
`airtime`, `chunk_size` and `mode_id`, exactly as it does for an
`afsk.Profile`.

Three things the adapter has to reconcile, because the link's contract grew
around CPFSK and VF3 does not fit all of it:

  - **Framing is VF3's own.** The CPFSK profiles hand their payload to
    `whale/framing.py` for a sync word, a length field and a CRC16.  VF3
    carries its own length, CRC32 and rate-1/2 convolutional code inside the
    payload grid, and its acquisition is the OFDM header, not a PN
    correlation.  So `encode`/`decode` here bypass `framing` entirely; what
    the link puts in is a packet, and the air header it prepends is just the
    first ten bytes of the VF3 payload.

  - **The frame is fixed-length.** A VF3 keying is 6.155 s whatever the
    payload, so `airtime` ignores `payload_len`.  Short packets waste the
    difference, which is why this mode is for DATA only: the control plane
    stays on `afsk.CONTROL_PROFILE`, where a 12-byte ACK costs 12 bytes of
    air.  The link already routes it that way (see `_tx_packet`).

  - **The preamble is native.** VF3 repeats its own sync core for the fixed
    universal head time, so acquisition and AGC see the same waveform power
    on every frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .. import framing
from . import vf3

#: On-air identifier.  0, 1 and 2 are the CPFSK profiles in whale/afsk.py;
#: this must stay stable once anything has shipped with it.
VF3_MODE_ID = 3

#: Largest DATA body one VF3 frame can carry, once the link's air header is
#: taken out of the VF3 payload.
CHUNK_SIZE = vf3.MAX_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES

#: `vf3.ACQUISITION_THRESHOLD` under the name the link reads it by.  The link
#: uses it for two decisions: whether a decode attempt counts as a real sync
#: lock worth waiting on, and whether the buffer is stale enough to prune.
CONFIDENCE_THRESHOLD = vf3.ACQUISITION_THRESHOLD


class Vf3Codec:
    """Bridges the link's codec calls onto `whale.modes.vf3`."""

    tx_sample_rate = vf3.SAMPLE_RATE
    rx_sample_rate = vf3.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Vf3Mode") -> np.ndarray:
        if len(payload) > vf3.MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{vf3.MAX_PAYLOAD_BYTES}")
        return vf3.modulate(bytes(payload))

    def decode(self, audio, mode: "Vf3Mode", **kwargs) -> dict:
        del mode
        return vf3.demodulate(audio, **kwargs)

    def airtime(self, payload_len: int, mode: "Vf3Mode") -> float:
        del payload_len  # a VF3 frame is the same length whatever it carries
        return vf3.frame_seconds()


VF3_CODEC = Vf3Codec()


@dataclass(frozen=True)
class Vf3Mode:
    """One negotiable VF3 setting, shaped like `afsk.Profile`.

    It is a separate type rather than a `Profile` with a different codec
    because `Profile`'s `baud`/`freq0`/`freq1` describe a two-tone CPFSK
    signal and mean nothing here.  What the link actually reads is this
    attribute surface, and `baud` is on it because the link still reports a
    mode's symbol rate in its logs.  VF3's OFDM symbol rate is the honest
    value to put there.
    """

    name: str = "vf3"
    mode_id: int = VF3_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    codec: Vf3Codec = field(default=VF3_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        """VF3's OFDM symbol rate, 41.667 symbol/s."""
        return vf3.SAMPLE_RATE / vf3.SYMBOL_SAMPLES

    def encode(self, payload: bytes):
        return self.codec.encode(payload, self)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


VF3 = Vf3Mode()


def registry_with_vf3(base=None):
    """`base` (default: the CPFSK registry) with VF3 appended as the top rung.

    This is what `whale.modes.default_registry()` returns, and therefore
    what every station runs: VF3 has carried full acceptance-test sessions
    over the air in both directions, at the top of the ladder, with no ARQ
    retries.  It stays a separate function because `base` is a parameter --
    a test or a bench run can compose VF3 onto some other ladder without
    going through the default.
    """
    from .. import afsk
    from ..waveform import ModeRegistry

    base = base if base is not None else afsk.default_registry()
    return ModeRegistry(tuple(base.modes) + (VF3,), base.control)
