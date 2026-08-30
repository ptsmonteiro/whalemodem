"""HC2 wrapped as the minimal object `whale.qualification.run_frame_trial`
and `scripts/benchmark_simulated_channels.py`'s summarizer expect.

Kept separate from `hc2.py` (the waveform) the way `whale/modes/hc1_mode.py`
is kept separate from `whale/modes/hc1.py`: this is wiring for the trial
runner, not part of the waveform definition.  HC2 is not registered in
`whale/mode_qualification.py`'s MANIFEST or in any `ModeRegistry` -- it has
no on-air mode ID and cannot be selected by a real link -- so this exists
only to let `experiments/hc2/benchmark_hc2.py` drive it through the same
Monte Carlo trial machinery HC0/HC1 were qualified with.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

import hc2

#: Not a shipped protocol identifier -- HC2 has none.  Only used as a
#: dict/seed key inside this experiment's own trial runs; kept well above
#: the real MANIFEST's allocated IDs (0-5) and non-negative because
#: `whale.qualification.trial_seed` feeds it straight into
#: `numpy.random.SeedSequence`, which rejects negative integers.
HC2_EXPERIMENTAL_MODE_ID = 9999


class Hc2Codec:
    tx_sample_rate = hc2.SAMPLE_RATE
    rx_sample_rate = hc2.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hc2Mode", *, include_head=True,
               head_seconds=hc2.DEFAULT_HEAD_SECONDS) -> np.ndarray:
        if len(payload) > hc2.MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at "
                f"most {hc2.MAX_PAYLOAD_BYTES}")
        if not include_head:
            head_seconds = hc2.DEFAULT_HEAD_SECONDS
        return hc2.modulate(bytes(payload), head_seconds=head_seconds)

    def decode(self, audio, mode: "Hc2Mode", *,
               head_seconds=hc2.DEFAULT_HEAD_SECONDS, **kwargs) -> dict:
        return hc2.demodulate(audio, head_seconds=head_seconds, **kwargs)

    def airtime(self, payload_len: int, mode: "Hc2Mode") -> float:
        del payload_len
        return hc2.frame_seconds()


HC2_CODEC = Hc2Codec()


@dataclass(frozen=True)
class Hc2Mode:
    name: str = "hc2"
    mode_id: int = HC2_EXPERIMENTAL_MODE_ID
    chunk_size: int = hc2.MAX_PAYLOAD_BYTES - 10  # matches AIR_HEADER_BYTES
    confidence_threshold: float = hc2.ACQUISITION_THRESHOLD
    codec: Hc2Codec = HC2_CODEC

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        return hc2.SAMPLE_RATE / hc2.SYMBOL_SAMPLES

    def encode(self, payload: bytes, *, include_head=True,
               head_seconds=hc2.DEFAULT_HEAD_SECONDS):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HC2 = Hc2Mode()
