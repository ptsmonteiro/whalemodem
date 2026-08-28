"""HC1: the robust HF frame -- what mode 0 is on FM, for an SSB channel.

`whale/afsk.py`'s `PROFILE_300` carries two jobs on the VHF FM bench: it is
the control plane (every CONNECT, ACK, DISC and timing frame, whatever the
data ladder is doing) and it is the bottom rung of the data ladder.  Both
jobs rest on assumptions FM makes true and SSB does not:

  - **Frequency is exact.** An FM receiver reproduces the transmitted audio
    frequency; two SSB receivers reproduce it offset by the difference
    between the two stations' reference oscillators plus whatever the two
    dials disagree about.  Half a ppm each way at 14 MHz is already 14 Hz,
    and `afsk.demodulate` integrates each tone in a fixed bin with no
    frequency estimate anywhere in it.
  - **A frame either arrives or does not.** FM capture makes the bench link
    essentially binary; a fading HF path delivers most of a frame most of
    the time.  CPFSK framing has a CRC and no correction, so on HF it
    throws away frames that are one bit wrong.
  - **The channel has no memory.** No FM path on this bench showed
    measurable delay spread; an HF path routinely has a millisecond or two,
    which smears a 300-baud symbol very little and a fast one a great deal.

HC1 answers those three directly, and reuses `whale/dsp/` for all of it:

    frequency  the cyclic prefix gives a coarse carrier offset and the
               header a fine one (`whale.dsp.freq`, written for exactly
               this and until now only a VF3 diagnostic).  Everything after
               acquisition runs on the corrected signal.
    fading     rate-1/2 K=7 convolutional coding with soft-decision Viterbi
               over an interleaved 1,292-bit grid, CRC32 underneath
               (`whale.dsp.fec`, `whale.dsp.framing`).  A frame survives
               errors instead of being discarded by them.
    delay      a 2.67 ms cyclic prefix, so echoes inside it cost margin
               rather than the symbol.

What it deliberately does *not* do is chase throughput.  This is the frame
every control exchange rides and the one a struggling link falls back to,
so it spends its bandwidth on margin: 19 carriers of differential QPSK,
half of them redundancy, in 690 ms.

    [2,304 lead-in][47 x (128 prefix + 512 core)][960 tail] = 33,344 samples

Geometry choices worth the ink:

  - **512-sample core, 93.75 Hz spacing.**  Twice VF3's spacing, which is
    what makes an uncorrected residual offset a small fraction of a
    carrier rather than a large one, and keeps each carrier far wider than
    any Doppler spread this path will show.
  - **19 carriers, 656.25-2343.75 Hz.**  Inside a 2.4 kHz SSB data filter
    with room at both skirts for the offset the receiver has yet to
    measure.  VF3's 468-3140 Hz band does not fit through one.
  - **A 34-symbol payload.**  1,292 coded bits is the smallest grid that
    leaves a whole number of packet bytes with no bits stranded (see
    `_check_constants`), and the 74 payload bytes it yields leave 64 for a
    DATA chunk once the link's 10-byte air header is taken out -- enough
    for the largest control frame the link builds, with margin.

Like VF3 this is geometry and wiring: every transform lives in
`whale/dsp/`.  What is HC1's own is which bins carry data, how long the
header is, and that the frequency estimate is applied rather than merely
reported.
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
CORE_SAMPLES = 512
GUARD_SAMPLES = 128
SYMBOL_SAMPLES = GUARD_SAMPLES + CORE_SAMPLES

#: 19 carriers at 93.75 Hz, 656.25-2343.75 Hz.  Bin 7 is the lowest that
#: clears an SSB filter's low skirt with room for a coarse offset still to
#: be measured; bin 25 is the highest that does the same at the top.
CARRIER_BINS = np.arange(7, 26, dtype=np.int32)
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
PAYLOAD_SYMBOLS = 34
TOTAL_SYMBOLS = HEADER_SYMBOLS + PAYLOAD_SYMBOLS
BITS_PER_SYMBOL = 2 * N_CARRIERS
PAYLOAD_BITS = PAYLOAD_SYMBOLS * BITS_PER_SYMBOL

TX_RMS = 0.13
MAX_SAMPLE = 0.95

GEOMETRY = _ofdm.Geometry(
    sample_rate=SAMPLE_RATE, core_samples=CORE_SAMPLES,
    guard_samples=GUARD_SAMPLES, carrier_bins=CARRIER_BINS,
).scaled_to_rms(TX_RMS)

#: 48 ms -- four whole sync cores plus HEAD_PHASE_SAMPLES.  SSB has no
#: squelch to blank the start of a transmission, so unlike the FM profiles'
#: 1 s head pad this floor is only what ramps the transmitter and the sound
#: card up.  The link negotiates it longer if the far end reports losing
#: more than that; see `lead_in_samples`.
LEAD_IN_SAMPLES = 2_304

#: Where in the sync core the head must stop, and the reason `lead_in_samples`
#: quantizes rather than rounding.
#:
#: A sync symbol is `core[-128:] + core`, so a head that ends exactly on a
#: core boundary has *already transmitted* a sync symbol in its own last 640
#: samples -- the tail of one core followed by a whole one is precisely that
#: shape.  Acquisition then finds its lag-640 self-correlation holding at
#: 1.0 for the 640 samples before the header, collapses that into one
#: proposal group, and returns whichever sample inside the plateau numerical
#: noise favours.  Measured before this was fixed: a clean frame acquired
#: 329 samples early and decoded to nothing.
#:
#: Stopping half a core short instead makes that alignment impossible for
#: any head length: the match needs the head to end at core phase 0 and this
#: guarantees phase 256.  The head stays core-periodic, so `_head.measure`
#: is unaffected -- it recovers the phase circularly and counts whole cores
#: back from wherever the header starts.
HEAD_PHASE_SAMPLES = CORE_SAMPLES // 2
LEAD_IN_FADE_SAMPLES = 240
TAIL_SAMPLES = 960
FRAME_SAMPLES = LEAD_IN_SAMPLES + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

FFT_OFFSET = GUARD_SAMPLES
ACQUISITION_THRESHOLD = 0.70
MIN_PRESENT_CARRIERS = 15
CARRIER_FLOOR_DB = 35.0

HEAD_MATCH_THRESHOLD = _head.MATCH_THRESHOLD
HEAD_MIN_ENERGY_FRACTION = _head.MIN_ENERGY_FRACTION
#: One sample of block-to-block alignment drift, allowed because the head is
#: measured on frequency-corrected audio.  See `_measure_head`.
HEAD_PHASE_TOLERANCE = 1

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


def lead_in_samples(head_seconds: float = None) -> int:
    """Leading sync-core samples for a requested head duration.

    Rounded *up* to the next whole core plus HEAD_PHASE_SAMPLES, so no
    requested duration can land the head on a core boundary and blunt
    acquisition.  LEAD_IN_SAMPLES is the floor, for the same reason it is in
    VF3: it is what ramps the transmitter and the sound card up, and nothing
    shorter has ever been transmitted.
    """
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

#: Interleaver stride, chosen for this grid rather than inherited.
#:
#: A multiplicative interleaver `i -> i*a mod N` has two spreads and both
#: are single numbers: on-air neighbours land `a` apart in the codeword,
#: and codeword neighbours land `a^-1 mod N` apart on the air.  VF2..VF5
#: all use 8101, which reduces to 349 on this 1,292-bit grid and leaves the
#: first spread at 349.  Searching every stride coprime with 1,292 for the
#: largest *worse* of the two puts 693 at the top: 591 either way, and its
#: inverse is 17 bit-slots out of 38, so two adjacent codeword bits land 15
#: symbols apart and on nearly opposite carriers.
#:
#: The carrier axis needs no help from the stride.  One carrier occupies
#: on-air positions 38 apart, and `38a mod 1292` is `38 * (a mod 34)` for
#: every `a`, so a carrier lost to a notch always maps to 34 codeword
#: positions evenly spaced across the whole codeword.
INTERLEAVER_STRIDE = 693

CODEC = dsp.PacketCodec(
    payload_bits=PAYLOAD_BITS,
    interleaver=dsp.interleave.multiplicative(PAYLOAD_BITS, INTERLEAVER_STRIDE),
    whitener_seed=0x1A5C7,
    code=dsp.K7,
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

#: Symbols the cyclic-prefix estimators measure.  The sync symbols are
#: skipped for timing (identical neighbours make their prefixes correlate
#: at every shift, so they carry no boundary information) and included for
#: frequency, where only the prefix-to-core angle within one symbol is read
#: and a repeated neighbour changes nothing.
_TIMING_SYMBOLS = np.arange(SYNC_SYMBOLS, TOTAL_SYMBOLS, dtype=np.int32)
_HEADER_CP_SYMBOLS = np.arange(HEADER_SYMBOLS, dtype=np.int32)


def _coarse_offset(analytic: np.ndarray, start: int) -> float:
    """Carrier offset from the header's cyclic prefixes, in Hz."""
    return _freq.coarse_offset_hz(GEOMETRY, analytic, start,
                                  _HEADER_CP_SYMBOLS)


