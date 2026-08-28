"""VF3: the 58-carrier, full-1024-core counterpart to VF2.

VF3 keeps VF2's 48 kHz / 24 ms / 214-symbol frame, coherent QPSK header,
interleaved rate-1/2 convolutional coding, CRC32 and acquisition
validation.  It spends the complete 1024-sample modulation interval on new
information instead of repeating a 512-sample core:

    [128 cyclic prefix][1024 OFDM core] = 1152 samples

That halves carrier spacing to 46.875 Hz and fits 58 carriers in
essentially the same audio band.  The deliberate cost is the loss of VF2's
repeated-core combining gain and 640-sample placement window; a
differentially encoded payload buys back the phase robustness that cost.

This module is geometry and wiring.  Every kernel it calls -- the OFDM
transforms, acquisition, timing, the channel fit, the Viterbi decoder, the
payload codec -- lives in `whale/dsp/`, parameterized rather than
hardcoded.  What is left here is what is genuinely VF3's: which bins carry
data, how long the header is, and the order the stages run in.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import hilbert

from .. import dsp
from ..dsp import (acquire as _acquire_kernel, differential as _diff,
                   equalize as _eq, freq as _freq, head as _head,
                   ofdm as _ofdm, timing as _timing)

# -- geometry -------------------------------------------------------------

SAMPLE_RATE = 48_000
CORE_SAMPLES = 1_024
GUARD_SAMPLES = 128
SYMBOL_SAMPLES = GUARD_SAMPLES + CORE_SAMPLES

CARRIER_BINS = np.arange(10, 68, dtype=np.int32)
CARRIER_SPACING_HZ = SAMPLE_RATE / CORE_SAMPLES
CARRIER_HZ = CARRIER_BINS.astype(np.float64) * CARRIER_SPACING_HZ
N_CARRIERS = len(CARRIER_BINS)

HEADER_SYMBOLS = 15
SYNC_SYMBOLS = 5
PAYLOAD_SYMBOLS = 199
TOTAL_SYMBOLS = HEADER_SYMBOLS + PAYLOAD_SYMBOLS
BITS_PER_SYMBOL = 2 * N_CARRIERS
PAYLOAD_BITS = PAYLOAD_SYMBOLS * BITS_PER_SYMBOL

TX_RMS = 0.13
MAX_SAMPLE = 0.95

GEOMETRY = _ofdm.Geometry(
    sample_rate=SAMPLE_RATE, core_samples=CORE_SAMPLES,
    guard_samples=GUARD_SAMPLES, carrier_bins=CARRIER_BINS,
).scaled_to_rms(TX_RMS)

LEAD_IN_SAMPLES = 2_160
LEAD_IN_FADE_SAMPLES = 240
TAIL_SAMPLES = 912
FRAME_SAMPLES = LEAD_IN_SAMPLES + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

FFT_OFFSET = GUARD_SAMPLES
ACQUISITION_THRESHOLD = 0.70
MIN_PRESENT_CARRIERS = 40
CARRIER_FLOOR_DB = 35.0

HEAD_MATCH_THRESHOLD = _head.MATCH_THRESHOLD
HEAD_MIN_ENERGY_FRACTION = _head.MIN_ENERGY_FRACTION

# -- the adaptive head ----------------------------------------------------
#
# The shipped link asks each mode for a leading guard of `head_seconds`,
# negotiated per direction at connect time and adjusted during transfer, to
# cover the receiver's squelch blackout (see whale/framing.py's
# HEAD_PAD_SECONDS).  VF3 already had exactly that in miniature: a 45 ms
# lead-in of the sync symbol's *core*, ramped up over 5 ms.  Lengthening
# that lead-in is the whole implementation.
#
# It is deliberately core-periodic (1024 samples) rather than
# symbol-periodic (1152).  Acquisition locks by correlating the signal
# against itself one whole symbol apart, so a head built from repeated
# *symbols* would extend that correlation's plateau across the entire head,
# collapse it into one contiguous proposal group, and leave the candidate
# ranking a single arbitrary offset inside the plateau to rank.  A
# core-periodic head is not autocorrelated at lag 1152 at all, so the
# acquisition peak stays where the real header is.
DEFAULT_HEAD_SECONDS = LEAD_IN_SAMPLES / SAMPLE_RATE


def lead_in_samples(head_seconds: float = DEFAULT_HEAD_SECONDS) -> int:
    """Leading sync-core samples for a requested head duration.

    The original 45 ms is the floor: it is what ramps the transmitter and
    the sound card up, and a head shorter than that has never been sent.
    """
    if head_seconds < 0:
        raise ValueError("head duration must not be negative")
    return max(LEAD_IN_SAMPLES, int(round(head_seconds * SAMPLE_RATE)))


def frame_samples(head_seconds: float = DEFAULT_HEAD_SECONDS) -> int:
    return (lead_in_samples(head_seconds)
            + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES)


def frame_seconds(head_seconds: float = DEFAULT_HEAD_SECONDS) -> float:
    return frame_samples(head_seconds) / SAMPLE_RATE


# -- reference constellations and the payload codec -----------------------

SYNC_VALUES = dsp.bits.qpsk_from_bits(
    dsp.bits.pn_bits(BITS_PER_SYMBOL, 0x0F35B))
HEADER_VALUES = np.vstack((
    np.tile(SYNC_VALUES, (SYNC_SYMBOLS, 1)),
    dsp.bits.qpsk_from_bits(
        dsp.bits.pn_bits((HEADER_SYMBOLS - SYNC_SYMBOLS) * BITS_PER_SYMBOL,
                         0x1B4C3)).reshape(HEADER_SYMBOLS - SYNC_SYMBOLS,
                                           N_CARRIERS),
))

CODEC = dsp.PacketCodec(
    payload_bits=PAYLOAD_BITS,
    interleaver=dsp.interleave.multiplicative(PAYLOAD_BITS, 8101),
    whitener_seed=0x17E35,
    code=dsp.K7,
)

FEC_INPUT_BITS = CODEC.information_bits
FEC_TAIL_BITS = CODEC.code.tail_bits
PACKET_BYTES = CODEC.packet_bytes
PACKET_BITS = PACKET_BYTES * 8
UNUSED_INFO_BITS = CODEC.unused_information_bits
MAX_PAYLOAD_BYTES = CODEC.max_payload_bytes

encode_payload_bits = CODEC.encode
decode_payload_bits = CODEC.decode_hard
decode_payload_soft = CODEC.decode_soft

# Kernel bindings kept under VF3's historical names, for the diagnostics
# and tests that reach for them directly.
pn_bits = dsp.bits.pn_bits
qpsk_from_bits = dsp.bits.qpsk_from_bits
bits_from_qpsk = dsp.bits.bits_from_qpsk
slice_qpsk = dsp.bits.slice_qpsk
convolutional_encode = dsp.K7.encode
convolutional_decode = dsp.K7.decode_hard
convolutional_decode_soft = dsp.K7.decode_soft
differential_bits = _diff.hard_bits
differential_observations = _diff.observations


def differential_encode(bits: np.ndarray, initial: np.ndarray) -> np.ndarray:
    return _diff.encode(bits, initial, PAYLOAD_SYMBOLS, N_CARRIERS)


def differential_soft_bits(values: np.ndarray,
                           carrier_weights: np.ndarray) -> np.ndarray:
    return _diff.soft_bits(values, carrier_weights)


# -- modulation -----------------------------------------------------------

def build_symbol(values: np.ndarray) -> np.ndarray:
    return _ofdm.build_symbol(GEOMETRY, values)


def symbol_carriers(symbol_audio: np.ndarray,
                    offset: int = FFT_OFFSET) -> np.ndarray:
    return _ofdm.symbol_carriers(GEOMETRY, symbol_audio, offset)


def sync_core() -> np.ndarray:
    """The 1024-sample periodic waveform the head and sync symbols share."""
    return build_symbol(SYNC_VALUES)[GUARD_SAMPLES:]


def frame_constellation(payload: bytes) -> np.ndarray:
    payload_values = differential_encode(
        encode_payload_bits(payload), HEADER_VALUES[-1])
    return np.vstack((HEADER_VALUES, payload_values))


def modulate(payload: bytes, *,
             head_seconds: float = DEFAULT_HEAD_SECONDS) -> np.ndarray:
    values = frame_constellation(payload)
    symbols = np.concatenate([build_symbol(row) for row in values])
    lead = np.resize(sync_core(), lead_in_samples(head_seconds)).copy()
    fade = LEAD_IN_FADE_SAMPLES
    lead[:fade] *= np.linspace(0.0, 1.0, fade, endpoint=True)
    audio = np.concatenate((lead, symbols, np.zeros(TAIL_SAMPLES)))
    if len(audio) != frame_samples(head_seconds):
        raise AssertionError(f"internal frame length error: {len(audio)}")
    peak = float(np.max(np.abs(audio)))
    if peak > MAX_SAMPLE:
        audio *= MAX_SAMPLE / peak
    return audio.astype(np.float32)


# -- demodulation ---------------------------------------------------------

_TIMING_SYMBOLS = np.arange(SYNC_SYMBOLS, TOTAL_SYMBOLS, dtype=np.int32)


def _header_bank(analytic: np.ndarray, start: int) -> np.ndarray | None:
    return _ofdm.carrier_bank(GEOMETRY, analytic, start, HEADER_SYMBOLS,
                              offset=FFT_OFFSET)


def _header_candidate_snr(analytic: np.ndarray, start: int) -> float:
    """Acquisition's scorer: how well a candidate start fits the header."""
    observed = _header_bank(analytic, start)
    if observed is None:
        return -np.inf
    return _eq.header_snr(observed, HEADER_VALUES)


