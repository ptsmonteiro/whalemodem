"""HF3: the HF4 waveform with an exact rate-2/3 inner FEC code.

Developed in the retired `experiments/hf3_fec34/` and moved here unmodified
when it became shipped product code; that directory's `README.md` records the
candidate and its controlled-channel screen.  (The earlier, unrelated
36-carrier OFDM HF3 in `experiments/hf3/` is historical material and is not
this waveform.)
"""

from __future__ import annotations

import threading

import numpy as np

from whale.dsp import interleave as _interleave
from whale.phy import hf4

FEC_K = 2
FEC_N = 3
FEC_RATE = FEC_K / FEC_N
FEC_CODE = hf4.FEC_CODE
FEC_TAIL_BITS = hf4.FEC_TAIL_BITS
SAMPLE_RATE = hf4.SAMPLE_RATE
RX_SAMPLE_RATE = hf4.RX_SAMPLE_RATE
CARRIER_COUNT = hf4.CARRIER_COUNT
# Shorter than HF4's maximum-speed frame so Doppler does not evolve across an
# entire codeword.
DATA_SYMBOLS = 36
SYMBOL_SAMPLES = hf4.SYMBOL_SAMPLES
DIVERSITY_COLUMNS = (hf4.CARRIER_COUNT // 2) * hf4.BITS_PER_CARRIER
DIVERSITY_SECOND_OFFSET = (hf4.CARRIER_COUNT // 2 + 1) * hf4.BITS_PER_CARRIER
ACQUISITION_THRESHOLD = hf4.ACQUISITION_THRESHOLD
DEFAULT_HEAD_SECONDS = hf4.DEFAULT_HEAD_SECONDS

# The HF4 pilot spacing is aimed at benign/static fading. HF3 uses a denser
# comb while testing time-varying Watterson tracking.
PILOT_PERIOD = 6
PILOT_SYMBOLS = DATA_SYMBOLS // PILOT_PERIOD
PAYLOAD_SYMBOLS = DATA_SYMBOLS + PILOT_SYMBOLS
TOTAL_SYMBOLS = hf4.SYNC_SYMBOLS + hf4.HEADER_SYMBOLS + PAYLOAD_SYMBOLS
FRAME_SECONDS = (hf4.LEAD_SAMPLES + TOTAL_SYMBOLS * SYMBOL_SAMPLES
                 + 2 * hf4.EDGE_WINDOW_SAMPLES + hf4.TAIL_SAMPLES) / RX_SAMPLE_RATE


def _payload_layout() -> list[tuple[str, int | None]]:
    layout = []
    data_index = 0
    for index in range(DATA_SYMBOLS):
        layout.append(("data", data_index))
        data_index += 1
        if (index + 1) % PILOT_PERIOD == 0:
            layout.append(("pilot", None))
    return layout


PAYLOAD_LAYOUT = _payload_layout()
PILOT_INDICES = np.array(
    [index for index, (kind, _) in enumerate(PAYLOAD_LAYOUT)
     if kind == "pilot"], dtype=np.int64)
DATA_ROWS = np.array(
    [index for index, (kind, _) in enumerate(PAYLOAD_LAYOUT)
     if kind == "data"], dtype=np.int64)

_SIZES = hf4._derive_fec_sizes(
    DATA_SYMBOLS, DIVERSITY_COLUMNS // hf4.BITS_PER_CARRIER,
    hf4.BITS_PER_CARRIER,
    FEC_K, FEC_N, FEC_TAIL_BITS, hf4.LENGTH_BYTES, hf4.CRC_BYTES)
RAW_BITS = _SIZES["raw_bits"]
FEC_PERIODS = _SIZES["periods"]
ENCODER_INPUT_BITS = _SIZES["encoder_input_bits"]
MESSAGE_CAPACITY_BITS = _SIZES["message_capacity_bits"]
PACKET_BYTES = _SIZES["packet_bytes"]
PAD_BITS = _SIZES["pad_bits"]
MAX_PAYLOAD_BYTES = _SIZES["max_payload_bytes"]
PUNCTURE_KEEP = hf4._puncture_keep_indices(FEC_K, FEC_N)
PUNCTURE_MOTHER_BITS = 2 * FEC_K
INTERLEAVER = _interleave.multiplicative(RAW_BITS, hf4._INTERLEAVE_STRIDE)
_WHITENER = hf4.pn_bits(PACKET_BYTES * 8, hf4._WHITENER_SEED)

_LOCK = threading.RLock()
_OVERRIDES = {
    "FEC_K": FEC_K, "FEC_N": FEC_N, "PUNCTURE_KEEP": PUNCTURE_KEEP,
    "PUNCTURE_MOTHER_BITS": PUNCTURE_MOTHER_BITS, "RAW_BITS": RAW_BITS,
    "FEC_PERIODS": FEC_PERIODS, "ENCODER_INPUT_BITS": ENCODER_INPUT_BITS,
    "MESSAGE_CAPACITY_BITS": MESSAGE_CAPACITY_BITS, "PACKET_BYTES": PACKET_BYTES,
    "PAD_BITS": PAD_BITS, "MAX_PAYLOAD_BYTES": MAX_PAYLOAD_BYTES,
    "INTERLEAVER": INTERLEAVER, "_WHITENER": _WHITENER,
    "PILOT_PERIOD": PILOT_PERIOD, "PILOT_SYMBOLS": PILOT_SYMBOLS,
    "PAYLOAD_SYMBOLS": PAYLOAD_SYMBOLS, "TOTAL_SYMBOLS": TOTAL_SYMBOLS,
    "PAYLOAD_LAYOUT": PAYLOAD_LAYOUT, "PILOT_INDICES": PILOT_INDICES,
    "DATA_ROWS": DATA_ROWS, "DATA_SYMBOLS": DATA_SYMBOLS,
    "TRACK_PILOT_AMPLITUDE": True,
    "PILOT_CHANNEL_SMOOTHING": True,
    "CARRIER_WEIGHT_LOW": 0.0,
    "CARRIER_WEIGHT_HIGH": 4.0,
    "CARRIER_ERASURE_SNR_DB": 10.0,
    "FREQUENCY_DIVERSITY": True,
    "DIVERSITY_COLUMNS": DIVERSITY_COLUMNS,
    "DIVERSITY_SECOND_OFFSET": DIVERSITY_SECOND_OFFSET,
    "SAMPLE_CLOCK_TRACKING": True,
}


def _run(function, *args, **kwargs):
    """Run one HF4 operation against the HF3 coding constants."""
    with _LOCK:
        old = {name: getattr(hf4, name) for name in _OVERRIDES}
        try:
            for name, value in _OVERRIDES.items():
                setattr(hf4, name, value)
            return function(*args, **kwargs)
        finally:
            for name, value in old.items():
                setattr(hf4, name, value)


def modulate(payload: bytes) -> np.ndarray:
    return _run(hf4.modulate, payload)


def demodulate(audio: np.ndarray, **kwargs) -> dict:
    return _run(hf4.demodulate, audio, **kwargs)


def airtime(payload_len: int | None = None) -> float:
    del payload_len
    return FRAME_SECONDS


def lead_in_samples() -> int:
    return hf4.lead_in_samples()
