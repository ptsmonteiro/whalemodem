"""HC1W: 23-carrier differential-QPSK OFDM for HF SSB.

The 468.75-2531.25 Hz carrier plan uses 93.75 Hz spacing, a 2.67 ms cyclic
prefix, a 5.895 s frame, and terminated rate-1/2 K=9 coding.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import hilbert

from .. import dsp, framing, rx_audio
from ..dsp import (acquire as _acquire_kernel, differential as _diff,
                   equalize as _eq, freq as _freq,
                   ofdm as _ofdm, timing as _timing)

# -- geometry -------------------------------------------------------------

SAMPLE_RATE = 48_000
CORE_SAMPLES = 512
GUARD_SAMPLES = 128
SYMBOL_SAMPLES = GUARD_SAMPLES + CORE_SAMPLES
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE
RX_CORE_SAMPLES = CORE_SAMPLES // rx_audio.DECIMATION
RX_GUARD_SAMPLES = GUARD_SAMPLES // rx_audio.DECIMATION
RX_SYMBOL_SAMPLES = RX_GUARD_SAMPLES + RX_CORE_SAMPLES

#: 23 carriers at 93.75 Hz, spanning 468.75-2531.25 Hz.
CARRIER_BINS = np.arange(5, 28, dtype=np.int32)
CARRIER_SPACING_HZ = SAMPLE_RATE / CORE_SAMPLES
CARRIER_HZ = CARRIER_BINS.astype(np.float64) * CARRIER_SPACING_HZ
N_CARRIERS = len(CARRIER_BINS)

#: 5 identical sync symbols -- what acquisition correlates against itself --
#: then 8 varying training symbols.  The training block does three jobs at
#: once: it ranks acquisition candidates, it is the known reference the fine
#: frequency estimate measures a phase step across, and it is the
#: least-squares fit that gives every carrier its gain and its SNR weight.
#: Eight is what makes the second of those precise enough to leave a
#: residual the differential payload does not notice; see FINE_OFFSET_NOTE.
SYNC_SYMBOLS = 5
HEADER_SYMBOLS = 13
PAYLOAD_SYMBOLS = 352
TOTAL_SYMBOLS = HEADER_SYMBOLS + PAYLOAD_SYMBOLS
BITS_PER_SYMBOL = 2 * N_CARRIERS
PAYLOAD_BITS = PAYLOAD_SYMBOLS * BITS_PER_SYMBOL

TX_RMS = 0.13
MAX_SAMPLE = 0.95

GEOMETRY = _ofdm.Geometry(
    sample_rate=SAMPLE_RATE, core_samples=CORE_SAMPLES,
    guard_samples=GUARD_SAMPLES, carrier_bins=CARRIER_BINS,
).scaled_to_rms(TX_RMS)
RX_GEOMETRY = _ofdm.Geometry(
    sample_rate=RX_SAMPLE_RATE, core_samples=RX_CORE_SAMPLES,
    guard_samples=RX_GUARD_SAMPLES, carrier_bins=CARRIER_BINS,
)

HEAD_PHASE_SAMPLES = CORE_SAMPLES // 2
LEAD_IN_FADE_SAMPLES = 240
TAIL_SAMPLES = 960

FFT_OFFSET = GUARD_SAMPLES
RX_FFT_OFFSET = RX_GUARD_SAMPLES
RX_TAIL_SAMPLES = TAIL_SAMPLES // rx_audio.DECIMATION
ACQUISITION_THRESHOLD = 0.70
MIN_PRESENT_CARRIERS = 19
CARRIER_FLOOR_DB = 35.0

HEAD_SECONDS = framing.HEAD_SECONDS

#: Unambiguous range of the two frequency estimators, as a fact about the
#: geometry rather than a tunable: the cyclic-prefix angle wraps at half a
#: carrier spacing, and the header's per-symbol phase step wraps at half the
#: symbol rate.  Reported by `describe()` and asserted in
#: `_check_constants`, because "how far off may the two radios be" is the
#: first question anyone puts an HF mode on the air with.
COARSE_OFFSET_LIMIT_HZ = SAMPLE_RATE / (2.0 * CORE_SAMPLES)
FINE_OFFSET_LIMIT_HZ = SAMPLE_RATE / (2.0 * SYMBOL_SAMPLES)

# FINE_OFFSET_NOTE -- why the fine estimate is applied to the carriers
# rather than to the audio.
#
# The coarse estimate has to be undone in the time domain: an offset of
# tens of Hz against a 93.75 Hz spacing leaks each carrier into its
# neighbours, and no per-symbol phase correction repairs that.  What is
# left after it is a residual of well under a Hz, which produces no
# measurable leakage and only a phase that advances from symbol to symbol
# -- common to every carrier in the symbol, since it is a property of where
# that symbol's FFT window sits.  So it is removed as one phase per symbol
# on the analyzed carriers, which costs an outer product instead of a
# second pass over the whole capture.  See `_remove_residual_offset`.


def lead_in_samples() -> int:
    """HC1W's fixed native preamble length, aligned for acquisition."""
    wanted = int(np.ceil(HEAD_SECONDS * SAMPLE_RATE))
    cores = -((-(wanted - HEAD_PHASE_SAMPLES)) // CORE_SAMPLES)
    return cores * CORE_SAMPLES + HEAD_PHASE_SAMPLES


LEAD_IN_SAMPLES = lead_in_samples()
FRAME_SAMPLES = LEAD_IN_SAMPLES + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE


def frame_samples() -> int:
    return FRAME_SAMPLES


def frame_seconds() -> float:
    return FRAME_SECONDS


# -- reference constellations and the payload codec -----------------------

SYNC_VALUES = dsp.bits.qpsk_from_bits(
    dsp.bits.pn_bits(BITS_PER_SYMBOL, 0x1C2A7))
HEADER_VALUES = np.vstack((
    np.tile(SYNC_VALUES, (SYNC_SYMBOLS, 1)),
    dsp.bits.qpsk_from_bits(
        dsp.bits.pn_bits((HEADER_SYMBOLS - SYNC_SYMBOLS) * BITS_PER_SYMBOL,
                         0x0D96F)).reshape(HEADER_SYMBOLS - SYNC_SYMBOLS,
                                           N_CARRIERS),
))

#: Coprime with the 16,192-bit coded payload grid.
INTERLEAVER_STRIDE = 811

CODEC = dsp.PacketCodec(
    payload_bits=PAYLOAD_BITS,
    interleaver=dsp.interleave.multiplicative(PAYLOAD_BITS, INTERLEAVER_STRIDE),
    whitener_seed=0x1A5C7,
    code=dsp.K9,
)

FEC_INPUT_BITS = CODEC.information_bits
FEC_TAIL_BITS = CODEC.code.tail_bits
PACKET_BYTES = CODEC.packet_bytes
UNUSED_INFO_BITS = CODEC.unused_information_bits
MAX_PAYLOAD_BYTES = CODEC.max_payload_bytes

encode_payload_bits = CODEC.encode
decode_payload_bits = CODEC.decode_hard
decode_payload_soft = CODEC.decode_soft


def differential_encode(bits: np.ndarray, initial: np.ndarray) -> np.ndarray:
    return _diff.encode(bits, initial, PAYLOAD_SYMBOLS, N_CARRIERS)


# -- modulation -----------------------------------------------------------

def build_symbol(values: np.ndarray) -> np.ndarray:
    return _ofdm.build_symbol(GEOMETRY, values)


def symbol_carriers(symbol_audio: np.ndarray,
                    offset: int = FFT_OFFSET) -> np.ndarray:
    return _ofdm.symbol_carriers(GEOMETRY, symbol_audio, offset)


def sync_core() -> np.ndarray:
    """The 512-sample periodic waveform the head and sync symbols share.

    Core-periodic and not symbol-periodic, for VF3's reason: acquisition
    correlates the capture against itself one whole symbol (640 samples)
    apart, and a head built from repeated symbols would hold that
    correlation high across the entire head and leave the candidate ranking
    one arbitrary offset inside a plateau to rank.  512 is not a factor of
    640, so a core-periodic head is not autocorrelated at that lag at all.
    """
    return build_symbol(SYNC_VALUES)[GUARD_SAMPLES:]


def rx_sync_core() -> np.ndarray:
    """The receive-rate representation of the periodic sync core."""
    return _ofdm.build_symbol(RX_GEOMETRY, SYNC_VALUES)[RX_GUARD_SAMPLES:]


def frame_constellation(payload: bytes) -> np.ndarray:
    payload_values = differential_encode(
        encode_payload_bits(payload), HEADER_VALUES[-1])
    return np.vstack((HEADER_VALUES, payload_values))


def modulate(payload: bytes) -> np.ndarray:
    values = frame_constellation(payload)
    symbols = np.concatenate([build_symbol(row) for row in values])
    lead = np.resize(sync_core(), LEAD_IN_SAMPLES).copy()
    fade = LEAD_IN_FADE_SAMPLES
    lead[:fade] *= np.linspace(0.0, 1.0, fade, endpoint=True)
    audio = np.concatenate((lead, symbols, np.zeros(TAIL_SAMPLES)))
    if len(audio) != frame_samples():
        raise AssertionError(f"internal frame length error: {len(audio)}")
    peak = float(np.max(np.abs(audio)))
    if peak > MAX_SAMPLE:
        audio *= MAX_SAMPLE / peak
    return audio.astype(np.float32)


# -- demodulation ---------------------------------------------------------

#: Symbols the cyclic-prefix estimators measure.  The sync symbols are
#: skipped for timing (identical neighbours make their prefixes correlate
#: at every shift, so they carry no boundary information) and included for
#: frequency, where only the prefix-to-core angle within one symbol is read
#: and a repeated neighbour changes nothing.
_TIMING_SYMBOLS = np.arange(SYNC_SYMBOLS, TOTAL_SYMBOLS, dtype=np.int32)
_HEADER_CP_SYMBOLS = np.arange(HEADER_SYMBOLS, dtype=np.int32)


def _coarse_offset(analytic: np.ndarray, start: int) -> float:
    """Carrier offset from the header's cyclic prefixes, in Hz."""
    return _freq.coarse_offset_hz(RX_GEOMETRY, analytic, start,
                                  _HEADER_CP_SYMBOLS)


def _remove_residual_offset(carriers: np.ndarray, offset_hz: float,
                            start: int, shifts: np.ndarray) -> np.ndarray:
    """Undo a small residual offset as one phase per symbol.

    See FINE_OFFSET_NOTE.  `shifts` is where each symbol's FFT window
    actually started relative to its nominal position, so the correction
    follows the sample-clock fit rather than assuming an even grid.
    """
    indices = np.arange(len(carriers))
    window_start = (start + indices * RX_SYMBOL_SAMPLES + shifts + RX_FFT_OFFSET)
    phase = np.exp(-2j * np.pi * offset_hz * window_start / RX_SAMPLE_RATE)
    return carriers * phase[:, None]


def _header_bank(analytic: np.ndarray, start: int) -> np.ndarray | None:
    return _ofdm.carrier_bank(RX_GEOMETRY, analytic, start, HEADER_SYMBOLS,
                              offset=RX_FFT_OFFSET)


def _header_candidate_snr(analytic: np.ndarray, start: int) -> float:
    """Acquisition's scorer: how well a candidate start fits the header.

    Unlike VF3's, this corrects the candidate's own coarse frequency offset
    first.  Without that the ranking degrades exactly when the mode is
    needed: a real header arriving 30 Hz off fits the reference no better
    than noise does, so acquisition would rank it below whatever periodic
    junk shared the buffer.  The slice is derotated rather than the whole
    capture because only the header is being scored.
    """
    span = HEADER_SYMBOLS * RX_SYMBOL_SAMPLES
    if start < 0 or start + span > len(analytic):
        return -np.inf
    offset = _coarse_offset(analytic, start)
    header = _freq.derotate(analytic[start:start + span], offset, RX_SAMPLE_RATE)
    observed = _ofdm.carrier_bank(RX_GEOMETRY, header, 0, HEADER_SYMBOLS,
                                  offset=RX_FFT_OFFSET)
    if observed is None:
        return -np.inf
    return _eq.header_snr(observed, HEADER_VALUES)


def _acquire(analytic: np.ndarray) -> tuple[int | None, float]:
    return _acquire_kernel.acquire(
        RX_GEOMETRY, analytic, sync_symbols=SYNC_SYMBOLS,
        rank=lambda start: _header_candidate_snr(analytic, start))


def _base_result() -> dict:
    return {
        "synced": False, "payload": None, "confidence": 0.0,
        "start_index": None, "cfo_hz": 0.0, "clock_offset_ppm": 0.0,
        "carrier_snr_db": np.full(N_CARRIERS, -np.inf),
        "symbol_evm_db": np.full(TOTAL_SYMBOLS, np.inf),
        "raw_payload_bits": None,
    }


def demodulate(audio: np.ndarray) -> dict:
    """Decode one HC1W frame from receive-rate audio."""
    result = _base_result()
    samples = np.asarray(audio, dtype=np.float64).reshape(-1)
    if len(samples) < HEADER_SYMBOLS * RX_SYMBOL_SAMPLES:
        result["failure"] = "capture shorter than header"
        return result

    analytic = hilbert(samples)
    start, confidence = _acquire(analytic)
    result.update(confidence=confidence, start_index=start)
    if start is None or confidence < ACQUISITION_THRESHOLD:
        result["failure"] = "header not found"
        return result
    result["sync_end_index"] = start + HEADER_SYMBOLS * RX_SYMBOL_SAMPLES

    # -- frequency.  Coarse in the time domain, fine on the carriers.
    coarse_hz = _coarse_offset(analytic, start)
    corrected = _freq.derotate(analytic, coarse_hz, RX_SAMPLE_RATE)
    result["coarse_cfo_hz"] = coarse_hz

    fit = _timing.estimate(RX_GEOMETRY, corrected, start, _TIMING_SYMBOLS)
    result["timing_drift_samples"] = fit.drift_samples(TOTAL_SYMBOLS)
    result["timing_confidence"] = fit.confidence

    carriers = _ofdm.carrier_bank(RX_GEOMETRY, corrected, start, TOTAL_SYMBOLS,
                                  fit.intercept, fit.slope, RX_FFT_OFFSET)
    if carriers is None:
        # Still arriving.  Deliberately no end_index: the caller must keep
        # this audio and try again rather than consume a partial frame.
        result["failure"] = "frame truncated"
        return result
    result["end_index"] = min(
        len(samples),
        start + TOTAL_SYMBOLS * RX_SYMBOL_SAMPLES + RX_TAIL_SAMPLES)

    shifts = np.array([fit.shift_at(i) for i in range(TOTAL_SYMBOLS)])
    fine_hz = _freq.fine_offset_hz(RX_GEOMETRY, carriers[:HEADER_SYMBOLS],
                                   HEADER_VALUES)
    carriers = _remove_residual_offset(carriers, fine_hz, start, shifts)
    result["fine_cfo_hz"] = fine_hz
    result["cfo_hz"] = coarse_hz + fine_hz
    result["clock_offset_ppm"] = fit.clock_offset_ppm(RX_GEOMETRY)

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
    corrected_grid = equalised.copy()
    corrected_grid[HEADER_SYMBOLS:] = differential

    evm = np.empty(TOTAL_SYMBOLS)
    evm[:HEADER_SYMBOLS] = np.sqrt(np.mean(
        np.abs(equalised[:HEADER_SYMBOLS] - HEADER_VALUES) ** 2, axis=1))
    evm[HEADER_SYMBOLS:] = np.sqrt(np.mean(
        np.abs(differential - _diff.decisions(differential)) ** 2, axis=1))
    result["symbol_evm_db"] = 20.0 * np.log10(np.maximum(evm, 1e-15))

    result["raw_payload_bits"] = _diff.hard_bits(differential)
    soft_bits = _diff.soft_bits(differential,
                                _eq.carrier_weights(channel.snr_db))
    payload, meta = CODEC.decode_soft(soft_bits)
    result.update(meta)
    result.update(payload=payload, synced=True, channel=channel.gain,
                  interference=channel.offset, constellation=corrected_grid,
                  soft_payload_bits=soft_bits)
    return result


def demodulate_debug(audio: np.ndarray, reference_payload: bytes | None = None) -> dict:
    result = demodulate(audio)
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
    return (f"hc1w: {N_CARRIERS}x differential QPSK carriers "
            f"{CARRIER_HZ[0]:.2f}-{CARRIER_HZ[-1]:.2f} Hz, {TOTAL_SYMBOLS} "
            f"symbols, {MAX_PAYLOAD_BYTES} B + CRC32 in {FRAME_SECONDS:.3f} s, "
            f"offset tolerance +-{COARSE_OFFSET_LIMIT_HZ:.1f} Hz")


def _check_constants() -> None:
    assert CORE_SAMPLES == 512 and GUARD_SAMPLES == 128
    assert SYMBOL_SAMPLES == 640
    assert CARRIER_SPACING_HZ == 93.75
    assert N_CARRIERS == 23
    assert CARRIER_HZ[0] == 468.75 and CARRIER_HZ[-1] == 2531.25
    assert TOTAL_SYMBOLS == 365 and PAYLOAD_BITS == 16_192
    assert FEC_INPUT_BITS == 8_096
    assert LEAD_IN_SAMPLES % CORE_SAMPLES == HEAD_PHASE_SAMPLES
    assert FRAME_SAMPLES == 282_944
    assert PACKET_BYTES == 1_011 and UNUSED_INFO_BITS == 0
    assert MAX_PAYLOAD_BYTES == 1_005
    assert COARSE_OFFSET_LIMIT_HZ == 46.875
    assert FINE_OFFSET_LIMIT_HZ == 37.5
    assert CODEC.interleaver.is_valid()


_check_constants()