def _acquire(analytic: np.ndarray) -> tuple[int | None, float]:
    return _acquire_kernel.acquire(
        GEOMETRY, analytic, sync_symbols=SYNC_SYMBOLS,
        rank=lambda start: _header_candidate_snr(analytic, start))


def _estimate_timing(analytic: np.ndarray,
                     start: int) -> tuple[float, float, float]:
    fit = _timing.estimate(GEOMETRY, analytic, start, _TIMING_SYMBOLS)
    return fit.intercept, fit.slope, fit.confidence


def _measure_head(samples: np.ndarray, start: int) -> tuple[int, float]:
    """How much of the transmitted head survived, in whole 1024-sample cores.

    Nothing in the decode path uses this; see `vf3_mode` for how the link
    turns it into head feedback.
    """
    return _head.measure(samples, start, sync_core())


def _base_result() -> dict:
    return {
        "synced": False, "payload": None, "confidence": 0.0,
        "start_index": None, "cfo_hz": 0.0, "clock_offset_ppm": 0.0,
        "carrier_snr_db": np.full(N_CARRIERS, -np.inf),
        "symbol_evm_db": np.full(TOTAL_SYMBOLS, np.inf),
        "phase_track": np.zeros(TOTAL_SYMBOLS), "raw_payload_bits": None,
    }


