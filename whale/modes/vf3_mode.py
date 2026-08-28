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

  - **The frame is fixed-length.** A VF3 keying is 5.2 s whatever the
    payload, so `airtime` ignores `payload_len`.  Short packets waste the
    difference, which is why this mode is for DATA only: the control plane
    stays on `afsk.CONTROL_PROFILE`, where a 12-byte ACK costs 12 bytes of
    air.  The link already routes it that way (see `_tx_packet`).

  - **Head feedback is not wired up.** VF3 measures its received head (see
    `vf3._measure_head`) and this adapter reports it as `head_cores_observed`
    for the logs, but deliberately *not* as `head_symbols_received`.  The
    link's `_head_feedback_request` compares an observation against
    `afsk.PAD_MATCH_WINDOW_SYMBOLS / baud`, a CPFSK constant in CPFSK bit
    units; against VF3's 21.3 ms head cores that allowance becomes 341 ms and
    would swallow every real deficit.  Reporting nothing makes the link take
    the "missing observation" branch and leave the head duration where the
    control-plane calibration put it, which is correct and conservative.
    Making that feedback loop mode-independent is its own change.
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

    sample_rate = vf3.SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Vf3Mode", *, include_head=True,
               head_seconds=vf3.DEFAULT_HEAD_SECONDS) -> np.ndarray:
        if len(payload) > vf3.MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{vf3.MAX_PAYLOAD_BYTES}")
        # include_head=False means "no adaptive guard", not "no lead-in":
        # vf3.lead_in_samples floors at the 45 ms that ramps the transmitter
        # and the sound card up, and a VF3 frame has never been sent without
        # it.  There is nothing to gain by trying -- the floor is 45 ms of a
        # 5.2 s keying.
        if not include_head:
            head_seconds = vf3.DEFAULT_HEAD_SECONDS
        return vf3.modulate(bytes(payload), head_seconds=head_seconds)

    def decode(self, audio, mode: "Vf3Mode", *,
               head_seconds=vf3.DEFAULT_HEAD_SECONDS, **kwargs) -> dict:
        result = vf3.demodulate(audio, head_seconds=head_seconds, **kwargs)
        observed = result.pop("head_cores_received", None)
        if observed is not None:
            # Diagnostic only -- see this module's docstring for why this is
            # not returned as head_symbols_received.
            result["head_cores_observed"] = observed
        return result

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
    attribute surface, and `baud` is on it for one reason: the head-feedback
    logging converts symbol counts to milliseconds with it.  VF3's symbol
    rate is the honest value to put there.
    """

    name: str = "vf3"
    mode_id: int = VF3_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    codec: Vf3Codec = field(default=VF3_CODEC, compare=False, repr=False)

    @property
    def sample_rate(self) -> int:
        return self.codec.sample_rate

    @property
    def baud(self) -> float:
        """VF3's OFDM symbol rate, 41.667 symbol/s."""
        return vf3.SAMPLE_RATE / vf3.SYMBOL_SAMPLES

    def encode(self, payload: bytes, *, include_head=True,
               head_seconds=vf3.DEFAULT_HEAD_SECONDS):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

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