def _remove_residual_offset(carriers: np.ndarray, offset_hz: float,
                            start: int, shifts: np.ndarray) -> np.ndarray:
    """Undo a small residual offset as one phase per symbol.

    See FINE_OFFSET_NOTE.  `shifts` is where each symbol's FFT window
    actually started relative to its nominal position, so the correction
    follows the sample-clock fit rather than assuming an even grid.
    """
    indices = np.arange(len(carriers))
    window_start = (start + indices * SYMBOL_SAMPLES + shifts + FFT_OFFSET)
    phase = np.exp(-2j * np.pi * offset_hz * window_start / SAMPLE_RATE)
    return carriers * phase[:, None]


def _header_bank(analytic: np.ndarray, start: int) -> np.ndarray | None:
    return _ofdm.carrier_bank(GEOMETRY, analytic, start, HEADER_SYMBOLS,
                              offset=FFT_OFFSET)


def _header_candidate_snr(analytic: np.ndarray, start: int) -> float:
    """Acquisition's scorer: how well a candidate start fits the header.

    Unlike VF3's, this corrects the candidate's own coarse frequency offset
    first.  Without that the ranking degrades exactly when the mode is
    needed: a real header arriving 30 Hz off fits the reference no better
    than noise does, so acquisition would rank it below whatever periodic
    junk shared the buffer.  The slice is derotated rather than the whole
    capture because only the header is being scored.
    """
    span = HEADER_SYMBOLS * SYMBOL_SAMPLES
    if start < 0 or start + span > len(analytic):
        return -np.inf
    offset = _coarse_offset(analytic, start)
    header = _freq.derotate(analytic[start:start + span], offset, SAMPLE_RATE)
    observed = _ofdm.carrier_bank(GEOMETRY, header, 0, HEADER_SYMBOLS,
                                  offset=FFT_OFFSET)
    if observed is None:
        return -np.inf
    return _eq.header_snr(observed, HEADER_VALUES)