def demodulate(audio: np.ndarray, *,
               head_seconds: float = DEFAULT_HEAD_SECONDS) -> dict:
    """Decode one VF3 frame out of `audio`.

    `head_seconds` is accepted for symmetry with `modulate` and with the
    `WaveformMode` contract; acquisition does not need to be told how long
    the head was, since it locks on the header rather than on the head.

    Alongside VF3's own diagnostics the result carries the three keys the
    link's receive loop reads: `confidence`, `sync_end_index` and
    `end_index`.  `end_index` is present only once the frame has been seen
    through to its end -- its absence, with confidence above threshold, is
    how the caller is told to wait for more audio rather than consume what
    it has.  See whale/link.py's _decode_one.
    """
    del head_seconds  # acquisition finds the header wherever the head ended
    result = _base_result()
    samples = np.asarray(audio, dtype=np.float64).reshape(-1)
    if len(samples) < HEADER_SYMBOLS * SYMBOL_SAMPLES:
        result["failure"] = "capture shorter than header"
        return result

    analytic = hilbert(samples)
    start, confidence = _acquire(analytic)
    result.update(confidence=confidence, start_index=start)
    if start is None or confidence < ACQUISITION_THRESHOLD:
        result["failure"] = "header not found"
        return result
    result["sync_end_index"] = start + HEADER_SYMBOLS * SYMBOL_SAMPLES

    head_cores, head_score = _measure_head(samples, start)
    result["head_cores_received"] = head_cores
    result["head_match"] = head_score

    fit = _timing.estimate(GEOMETRY, analytic, start, _TIMING_SYMBOLS)
    result["timing_drift_samples"] = fit.drift_samples(TOTAL_SYMBOLS)
    result["timing_confidence"] = fit.confidence

    carriers = _ofdm.carrier_bank(GEOMETRY, analytic, start, TOTAL_SYMBOLS,
                                  fit.intercept, fit.slope, FFT_OFFSET)
    if carriers is None:
        # Still arriving.  Deliberately no end_index: the caller must keep
        # this audio and try again rather than consume a partial frame.
        result["failure"] = "frame truncated"
        return result
    result["end_index"] = min(
        len(samples),
        start + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES)

    channel = _eq.fit_header(carriers[:HEADER_SYMBOLS], HEADER_VALUES)
    present = channel.present_carriers(CARRIER_FLOOR_DB)
    result["present_carriers"] = present
    if present < MIN_PRESENT_CARRIERS:
        result["failure"] = f"header has only {present}/{N_CARRIERS} carriers"
        return result
    result["carrier_snr_db"] = channel.snr_db

    equalised = channel.equalize(carriers)
    differential = _diff.observations(equalised[HEADER_SYMBOLS:],
                                      equalised[HEADER_SYMBOLS - 1])
    corrected = equalised.copy()
    corrected[HEADER_SYMBOLS:] = differential
    result["phase_track"] = np.zeros(TOTAL_SYMBOLS)

    evm = np.empty(TOTAL_SYMBOLS)
    evm[:HEADER_SYMBOLS] = np.sqrt(np.mean(
        np.abs(corrected[:HEADER_SYMBOLS] - HEADER_VALUES) ** 2, axis=1))
    evm[HEADER_SYMBOLS:] = np.sqrt(np.mean(
        np.abs(differential - _diff.decisions(differential)) ** 2, axis=1))
    result["symbol_evm_db"] = 20.0 * np.log10(np.maximum(evm, 1e-15))

    result["raw_payload_bits"] = _diff.hard_bits(differential)
    soft_bits = _diff.soft_bits(differential,
                                _eq.carrier_weights(channel.snr_db))
    payload, meta = CODEC.decode_soft(soft_bits)
    result.update(meta)
    result.update(payload=payload, synced=True, channel=channel.gain,
                  interference=channel.offset, constellation=corrected,
                  soft_payload_bits=soft_bits)
    result["clock_offset_ppm"] = fit.clock_offset_ppm(GEOMETRY)
    # Diagnostic only.  A differential payload behind a per-carrier
    # equalizer is already indifferent to a static offset, so nothing above
    # corrects with this; it is here because it is worth seeing.
    result["cfo_hz"] = _freq.coarse_offset_hz(
        GEOMETRY, analytic, start, _TIMING_SYMBOLS,
        np.array([fit.shift_at(int(i)) for i in _TIMING_SYMBOLS]))
    return result


