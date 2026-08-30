"""HC2: a candidate fast HF-SSB rung above HC1 -- differential 8-PSK.

HC1 (`whale/modes/hc1.py`) qualified in quiet and moderate simulated HF
conditions but stalled at roughly 45-60% frame delivery under the disturbed
Watterson preset (2 ms delay spread, 1.0 Hz Doppler spread) even at 20 dB
SNR -- see `logs/mode_qualification/hf-ssb/hc0-hc1/2026-08-30/INDEX.md`.
That investigation traced the ceiling to a structural cause, not a thermal
noise floor: HC1's 93.75 Hz carrier spacing is comparable to the coherence
bandwidth the disturbed preset implies, so several adjacent carriers fade
together, and a correlated multi-carrier fade costs the rate-1/2 K=7 Viterbi
decoder more coded bits at once than it reliably recovers -- interleaving
alone cannot fix a fade that lasts the whole frame on the same carriers.

HC2 does not attempt to out-run that ceiling; nothing changes the coherence
bandwidth of the path, and a mode that adds bits per symbol only sharpens
the same correlated-fade problem if it does not also add margin.  Instead
it makes a different bet, the one VARA's own multi-rate ladder makes: push
throughput on the conditions this geometry already clears (quiet, and
moderate above HC1's own floor), spend some of the modulation gain buying
back headroom with a stronger code, and let a station that cannot hold HC2
fall back to HC1 or HC0 the way `_maybe_adapt` already falls back off VF3 on
VHF.  It reuses HC1's exact carrier geometry -- same 512-sample core, same
128-sample cyclic prefix, same 19 carriers at 656.25-2343.75 Hz -- because
that geometry is proven to fit an SSB filter and its coherence-bandwidth
behaviour is now measured, not guessed.

What changes, and why:

  modulation   differential 8-PSK (3 bits/carrier/symbol) instead of
               differential QPSK (2 bits).  Gray-coded
               (`whale.dsp.differential.gray_psk`) so a one-step phase slip
               -- the dominant error at moderate SNR -- costs one coded bit
               rather than risking more.  Differential, not coherent,
               for the same reason HC1 is: a per-carrier equalizer is fit
               once at the header and the payload is assumed to hold still
               under it for less than a second, which HC1's own bench
               notes hold on a real HF path.
  code         rate-1/2 K=9 (561, 753) in place of K=7 (171, 133)
               (`whale.dsp.fec.K9`).  Free distance 12 against 10, roughly
               0.6-1 dB more coding gain, spent narrowing the SNR penalty
               8-PSK pays against QPSK (~3.6 dB at equal bit-error rate in
               AWGN) rather than eliminating it -- see RESULTS.md for the
               benchmark that this budget was closed against, and where it
               was not.  4x the trellis states of K7 (256 vs 64), so 4x the
               per-frame decode work; RESULTS.md also has the measured
               wall-clock cost against a Pi-class CPU budget.

Everything else is unchanged from HC1's wiring: the same header/acquisition
scheme (`whale.dsp.acquire`, `whale.dsp.freq`, `whale.dsp.timing`,
`whale.dsp.equalize`), the same interleaver family
(`whale.dsp.interleave.multiplicative`), the same CRC32/length/whitening
packet codec (`whale.dsp.framing.PacketCodec`).  HC2 keeps HC1's 47-symbol,
0.695 s frame exactly, so the throughput comparison in RESULTS.md isolates
what the modulation and code choice alone buy: 114 payload bytes against
HC1's 74, in the same airtime.

This module lives under `experiments/` rather than `whale/modes/` because it
has not been through the qualification process in MODE_QUALIFICATION.md --
no hardware evidence, no retained default-registry entry, no mode ID
allocation for shipping.  Simulated-channel results are retained under
`logs/mode_qualification/hf-ssb/hc2-experimental/` with that provisional
framing, following the pattern `MODE_QUALIFICATION.md` already uses for
incomplete evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import hilbert

from whale import dsp, rx_audio
from whale.dsp import (acquire as _acquire_kernel, differential as _diff,
                       equalize as _eq, fec as _fec, freq as _freq,
                       head as _head, ofdm as _ofdm, timing as _timing)

# -- geometry, identical to HC1 --------------------------------------------

SAMPLE_RATE = 48_000
CORE_SAMPLES = 512
GUARD_SAMPLES = 128
SYMBOL_SAMPLES = GUARD_SAMPLES + CORE_SAMPLES
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE
RX_CORE_SAMPLES = CORE_SAMPLES // rx_audio.DECIMATION
RX_GUARD_SAMPLES = GUARD_SAMPLES // rx_audio.DECIMATION
RX_SYMBOL_SAMPLES = RX_GUARD_SAMPLES + RX_CORE_SAMPLES

#: Same 19 carriers, 93.75 Hz spacing, as HC1 -- see hc1.py's own note on
#: why this fits an SSB filter with headroom.  Kept identical rather than
#: rederived: this is the one part of HC1's design the disturbed-preset
#: investigation did not indict.  A carrier spacing *narrower* than HC1's
#: would only make correlated fading worse; wider would need more
#: bandwidth than a 2.4 kHz SSB channel has to give while keeping 19
#: carriers, which is not on offer.
CARRIER_BINS = np.arange(7, 26, dtype=np.int32)
CARRIER_SPACING_HZ = SAMPLE_RATE / CORE_SAMPLES
CARRIER_HZ = CARRIER_BINS.astype(np.float64) * CARRIER_SPACING_HZ
N_CARRIERS = len(CARRIER_BINS)

#: 3 bits/carrier/symbol -- differential 8-PSK, Gray-coded.
BITS_PER_SYMBOL_PER_CARRIER = 3
PSK_POINTS, PSK_LABELS = dsp.differential.gray_psk(BITS_PER_SYMBOL_PER_CARRIER)

SYNC_SYMBOLS = 5
HEADER_SYMBOLS = 13
#: Kept equal to HC1's payload symbol count, so the two modes' frames are
#: the same duration and the throughput difference reported in RESULTS.md
#: comes only from the modulation order and code, not from spending more
#: airtime.
PAYLOAD_SYMBOLS = 34
TOTAL_SYMBOLS = HEADER_SYMBOLS + PAYLOAD_SYMBOLS
BITS_PER_SYMBOL = BITS_PER_SYMBOL_PER_CARRIER * N_CARRIERS
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

LEAD_IN_SAMPLES = 2_304
HEAD_PHASE_SAMPLES = CORE_SAMPLES // 2
LEAD_IN_FADE_SAMPLES = 240
TAIL_SAMPLES = 960
FRAME_SAMPLES = LEAD_IN_SAMPLES + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

FFT_OFFSET = GUARD_SAMPLES
RX_FFT_OFFSET = RX_GUARD_SAMPLES
RX_TAIL_SAMPLES = TAIL_SAMPLES // rx_audio.DECIMATION
ACQUISITION_THRESHOLD = 0.70
MIN_PRESENT_CARRIERS = 15
CARRIER_FLOOR_DB = 35.0

HEAD_MATCH_THRESHOLD = _head.MATCH_THRESHOLD
HEAD_MIN_ENERGY_FRACTION = _head.MIN_ENERGY_FRACTION
HEAD_PHASE_TOLERANCE = 1

COARSE_OFFSET_LIMIT_HZ = SAMPLE_RATE / (2.0 * CORE_SAMPLES)
FINE_OFFSET_LIMIT_HZ = SAMPLE_RATE / (2.0 * SYMBOL_SAMPLES)


def lead_in_samples(head_seconds: float = None) -> int:
    if head_seconds is None:
        wanted = LEAD_IN_SAMPLES
    elif head_seconds < 0:
        raise ValueError("head duration must not be negative")
    else:
        wanted = max(LEAD_IN_SAMPLES, int(round(head_seconds * SAMPLE_RATE)))
    cores = -(-(wanted - HEAD_PHASE_SAMPLES) // CORE_SAMPLES)
    return cores * CORE_SAMPLES + HEAD_PHASE_SAMPLES


DEFAULT_HEAD_SECONDS = LEAD_IN_SAMPLES / SAMPLE_RATE


def frame_samples(head_seconds: float = DEFAULT_HEAD_SECONDS) -> int:
    return (lead_in_samples(head_seconds)
            + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES)


def frame_seconds(head_seconds: float = DEFAULT_HEAD_SECONDS) -> float:
    return frame_samples(head_seconds) / SAMPLE_RATE


# -- reference constellations and the payload codec -------------------------

#: The header stays coherent QPSK, as HC1's does: it is the acquisition and
#: equalizer-fit reference, not the payload modulation, and there is no
#: reason for it to match the payload's constellation order.
SYNC_VALUES = dsp.bits.qpsk_from_bits(
    dsp.bits.pn_bits(2 * N_CARRIERS, 0x0B37D))
HEADER_VALUES = np.vstack((
    np.tile(SYNC_VALUES, (SYNC_SYMBOLS, 1)),
    dsp.bits.qpsk_from_bits(
        dsp.bits.pn_bits((HEADER_SYMBOLS - SYNC_SYMBOLS) * 2 * N_CARRIERS,
                         0x152A9)).reshape(HEADER_SYMBOLS - SYNC_SYMBOLS,
                                           N_CARRIERS),
))

#: Coprime with PAYLOAD_BITS (1,938 = 2 x 3 x 17 x 19); picked the way HC1's
#: was, by the same two spreads (on-air neighbours in the codeword, codeword
#: neighbours on the air), searched over strides coprime with 1,938.
INTERLEAVER_STRIDE = 701

CODEC = dsp.PacketCodec(
    payload_bits=PAYLOAD_BITS,
    interleaver=dsp.interleave.multiplicative(PAYLOAD_BITS, INTERLEAVER_STRIDE),
    whitener_seed=0x0F1B3,
    code=_fec.K9,
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
    return _diff.encode(bits, initial, PAYLOAD_SYMBOLS, N_CARRIERS,
                        points=PSK_POINTS, labels=PSK_LABELS)


# -- modulation ---------------------------------------------------------

def build_symbol(values: np.ndarray) -> np.ndarray:
    return _ofdm.build_symbol(GEOMETRY, values)


def symbol_carriers(symbol_audio: np.ndarray,
                    offset: int = FFT_OFFSET) -> np.ndarray:
    return _ofdm.symbol_carriers(GEOMETRY, symbol_audio, offset)


def sync_core() -> np.ndarray:
    return build_symbol(SYNC_VALUES)[GUARD_SAMPLES:]


def rx_sync_core() -> np.ndarray:
    return _ofdm.build_symbol(RX_GEOMETRY, SYNC_VALUES)[RX_GUARD_SAMPLES:]


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
_HEADER_CP_SYMBOLS = np.arange(HEADER_SYMBOLS, dtype=np.int32)


def _coarse_offset(analytic: np.ndarray, start: int) -> float:
    return _freq.coarse_offset_hz(RX_GEOMETRY, analytic, start,
                                  _HEADER_CP_SYMBOLS)


def _remove_residual_offset(carriers: np.ndarray, offset_hz: float,
                            start: int, shifts: np.ndarray) -> np.ndarray:
    indices = np.arange(len(carriers))
    window_start = (start + indices * RX_SYMBOL_SAMPLES + shifts + RX_FFT_OFFSET)
    phase = np.exp(-2j * np.pi * offset_hz * window_start / RX_SAMPLE_RATE)
    return carriers * phase[:, None]


def _header_candidate_snr(analytic: np.ndarray, start: int) -> float:
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


def _measure_head(samples: np.ndarray, start: int) -> tuple[int, float]:
    return _head.measure(samples, start, rx_sync_core(),
                         phase_tolerance=HEAD_PHASE_TOLERANCE)


def _base_result() -> dict:
    return {
        "synced": False, "payload": None, "confidence": 0.0,
        "start_index": None, "cfo_hz": 0.0, "clock_offset_ppm": 0.0,
        "carrier_snr_db": np.full(N_CARRIERS, -np.inf),
        "symbol_evm_db": np.full(TOTAL_SYMBOLS, np.inf),
        "raw_payload_bits": None,
    }


def demodulate(audio: np.ndarray, *,
               head_seconds: float = DEFAULT_HEAD_SECONDS) -> dict:
    del head_seconds
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

    coarse_hz = _coarse_offset(analytic, start)
    corrected = _freq.derotate(analytic, coarse_hz, RX_SAMPLE_RATE)
    result["coarse_cfo_hz"] = coarse_hz

    head_cores, head_score = _measure_head(np.real(corrected), start)
    result["head_cores_received"] = head_cores
    result["head_match"] = head_score

    fit = _timing.estimate(RX_GEOMETRY, corrected, start, _TIMING_SYMBOLS)
    result["timing_drift_samples"] = fit.drift_samples(TOTAL_SYMBOLS)
    result["timing_confidence"] = fit.confidence

    carriers = _ofdm.carrier_bank(RX_GEOMETRY, corrected, start, TOTAL_SYMBOLS,
                                  fit.intercept, fit.slope, RX_FFT_OFFSET)
    if carriers is None:
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

    decided = _diff.decisions(differential, PSK_POINTS)
    evm = np.empty(TOTAL_SYMBOLS)
    evm[:HEADER_SYMBOLS] = np.sqrt(np.mean(
        np.abs(equalised[:HEADER_SYMBOLS] - HEADER_VALUES) ** 2, axis=1))
    evm[HEADER_SYMBOLS:] = np.sqrt(np.mean(
        np.abs(differential - decided) ** 2, axis=1))
    result["symbol_evm_db"] = 20.0 * np.log10(np.maximum(evm, 1e-15))

    result["raw_payload_bits"] = _diff.hard_bits(
        differential, PSK_POINTS, PSK_LABELS)
    soft_bits = _diff.soft_bits(differential, _eq.carrier_weights(channel.snr_db),
                                PSK_POINTS, PSK_LABELS)
    payload, meta = CODEC.decode_soft(soft_bits)
    result.update(meta)
    result.update(payload=payload, synced=True, channel=channel.gain,
                  interference=channel.offset, constellation=corrected_grid,
                  soft_payload_bits=soft_bits)
    return result


def demodulate_debug(audio: np.ndarray, reference_payload: bytes | None = None,
                     *, head_seconds: float = DEFAULT_HEAD_SECONDS) -> dict:
    result = demodulate(audio, head_seconds=head_seconds)
    if reference_payload is None or result.get("raw_payload_bits") is None:
        return result
    expected = encode_payload_bits(reference_payload)
    errors = result["raw_payload_bits"] != expected
    grid = errors.reshape(PAYLOAD_SYMBOLS, N_CARRIERS,
                          BITS_PER_SYMBOL_PER_CARRIER)
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
    return (f"hc2: {N_CARRIERS}x differential 8-PSK carriers "
            f"{CARRIER_HZ[0]:.2f}-{CARRIER_HZ[-1]:.2f} Hz, {TOTAL_SYMBOLS} "
            f"symbols, K=9 conv. code, {MAX_PAYLOAD_BYTES} B + CRC32 in "
            f"{FRAME_SECONDS:.3f} s, offset tolerance "
            f"+-{COARSE_OFFSET_LIMIT_HZ:.1f} Hz")


def _check_constants() -> None:
    assert CORE_SAMPLES == 512 and GUARD_SAMPLES == 128
    assert SYMBOL_SAMPLES == 640
    assert CARRIER_SPACING_HZ == 93.75
    assert N_CARRIERS == 19
    assert CARRIER_HZ[0] == 656.25 and CARRIER_HZ[-1] == 2343.75
    assert TOTAL_SYMBOLS == 47
    assert BITS_PER_SYMBOL == 57 and PAYLOAD_BITS == 1_938
    assert FEC_INPUT_BITS == 969
    assert FEC_TAIL_BITS == 8
    assert PACKET_BYTES == 120
    assert MAX_PAYLOAD_BYTES == 114
    assert FRAME_SAMPLES == 33_344
    assert COARSE_OFFSET_LIMIT_HZ == 46.875
    assert FINE_OFFSET_LIMIT_HZ == 37.5
    assert CODEC.interleaver.is_valid()
    assert PSK_POINTS.shape == (8,) and PSK_LABELS.shape == (8, 3)


_check_constants()