def _acquire(analytic: np.ndarray) -> tuple[int | None, float]:
    return _acquire_kernel.acquire(
        GEOMETRY, analytic, sync_symbols=SYNC_SYMBOLS,
        rank=lambda start: _header_candidate_snr(analytic, start))


def _measure_head(samples: np.ndarray, start: int) -> tuple[int, float]:
    """How much of the transmitted head survived, in whole 512-sample cores.

    Takes the *frequency-corrected* audio.  The head is matched against a
    reference waveform, and even an 8 Hz offset -- less than this bench
    measured between its two radios -- cuts a 47-core head to 1, which the
    link would answer by lengthening a head that was arriving perfectly.
    Correcting first is what keeps the head feedback meaningful on an
    offset channel.

    Correcting is also why the phase tolerance is one sample rather than
    zero: the correction is a slow phase ramp across the whole capture, and
    a ramp of a few degrees walks the correlation peak over a sample
    boundary partway down a long head.  See `_head.measure`'s
    `phase_tolerance`.
    """
    return _head.measure(samples, start, sync_core(),
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
    """Decode one HC1 frame out of `audio`.

    `head_seconds` is accepted for symmetry with `modulate` and with the
    `WaveformMode` contract; acquisition locks on the header, not the head,
    so it does not need to be told how long the head was.

    Alongside HC1's own diagnostics the result carries the three keys the
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

    # -- frequency.  Coarse in the time domain, fine on the carriers.
    coarse_hz = _coarse_offset(analytic, start)
    corrected = _freq.derotate(analytic, coarse_hz, SAMPLE_RATE)
    result["coarse_cfo_hz"] = coarse_hz

    head_cores, head_score = _measure_head(np.real(corrected), start)
    result["head_cores_received"] = head_cores
    result["head_match"] = head_score

    fit = _timing.estimate(GEOMETRY, corrected, start, _TIMING_SYMBOLS)
    result["timing_drift_samples"] = fit.drift_samples(TOTAL_SYMBOLS)
    result["timing_confidence"] = fit.confidence

    carriers = _ofdm.carrier_bank(GEOMETRY, corrected, start, TOTAL_SYMBOLS,
                                  fit.intercept, fit.slope, FFT_OFFSET)
    if carriers is None:
        # Still arriving.  Deliberately no end_index: the caller must keep
        # this audio and try again rather than consume a partial frame.
        result["failure"] = "frame truncated"
        return result
    result["end_index"] = min(
        len(samples),
        start + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES)

    shifts = np.array([fit.shift_at(i) for i in range(TOTAL_SYMBOLS)])
    fine_hz = _freq.fine_offset_hz(GEOMETRY, carriers[:HEADER_SYMBOLS],
                                   HEADER_VALUES)
    carriers = _remove_residual_offset(carriers, fine_hz, start, shifts)
    result["fine_cfo_hz"] = fine_hz
    result["cfo_hz"] = coarse_hz + fine_hz
    result["clock_offset_ppm"] = fit.clock_offset_ppm(GEOMETRY)

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
    return (f"hc1: {N_CARRIERS}x differential QPSK carriers "
            f"{CARRIER_HZ[0]:.2f}-{CARRIER_HZ[-1]:.2f} Hz, {TOTAL_SYMBOLS} "
            f"symbols, {MAX_PAYLOAD_BYTES} B + CRC32 in {FRAME_SECONDS:.3f} s, "
            f"offset tolerance +-{COARSE_OFFSET_LIMIT_HZ:.1f} Hz")


def _check_constants() -> None:
    assert CORE_SAMPLES == 512 and GUARD_SAMPLES == 128
    assert SYMBOL_SAMPLES == 640
    assert CARRIER_SPACING_HZ == 93.75
    assert N_CARRIERS == 19
    assert CARRIER_HZ[0] == 656.25 and CARRIER_HZ[-1] == 2343.75
    assert TOTAL_SYMBOLS == 47 and PAYLOAD_BITS == 1_292
    assert FEC_INPUT_BITS == 646
    assert LEAD_IN_SAMPLES % CORE_SAMPLES == HEAD_PHASE_SAMPLES
    assert FRAME_SAMPLES == 33_344
    # No stranded bits: the coded grid divides exactly into whole packet
    # bytes plus the trellis tail.  This is what picked 34 payload symbols.
    assert PACKET_BYTES == 80 and UNUSED_INFO_BITS == 0
    assert MAX_PAYLOAD_BYTES == 74
    assert COARSE_OFFSET_LIMIT_HZ == 46.875
    assert FINE_OFFSET_LIMIT_HZ == 37.5
    assert CODEC.interleaver.is_valid()


_check_constants()
