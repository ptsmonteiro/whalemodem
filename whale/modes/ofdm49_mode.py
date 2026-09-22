"""Shared adapter machinery for whale.phy.ofdm49's negotiable modes.

hf6-hf9 all wrap one `whale.phy.ofdm49.OFDM49Mode` PHY with the same
codec-call shape: `encode` rejects an oversize payload and hands off to
`modulate()` (optionally padding to a minimum keying length), `decode`
short-circuits non-1-D audio and hands off to `demodulate()` with the
mode's fixed decode options applied as defaults, and `airtime` reports the
PHY's whole keying. `band_hz` and `baud` read off the wrapped PHY, and every
rung on this PHY takes a frequency hint. Each mode module supplies its own
`OFDM49Mode` configuration, `Ofdm49Codec` options, and the handful of facts
(name, mode_id, modulation, fec, ...) that make it that mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from whale.phy import ofdm49

from .. import waveform


class Ofdm49Codec:
    """Bridges a mode's codec calls onto one `whale.phy.ofdm49.OFDM49Mode`."""

    tx_sample_rate = ofdm49.TX_SAMPLE_RATE
    rx_sample_rate = ofdm49.RX_SAMPLE_RATE

    def __init__(self, phy, *, decode_kwargs: dict | None = None,
                 streaming: bool = False, pad_seconds: float | None = None):
        self.phy = phy
        #: The PHY streaming.py's OfdmReceiver can run against, or None.
        self.streaming_phy = phy if streaming else None
        self._decode_kwargs = decode_kwargs or {}
        self._pad_seconds = pad_seconds

    def encode(self, payload: bytes, mode) -> np.ndarray:
        if len(payload) > self.phy.max_payload_bytes:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{self.phy.max_payload_bytes}")
        audio = self.phy.modulate(bytes(payload))
        if self._pad_seconds is not None:
            target = int(round(self._pad_seconds * self.tx_sample_rate))
            audio = np.pad(audio, (0, max(0, target - len(audio))))
        return audio

    def decode(self, audio, mode, **kwargs) -> dict:
        del mode
        if np.asarray(audio).ndim != 1:
            return {"synced": False, "payload": None}
        for key, value in self._decode_kwargs.items():
            kwargs.setdefault(key, value)
        return self.phy.demodulate(np.asarray(audio), **kwargs)

    def airtime(self, payload_len: int, mode) -> float:
        del payload_len, mode
        # Air time is the whole keying: settling head plus frame.
        return max(self._pad_seconds or 0.0, self.phy.keying_seconds())


#: Head-resized PHYs, by (mode name, head length). `dataclasses.replace`
#: on an OFDM49Mode re-runs a `__post_init__` that builds every reference
#: constellation, so a sweep that re-encodes per frame must not pay for it
#: per frame.
_HEAD_VARIANTS: dict = {}


@dataclass(frozen=True)
class Ofdm49Mode(waveform.CodecMode):
    """A negotiable mode wrapping one `whale.phy.ofdm49.OFDM49Mode`.

    `encode`/`decode`/`airtime`/`tx_sample_rate`/`rx_sample_rate` come from
    `CodecMode`; `streaming_phy`, `baud` and `band_hz` here read off the
    wrapped PHY on `self.codec`.
    """

    supports_frequency_hint: bool = field(default=True, init=False, repr=False)

    @property
    def streaming_phy(self):
        return self.codec.streaming_phy

    @property
    def settling_head_variant(self):
        """Resizable settling head; the PHY carries it as `head_seconds`."""
        def variant(head_seconds):
            key = (self.name, head_seconds)
            if key not in _HEAD_VARIANTS:
                _HEAD_VARIANTS[key] = replace(self.codec.phy,
                                              head_seconds=head_seconds)
            phy = _HEAD_VARIANTS[key]
            return phy.modulate, lambda payload_len: phy.keying_seconds()
        return variant

    @property
    def baud(self) -> float:
        return ofdm49.DESIGN_RATE / self.codec.phy.symbol_len

    @property
    def band_hz(self) -> tuple[float, float]:
        """Lowest to highest carrier/tone centre, in Hz."""
        return self.codec.phy.band_hz
