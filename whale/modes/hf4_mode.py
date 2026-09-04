"""HF4 as a negotiable, experimental `WaveformMode`.

HF4 is a from-scratch single-carrier audio-passband data mode (8PSK @ 1500
baud, no FEC, sparse mid-frame BPSK pilots for phase-drift tracking) for the
IC-7300 -> IC-705 audio-coupled HF SSB path. It wires
`experiments/hf13_fast_sync_v1/sc_fast.SingleCarrierMode` -- a drop-in,
~4.8x-cheaper-to-decode replacement for
`experiments/hf5_8psk_4k/sc.SingleCarrierMode`'s already real-hardware
qualified operating point (8PSK@1500baud, packet_bytes=2994,
pilot_interval=150, ~4049 bps net) -- into the link's `WaveformMode`
contract, mirroring `whale/modes/hf3_mode.py`'s pattern; it does not
re-derive or re-run any of hf5's or hf13's own design or evidence. See
`experiments/hf5_8psk_4k/RESULTS.md` for the original PHY's qualification
record and `experiments/hf13_fast_sync_v1/RESULTS.md` for the fused-FFT
sync search's real-hardware equivalence evidence (0/10 discrepancies vs.
the original sync search).

HF4 is registered as EXPERIMENTAL only (see `whale/mode_qualification.py`),
the same disposition as HF3 -- it is not a default or optional mode on any
ladder, and remains available at the experimental registry level for a
station that opts in.

Unlike HF2/HF3's OFDM waveforms, HF4's single-carrier preamble is a fixed,
self-contained acquisition sequence (a 63-chip BPSK PN preamble, joint
time/frequency-offset matched-filter search) -- there is no adaptive
`whale/modes/hf_lead.py` head to negotiate, so `encode`/`decode` accept and
ignore `include_head`/`head_seconds` exactly like `whale/modes/hf4_mode.py`
did before this promotion (the field is part of the `WaveformMode`
contract's signature, not something every mode must use).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from experiments.hf13_fast_sync_v1 import sc_fast

from .. import framing

#: On-air identifier. 0, 1, 2 are the CPFSK profiles; 3 is VF3; 4 is HC1;
#: 5 is HC0; 6 is VF6; 7 is HF2; 8 is VF4; 9 is HF3; 10 is HR0 -- this must
#: stay stable once anything has shipped with it, and must not collide with
#: any of those. The number is global across channels even though nothing
#: currently offers HF4 alongside another mode -- a mode id names one
#: waveform, everywhere.
HF4_MODE_ID = 11

#: HF13's real-hardware qualified operating point (see
#: experiments/hf13_fast_sync_v1/hardware_test.py's defaults and
#: experiments/hf5_8psk_4k/RESULTS.md step 28): 8PSK @ 1500 baud, no FEC,
#: a mid-frame BPSK pilot block every 150 data symbols.
BAUD = 1500.0
BITS_PER_SYMBOL = 3
PACKET_BYTES = 2994
PILOT_INTERVAL = 150

#: The single, shared PHY instance every encode/decode call runs against.
#: `sc_fast.SingleCarrierMode` is a frozen dataclass with no per-call
#: mutable state, so one instance is safe to reuse across frames.
HF4_PHY = sc_fast.SingleCarrierMode(baud=BAUD, bits_per_symbol=BITS_PER_SYMBOL,
                                    packet_bytes=PACKET_BYTES,
                                    pilot_interval=PILOT_INTERVAL)

#: Largest DATA body one HF4 frame can carry, once the link's air header is
#: taken out of the HF4 payload.
CHUNK_SIZE = HF4_PHY.max_payload_bytes - framing.AIR_HEADER_BYTES

#: `sc.py`/`sc_fast.py` gate acquisition on `confidence >= 0.12` inline in
#: `demodulate()` (there is no named constant upstream to import); this is
#: that same threshold under the name the link reads it by.
CONFIDENCE_THRESHOLD = 0.12


class Hf4Codec:
    """Bridges the link's codec calls onto `experiments.hf13_fast_sync_v1.sc_fast`."""

    tx_sample_rate = sc_fast.TX_SAMPLE_RATE
    rx_sample_rate = sc_fast.RX_SAMPLE_RATE

    def encode(self, payload: bytes, mode: "Hf4Mode", *, include_head=True,
               head_seconds=None) -> np.ndarray:
        del include_head, head_seconds  # HF4's preamble is fixed; see module docstring
        if len(payload) > HF4_PHY.max_payload_bytes:
            raise ValueError(
                f"packet is {len(payload)} bytes; {mode.name} carries at most "
                f"{HF4_PHY.max_payload_bytes}")
        return HF4_PHY.modulate(bytes(payload))

    def decode(self, audio, mode: "Hf4Mode", *, head_seconds=None, **kwargs) -> dict:
        del mode, head_seconds, kwargs
        return HF4_PHY.demodulate(audio)

    def airtime(self, payload_len: int, mode: "Hf4Mode") -> float:
        del payload_len, mode  # an HF4 frame is the same length whatever it carries
        return HF4_PHY.frame_seconds()


HF4_CODEC = Hf4Codec()


@dataclass(frozen=True)
class Hf4Mode:
    """One negotiable HF4 setting, shaped like `hf3_mode.Hf3Mode`."""

    name: str = "hf4"
    mode_id: int = HF4_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    codec: Hf4Codec = field(default=HF4_CODEC, compare=False, repr=False)

    @property
    def tx_sample_rate(self) -> int:
        return self.codec.tx_sample_rate

    @property
    def rx_sample_rate(self) -> int:
        return self.codec.rx_sample_rate

    @property
    def baud(self) -> float:
        """HF4's single-carrier symbol rate."""
        return HF4_PHY.baud

    def encode(self, payload: bytes, *, include_head=True, head_seconds=None):
        return self.codec.encode(payload, self, include_head=include_head,
                                 head_seconds=head_seconds)

    def decode(self, audio, **kwargs):
        return self.codec.decode(audio, self, **kwargs)

    def airtime(self, payload_len: int) -> float:
        return self.codec.airtime(payload_len, self)


HF4 = Hf4Mode()