def demodulate_debug(audio: np.ndarray, reference_payload: bytes | None = None,
                     *, head_seconds: float = DEFAULT_HEAD_SECONDS) -> dict:
    result = demodulate(audio, head_seconds=head_seconds)
    if reference_payload is None or result.get("raw_payload_bits") is None:
        return result
    expected = encode_payload_bits(reference_payload)
    errors = result["raw_payload_bits"] != expected
    grid = errors.reshape(PAYLOAD_SYMBOLS, N_CARRIERS, 2)
    result["total_bit_errors"] = int(np.count_nonzero(errors))
    result["carrier_bit_errors"] = np.sum(grid, axis=(0, 2)).astype(int)
    symbol_errors = np.zeros(TOTAL_SYMBOLS, dtype=int)
    symbol_errors[HEADER_SYMBOLS:] = np.sum(grid, axis=(1, 2))
    result["symbol_bit_errors"] = symbol_errors
    result["ber"] = float(np.mean(errors))
    return result


@dataclass(frozen=True)
class FrameInfo:
    sample_rate: int = SAMPLE_RATE
    carrier_count: int = N_CARRIERS
    header_symbols: int = HEADER_SYMBOLS
    payload_symbols: int = PAYLOAD_SYMBOLS
    frame_samples: int = FRAME_SAMPLES
    max_payload_bytes: int = MAX_PAYLOAD_BYTES

    @property
    def frame_seconds(self) -> float:
        return self.frame_samples / self.sample_rate


INFO = FrameInfo()


def describe() -> str:
    return (f"vf3: {N_CARRIERS}x QPSK carriers {CARRIER_HZ[0]:.2f}-"
            f"{CARRIER_HZ[-1]:.2f} Hz, {TOTAL_SYMBOLS} symbols, "
            f"{MAX_PAYLOAD_BYTES} B + CRC32 in {FRAME_SECONDS:.3f} s")


def _check_constants() -> None:
    assert CORE_SAMPLES == 1024 and GUARD_SAMPLES == 128
    assert SYMBOL_SAMPLES == 1152
    assert CARRIER_SPACING_HZ == 46.875
    assert N_CARRIERS == 58
    assert CARRIER_HZ[0] == 468.75 and CARRIER_HZ[-1] == 3140.625
    assert TOTAL_SYMBOLS == 214 and PAYLOAD_BITS == 23_084
    assert FEC_INPUT_BITS == 11_542
    assert FRAME_SAMPLES == 249_600 and FRAME_SECONDS == 5.2
    assert PACKET_BYTES == 1_442 and UNUSED_INFO_BITS == 0
    assert MAX_PAYLOAD_BYTES == 1_436
    assert CODEC.interleaver.is_valid()


_check_constants()
