"""HF4: a from-scratch, maximum-speed HF-SSB waveform.

HF4 targets Level 4 of the HF SSB speed ladder in ``SPEED_LADDERS.md``:
maximum speed inside a deliberately narrow envelope (benign/static fading at
+13 dB waveform SNR and above). Unlike the general-purpose and fast rungs
below it, a Level-4 waveform is not expected to spend any margin on fading
robustness, so this design spends its whole budget on raw bits/second inside
a 2,400 Hz slot (300-2,700 Hz) rather than on diversity or heavy coding.

This module is a standalone experiment. It is independent of every other
mode's specific frame geometry, carrier plan, or coding choice -- see
``DESIGN.md`` in this directory for the full rationale -- and reuses only
the project's generic, mode-agnostic DSP library (``whale.dsp``), the same
way any mode may reuse NumPy.

Design summary (see DESIGN.md for the numbers behind each choice):

* Coherent OFDM, 149 carriers, 15.625 Hz spacing, 343.75-2,656.25 Hz -- well
  inside the 300-2,700 Hz ceiling, with headroom at both edges for real SSB
  radio filter rolloff. Doubled from the original 75-carrier/31.25 Hz plan
  in the 2026-09-01 dense-carrier redesign, which halves the fixed 64-sample
  cyclic prefix's relative overhead (14.3% -> 7.7% of a symbol) to afford a
  stronger inner code -- see DESIGN.md's "Carrier plan" and "Dense-carrier
  redesign" for the full rationale and the frame-length/rate search behind
  the final choice.
* 16-QAM, Gray-coded, on every carrier.
* A punctured rate-11/12 inner convolutional code (``whale.dsp.fec.K7``,
  soft Viterbi) plus a block interleaver spreading coded bits across every
  carrier and data symbol, and per-carrier reliability weighting from the
  header fit's SNR estimate -- added 2026-09-01 after a Monte Carlo campaign
  found the original no-FEC design plateaus at 70-83% frame decode even at
  35 dB waveform SNR, because a handful of the required channel's carriers
  land in a real, frame-static fade regardless of aggregate SNR; strengthened
  from an original rate-19/20 (0.95, the strongest the old sparser carrier
  plan could afford) once the dense-carrier redesign freed enough throughput
  budget for a materially stronger code. See DESIGN.md's "Inner FEC and
  interleaving" for the full mechanism and why the interleaver's exact
  construction matters. A 16-bit length field and CRC32 (whitened, so a
  stuck carrier does not look like a long run of one symbol) still detect
  what the code cannot fix; the link's ARQ retransmits it.
* One repeated-symbol sync block for acquisition, a short block of distinct
  known OFDM symbols for coarse per-carrier channel/CFO estimation, and
  sparse full-band pilot symbols through the payload for phase tracking --
  spending a little payload time, not any carrier bandwidth, on channel
  tracking.
* A raised-cosine amplitude taper at the very start and end of the burst
  controls spectral splatter at the edges of the keying; carriers stop a
  clear 3-4 bin margin short of both edges of the 300-2,700 Hz ceiling so
  that residual OFDM sidelobes, and a real SSB transmit/receive filter's
  rolloff, both stay inside the passband.
"""

from __future__ import annotations

import binascii

import numpy as np
from scipy.signal import hilbert, resample_poly

from whale.dsp import acquire as _acquire
from whale.dsp import equalize as _equalize
from whale.dsp import fec as _fec
from whale.dsp import freq as _freq
from whale.dsp import interleave as _interleave
from whale.dsp import ofdm as _ofdm
from whale.dsp.bits import pn_bits, qpsk_from_bits

# --------------------------------------------------------------------------
# Sample rates.  The link's shared receive front end hands every decoder
# 12 kHz audio (see FRAMING.md / whale/waveform.py); transmit audio matches
# the 48 kHz radio I/O rate every other mode uses.  12 kHz is also a
# convenient native rate for this design's own DSP: no separate downsample
# step is needed in decode(), and encode() only needs a clean 4x upsample.
# --------------------------------------------------------------------------
RX_SAMPLE_RATE = 12_000
SAMPLE_RATE = 48_000
_TX_UPSAMPLE = SAMPLE_RATE // RX_SAMPLE_RATE
assert SAMPLE_RATE == RX_SAMPLE_RATE * _TX_UPSAMPLE

# --------------------------------------------------------------------------
# OFDM geometry.
#
# core_samples=384 at 12 kHz gives 31.25 Hz carrier spacing. Carriers run
# from bin 11 (343.75 Hz) through bin 85 (2,656.25 Hz): 75 carriers, with
# 43.75 Hz of raw carrier-frequency margin below 300 Hz and 43.75 Hz below
# 2,700 Hz for real SSB transmit/receive filter rolloff and residual OFDM
# sidelobe energy, on top of the explicit edge taper below. (Two carriers
# were added at the top and one at the bottom relative to the original
# 72-carrier/12-83 bin plan, to recover throughput lost to the longer guard
# below; see the 2026-09-01 fix note for why, and RESULTS.md for the
# measured occupied-bandwidth campaign confirming this still clears the
# 300-2,700 Hz ceiling with margin.)
#
# guard_samples=64 (5.33 ms at 12 kHz) is sized against the *channel
# filter's* impulse-response memory, not just propagation delay spread.
# SPEED_LADDERS.md's benign/static envelope separately requires a
# qualification channel to retain its complete filter description (not
# identity/AWGN-only), and the required 250-3,100 Hz 6th-order Butterworth
# bandpass (`whale.channel.FilterChannel`, applied twice -- once before and
# once after the propagation/noise stages) has a settling tail around
# 6-11 ms at 12 kHz (99-99.99% impulse energy; cascaded through both filter
# applications), an order of magnitude longer than the 0.1 ms propagation
# delay-spread figure the original 12-sample (1.0 ms) guard was sized
# against alone. A 2026-09-01 qualification campaign
# (`logs/mode_qualification/hf-ssb/hf4/2026-09-01/INDEX.md`) found the
# original guard left every frame corrupted by inter-symbol interference
# from this filter memory, independent of SNR. 64 samples was chosen from a
# direct empirical sweep (`experiments/hf4/test_hf4.py`'s guard-margin
# regression test and the fix campaign's diagnostics): guard lengths from
# 12 up to ~48 samples still failed the noiseless filter-only diagnostic,
# while 64 samples and above decoded cleanly and repeatably across
# independent random payloads/seeds -- comfortable margin above the
# failure boundary without paying for a much longer guard this mode does
# not need.
# --------------------------------------------------------------------------
#: 2026-09-01 dense-carrier redesign. CORE_SAMPLES doubled from 384 to 768
#: (halving carrier spacing from 31.25 Hz to 15.625 Hz, doubling carrier
#: count at essentially the same occupied Hz band) while GUARD_SAMPLES stays
#: fixed at 64 samples (5.33 ms) -- the guard duration is load-bearing
#: against the required benign/static channel's bandpass-filter memory (see
#: the note below) and is not touched by this change. The point of this
#: change is purely to shrink the guard's *relative* overhead: symbol length
#: goes from 448 to 832 samples, so the fixed 64-sample guard falls from
#: 14.3% to 7.7% of every symbol's airtime. That freed-up fraction of the
#: raw bit budget is what pays for a stronger inner code (see FEC_K/FEC_N
#: below) -- this is the fix for the 2026-09-01-fec campaign's finding that
#: no code below rate ~0.895 could clear the 7,000 bit/s floor on the old,
#: sparser carrier plan, and that thin a code could not clear the frame
#: Monte Carlo gate. See RESULTS.md's "dense carrier plan" section for the
#: throughput-vs-rate search this was chosen from.
CORE_SAMPLES = 768
GUARD_SAMPLES = 64
CARRIER_LOW_BIN = 22
CARRIER_HIGH_BIN = 170
CARRIER_BINS = np.arange(CARRIER_LOW_BIN, CARRIER_HIGH_BIN + 1, dtype=np.int64)
CARRIER_COUNT = len(CARRIER_BINS)  # 149

#: Target TX RMS, backed off from full scale so a 72-carrier OFDM symbol's
#: crest factor (observed peak/RMS around 7-7.5x, typical for this many
#: independently modulated carriers) does not clip a float32 soundcard
#: buffer or drive a peak-limited SSB transmitter's ALC into compression.
_TARGET_RMS = 0.115

GEOMETRY = _ofdm.Geometry(
    sample_rate=RX_SAMPLE_RATE,
    core_samples=CORE_SAMPLES,
    guard_samples=GUARD_SAMPLES,
    carrier_bins=CARRIER_BINS,
).scaled_to_rms(_TARGET_RMS)

SYMBOL_SAMPLES = GEOMETRY.symbol_samples  # 396
CARRIER_SPACING_HZ = GEOMETRY.carrier_spacing_hz  # 31.25

# --------------------------------------------------------------------------
# Frame geometry: sync, header/training, and an interleaved payload of data
# and pilot symbols.
# --------------------------------------------------------------------------
SYNC_SYMBOLS = 4
HEADER_SYMBOLS = 3
BITS_PER_CARRIER = 4  # 16-QAM

#: One pilot (full-band, known) symbol after every thirty-six data symbols
#: (three pilots total). The benign/static envelope's 0.005 Hz Doppler
#: spread barely moves the channel across a whole frame (at most a few
#: degrees of phase drift end to end), so pilots are sparse -- spending
#: time, not carrier bandwidth, on tracking, which is why every carrier is
#: available for data on every non-pilot symbol. Thinned from every-12 to
#: every-36 (9 pilots down to 3) as part of the 2026-09-01 guard-interval
#: fix, to recover airtime the longer cyclic prefix below now spends.
PILOT_PERIOD = 36
#: 2026-09-01 FEC fix: raised from 108 to 360 data symbols (still an exact
#: multiple of PILOT_PERIOD, so pilot spacing/density is unchanged -- ten
#: pilots instead of three). A frame Monte Carlo campaign at this length
#: (`logs/mode_qualification/hf-ssb/hf4/2026-09-01-fix/INDEX.md`) found
#: HF4 decoding 0/300 frames at +13 dB and plateauing at 70-83% decoded
#: even at 35 dB waveform SNR: the required benign/static two-path channel
#: puts a handful of the 75 carriers into a deep, frame-static fade often
#: enough that uncoded 16-QAM has no way to recover those carriers' bits,
#: independent of SNR. The fix is an inner FEC layer (below), which needs
#: airtime budget of its own; DATA_SYMBOLS was lengthened so the extra
#: carrier-bit budget from more data symbols pays for that redundancy
#: while keeping net throughput above the 7,000 bit/s floor (see
#: `_derive_fec_sizes` and RESULTS.md for the exact throughput accounting).
#: This does not touch the carrier plan or occupied bandwidth: same 75
#: carriers, same edges, same taper -- only the frame runs longer.
#:
#: 2026-09-01 dense-carrier redesign: lowered from 360 to 288 (still a
#: multiple of PILOT_PERIOD, 8 pilots instead of 10). With 149 carriers
#: (double the old 75) each data symbol now carries roughly double the raw
#: bits, so fewer data symbols are needed to clear the throughput floor even
#: after spending the freed CP-overhead budget on a much stronger FEC rate
#: (8/9 instead of 19/20, below) -- see RESULTS.md's rate/length search.
DATA_SYMBOLS = 108
PILOT_SYMBOLS = DATA_SYMBOLS // PILOT_PERIOD
assert DATA_SYMBOLS % PILOT_PERIOD == 0
PAYLOAD_SYMBOLS = DATA_SYMBOLS + PILOT_SYMBOLS
TOTAL_SYMBOLS = SYNC_SYMBOLS + HEADER_SYMBOLS + PAYLOAD_SYMBOLS

#: Trimmed from 0.128 s as part of the 2026-09-01 guard-interval fix (to
#: recover a little of the airtime the longer cyclic prefix now spends);
#: still comfortably longer than one full sync+header block search window
#: at this guard length.
LEAD_SECONDS = 0.096
TAIL_SECONDS = 0.020
LEAD_SAMPLES = int(round(LEAD_SECONDS * RX_SAMPLE_RATE))
TAIL_SAMPLES = int(round(TAIL_SECONDS * RX_SAMPLE_RATE))
#: Raised-cosine taper applied to the very start/end of the modulated
#: burst (not to every symbol boundary -- the cyclic prefix already makes
#: interior boundaries continuous). This is the leaky-sidelobe control
#: HF2's qualification failure showed is not optional for OFDM on a real
#: SSB radio: without it, energy from the burst's hard edges spreads well
#: outside the carrier band.
EDGE_WINDOW_SAMPLES = 32

TOTAL_CORE_SAMPLES = TOTAL_SYMBOLS * SYMBOL_SAMPLES + 2 * EDGE_WINDOW_SAMPLES
TOTAL_RX_SAMPLES = LEAD_SAMPLES + TOTAL_CORE_SAMPLES + TAIL_SAMPLES
FRAME_SECONDS = TOTAL_RX_SAMPLES / RX_SAMPLE_RATE

LENGTH_BYTES = 2
CRC_BYTES = 4

# --------------------------------------------------------------------------
# Inner FEC: a punctured rate-19/20 K=7 convolutional code
# (`whale.dsp.fec.K7`) plus a block interleaver spreading coded bits across
# every carrier and every data symbol.
#
# 2026-09-01 FEC fix. The prior no-FEC design (see "Why no inner FEC" in
# DESIGN.md) was found, after the guard-interval fix above, to plateau at
# 70-83% frame decode even at 35 dB waveform SNR against the required
# benign/static channel: a two-path model puts a handful of the 75 carriers
# into a deep, frame-static fade often enough that a hard-sliced, uncoded
# 16-QAM carrier has no way to recover, and no amount of clean-channel SNR
# fixes a structurally bad carrier. The fix is a *light* inner code (a
# strong, low-rate code was avoided on purpose -- Level 4 does not need
# Watterson-class margin, and the throughput budget cannot absorb one; see
# RESULTS.md's throughput-vs-rate search): high enough redundancy to give
# the Viterbi decoder scattered, evenly-spread erroneous bits something to
# work with, low enough overhead (5%) to still clear the 7,000 bit/s floor.
# Interleaving is what makes that redundancy effective against this
# specific failure mode: a bad carrier's errors are concentrated on that
# one carrier for the whole frame (the channel is "static" within one
# ~10+ second frame), so spreading the coded bit stream across every
# carrier *and* every data symbol turns one carrier's worth of concentrated
# errors into a small, uniform-looking error rate across the whole coded
# block instead of one dense un-correctable cluster -- exactly the shape a
# convolutional code is good at, and exactly what an uninterleaved code
# would not fix (a bad carrier would still wipe out one contiguous stretch
# of the trellis).
#
# FEC_K/FEC_N is the code rate as a ratio: FEC_K message bits produce
# FEC_N transmitted (post-puncture) bits every period. Puncturing selects
# FEC_N of the mother code's 2*FEC_K bits per period, evenly spaced
# (`_puncture_keep_indices`) rather than using a hand-picked table, so the
# two convolutional output streams are each represented roughly evenly;
# depuncturing reinserts zero-confidence (erasure) soft values at every
# position that was punctured, which `ConvolutionalCode.decode_soft`
# handles natively (a zero-magnitude soft bit carries no information).
#: 2026-09-01 dense-carrier redesign: lowered from 19/20 (0.95) to 8/9
#: (0.889). The old rate was set by the old carrier plan's thin overhead
#: budget (`_derive_fec_sizes` found no rate below ~0.895 could clear
#: 7,000 bit/s at 75 carriers/31.25 Hz spacing), and that thin a code did
#: not survive the +13 dB benign/static frame Monte Carlo campaign
#: (14/300 decoded -- `logs/mode_qualification/hf-ssb/hf4/2026-09-01-fec/
#: INDEX.md`). Doubling carrier density (below) halves the guard interval's
#: relative overhead and frees enough raw-bit budget to afford this
#: substantially stronger code (11.1% redundancy instead of 5%) while still
#: clearing 7,000 bit/s -- see RESULTS.md's "dense carrier plan" throughput/
#: rate search for the exact combination chosen.
FEC_K = 11
FEC_N = 12
FEC_CODE = _fec.K7
FEC_TAIL_BITS = FEC_CODE.tail_bits  # 6, to terminate the trellis at state 0


def _puncture_keep_indices(k: int, n: int) -> np.ndarray:
    """`n` evenly-spaced indices out of the mother code's `2*k` bits.

    Deterministic and rate-exact (`n` distinct indices in `[0, 2*k)`), not
    a hand-tuned puncturing table -- this design's overhead budget is thin
    enough (see the module docstring above) that a hand-tuned table was not
    worth chasing given the empirical, benchmark-driven tuning loop this
    project uses for HF4; even spacing keeps both mother-code output
    streams represented in roughly the same proportion.
    """
    mother = 2 * k
    if not (0 < n <= mother):
        raise ValueError(f"puncture keep count {n} out of range for {mother} bits")
    keep = np.floor(np.linspace(0, mother, n, endpoint=False)).astype(np.int64)
    assert len(set(keep.tolist())) == n
    return keep


PUNCTURE_KEEP = _puncture_keep_indices(FEC_K, FEC_N)
PUNCTURE_MOTHER_BITS = 2 * FEC_K


def _derive_fec_sizes(data_symbols: int, carrier_count: int, bits_per_carrier: int,
                       fec_k: int, fec_n: int, tail_bits: int,
                       length_bytes: int, crc_bytes: int) -> dict:
    """Work out every FEC-related size from the frame geometry, once.

    `RAW_BITS` (the OFDM data-symbol grid's raw bit capacity) must divide
    evenly by `fec_n` so puncturing exactly fills the grid with no leftover
    bits and no gaps -- this is checked, not assumed.
    """
    raw_bits = data_symbols * carrier_count * bits_per_carrier
    if raw_bits % fec_n != 0:
        raise ValueError(
            f"raw bit capacity {raw_bits} is not a multiple of FEC_N={fec_n}")
    periods = raw_bits // fec_n
    encoder_input_bits = periods * fec_k
    message_capacity_bits = encoder_input_bits - tail_bits
    packet_bytes = message_capacity_bits // 8
    pad_bits = message_capacity_bits - packet_bytes * 8
    max_payload_bytes = packet_bytes - length_bytes - crc_bytes
    if max_payload_bytes <= 0:
        raise ValueError("FEC/frame sizing leaves no room for payload")
    return {
        "raw_bits": raw_bits,
        "periods": periods,
        "encoder_input_bits": encoder_input_bits,
        "message_capacity_bits": message_capacity_bits,
        "packet_bytes": packet_bytes,
        "pad_bits": pad_bits,
        "max_payload_bytes": max_payload_bytes,
    }


_FEC_SIZES = _derive_fec_sizes(
    DATA_SYMBOLS, CARRIER_COUNT, BITS_PER_CARRIER, FEC_K, FEC_N,
    FEC_TAIL_BITS, LENGTH_BYTES, CRC_BYTES)
RAW_BITS = _FEC_SIZES["raw_bits"]
FEC_PERIODS = _FEC_SIZES["periods"]
ENCODER_INPUT_BITS = _FEC_SIZES["encoder_input_bits"]
MESSAGE_CAPACITY_BITS = _FEC_SIZES["message_capacity_bits"]
PACKET_BYTES = _FEC_SIZES["packet_bytes"]
PAD_BITS = _FEC_SIZES["pad_bits"]
MAX_PAYLOAD_BYTES = _FEC_SIZES["max_payload_bytes"]

#: Interleaver spreading the RAW_BITS-long punctured coded-bit stream
#: across the (DATA_SYMBOLS, CARRIER_COUNT * BITS_PER_CARRIER) grid so that
#: a single bad carrier's errors, concentrated on that one carrier for the
#: whole frame (this design's failure mode is a frame-*static* per-carrier
#: fade, not a transient one), land on widely separated positions in
#: decode order instead of one contiguous run the Viterbi decoder cannot
#: recover from.
#:
#: 2026-09-01 hardware-debug fix. The original construction here was
#: `whale.dsp.interleave.block(rows=DATA_SYMBOLS, columns=CARRIER_COUNT *
#: BITS_PER_CARRIER)`, combined with an extra transpose in
#: `_to_symbol_grid`/`_from_symbol_grid` that the block interleaver's
#: comment said was required. That transpose was a bug: composing
#: `block()`'s permutation with the extra transpose is provably the
#: identity -- `_to_symbol_grid` reduced exactly to `coded_bits.reshape(
#: DATA_SYMBOLS, CARRIER_COUNT * BITS_PER_CARRIER)`, i.e. *no interleaving
#: at all* (confirmed by direct comparison against a plain reshape; see
#: the 2026-09-01-hardware postmortem in RESULTS.md). That accidental
#: no-op did still spread one fixed carrier-bit lane's coded bits
#: `CARRIER_COUNT * BITS_PER_CARRIER` apart across symbols (matching the
#: one-dead-carrier regression test this module ships, which is why that
#: test did not catch the bug), but it left every *symbol*'s row made up
#: of contiguous, unshuffled source positions -- so any corruption
#: localized to one OFDM symbol in time (an SSB radio's AGC/ALC settling
#: right after the header, a filter transient, anything the frame-static
#: two-path benign/static simulation model does not exercise) lands as one
#: dense, unrecoverable burst in the coded stream instead of scattered
#: single-bit errors. A real IC-7300/IC-705 hardware capture hit exactly
#: this: two independently-faded synced frames both corrupted the packet's
#: length field (the very first symbol's worth of coded bits) to the
#: identical wrong value, which a two-path AWGN fade could not do by
#: chance but a symbol-0-localized burst, decoded the same way both times
#: because the interleaver did nothing to break it up, can. The fix swaps
#: in `whale.dsp.interleave.multiplicative` (the same interleaver
#: construction VF2 through VF5 use, with the same stride), which scatters
#: bits across *both* carriers and symbols simultaneously -- no fixed
#: symbol row or fixed carrier column maps back to a contiguous run of the
#: coded stream -- protecting against a bad symbol and a bad carrier alike.
_INTERLEAVE_COLUMNS = CARRIER_COUNT * BITS_PER_CARRIER
#: Stride shared with VF2-VF5's own multiplicative interleavers; checked
#: coprime with RAW_BITS at import time by `_interleave.multiplicative`
#: itself (it raises otherwise), not assumed.
_INTERLEAVE_STRIDE = 8101
INTERLEAVER = _interleave.multiplicative(RAW_BITS, _INTERLEAVE_STRIDE)


def _to_symbol_grid(coded_bits: np.ndarray) -> np.ndarray:
    """Punctured coded-bit stream -> `(DATA_SYMBOLS, CARRIER_COUNT *
    BITS_PER_CARRIER)`, via `INTERLEAVER.spread` and a plain reshape (the
    multiplicative interleaver's permutation already scatters bits across
    both grid axes; no extra transpose is needed or correct here -- see
    `INTERLEAVER`'s comment for why the old block-plus-transpose
    construction was a no-op bug)."""
    return INTERLEAVER.spread(coded_bits).reshape(DATA_SYMBOLS, _INTERLEAVE_COLUMNS)


def _from_symbol_grid(grid: np.ndarray) -> np.ndarray:
    """Inverse of `_to_symbol_grid`, for hard bits or soft reliabilities
    alike (both are plain arrays to `Interleaver.gather`)."""
    return INTERLEAVER.gather(np.asarray(grid).reshape(-1))

#: Acquisition confidence gate. `whale.dsp.acquire` reports a normalized
#: self-correlation score of the repeated sync block; this is the same
#: proposal-scale threshold the shared library defaults to.
ACQUISITION_THRESHOLD = 0.60

DEFAULT_HEAD_SECONDS = LEAD_SECONDS

# --------------------------------------------------------------------------
# Known constellations (sync / header / pilot). Deterministic PN-derived
# QPSK patterns: fixed, nonzero seeds, all distinct.
# --------------------------------------------------------------------------
_SYNC_SEED = 0x1ACE9
_HEADER_BASE_SEED = 0x20001
_PILOT_SEED = 0x30009
_WHITENER_SEED = 0x4B0B1


def _known_qpsk(seed: int) -> np.ndarray:
    return qpsk_from_bits(pn_bits(2 * CARRIER_COUNT, seed))


SYNC_VALUES = _known_qpsk(_SYNC_SEED)
#: Each header row is the same per-carrier base value at a distinct known
#: rotation. A per-carrier two-parameter (gain, offset) least-squares fit
#: needs the reference points it is given to be non-degenerate; drawing
#: each row independently from a 4-point QPSK alphabet risks two of only a
#: few header rows landing on the same constellation point for a given
#: carrier by pure chance, which makes that carrier's fit singular.
#: Rotating a single base value by even angles around the circle instead
#: guarantees every row differs, for every carrier, deterministically.
_HEADER_BASE = _known_qpsk(_HEADER_BASE_SEED)
_HEADER_ROTATIONS = np.exp(2j * np.pi * np.arange(HEADER_SYMBOLS) / HEADER_SYMBOLS)
HEADER_VALUES = _HEADER_BASE[None, :] * _HEADER_ROTATIONS[:, None]
PILOT_VALUES = _known_qpsk(_PILOT_SEED)
_WHITENER = pn_bits(PACKET_BYTES * 8, _WHITENER_SEED)


def _payload_layout() -> list[tuple[str, int | None]]:
    layout: list[tuple[str, int | None]] = []
    data_index = 0
    for i in range(DATA_SYMBOLS):
        layout.append(("data", data_index))
        data_index += 1
        if (i + 1) % PILOT_PERIOD == 0:
            layout.append(("pilot", None))
    return layout


PAYLOAD_LAYOUT = _payload_layout()
assert len(PAYLOAD_LAYOUT) == PAYLOAD_SYMBOLS
PILOT_INDICES = np.array(
    [i for i, (kind, _) in enumerate(PAYLOAD_LAYOUT) if kind == "pilot"],
    dtype=np.int64)
DATA_ROWS = np.array(
    [i for i, (kind, _) in enumerate(PAYLOAD_LAYOUT) if kind == "data"],
    dtype=np.int64)
assert len(PILOT_INDICES) == PILOT_SYMBOLS
assert len(DATA_ROWS) == DATA_SYMBOLS

# --------------------------------------------------------------------------
# 16-QAM Gray mapping.  LEVELS is the natural amplitude order; BIT_PAIRS is
# the Gray-coded bit pair for each LEVELS index, so adjacent amplitude
# levels differ by exactly one bit -- a slicing error moves to a neighbour
# amplitude and typically costs one bit, not two.
# --------------------------------------------------------------------------
LEVELS = np.array([-3.0, -1.0, 1.0, 3.0]) / np.sqrt(10.0)
BIT_PAIRS = np.array([[0, 0], [0, 1], [1, 1], [1, 0]], dtype=np.uint8)
#: FORWARD_ARR[natural_2bit_value] -> index into LEVELS/BIT_PAIRS.
_FORWARD_ARR = np.array([0, 1, 3, 2], dtype=np.int64)


def bits_to_16qam(bits: np.ndarray) -> np.ndarray:
    """`(..., 4)` bits `[I_msb, I_lsb, Q_msb, Q_lsb]` -> complex values."""
    bits = np.asarray(bits, dtype=np.uint8)
    i_natural = (bits[..., 0].astype(np.int64) << 1) | bits[..., 1].astype(np.int64)
    q_natural = (bits[..., 2].astype(np.int64) << 1) | bits[..., 3].astype(np.int64)
    i_level = LEVELS[_FORWARD_ARR[i_natural]]
    q_level = LEVELS[_FORWARD_ARR[q_natural]]
    return i_level + 1j * q_level


def qam16_to_bits(values: np.ndarray) -> np.ndarray:
    """Nearest-neighbour hard slice; inverse of `bits_to_16qam`."""
    values = np.asarray(values)
    real_idx = np.argmin(
        np.abs(values.real[..., None] - LEVELS[None, :]), axis=-1)
    imag_idx = np.argmin(
        np.abs(values.imag[..., None] - LEVELS[None, :]), axis=-1)
    return np.concatenate(
        (BIT_PAIRS[real_idx], BIT_PAIRS[imag_idx]), axis=-1)


#: Boundary between the "inner" (+-1/sqrt(10)) and "outer" (+-3/sqrt(10))
#: pairs of levels -- the midpoint an axis's LSB soft metric is measured
#: against (see `_soft_axis_bits` below).
_LSB_BOUNDARY = (1.0 + 3.0) / (2.0 * np.sqrt(10.0))


def _soft_axis_bits(axis: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Approximate Gray-coded soft (MSB, LSB) metrics for one PAM axis.

    `LEVELS`/`BIT_PAIRS` place the sign of the axis value entirely in the
    MSB (both negative levels carry MSB=0, both positive carry MSB=1) and
    the inner-vs-outer pair entirely in the LSB (the two outer levels
    +-3/sqrt(10) carry LSB=0, the two inner levels +-1/sqrt(10) carry
    LSB=1). That makes a per-bit soft metric cheap and exact in shape (if
    not a calibrated LLR): positive means "bit is 0", by
    `ConvolutionalCode.decode_soft`'s convention.
    """
    msb_soft = -axis
    lsb_soft = np.abs(axis) - _LSB_BOUNDARY
    return msb_soft, lsb_soft


def soft_16qam_bits(values: np.ndarray) -> np.ndarray:
    """Soft (I_msb, I_lsb, Q_msb, Q_lsb) metrics; soft-decision counterpart
    of `qam16_to_bits`, for the inner convolutional code's Viterbi decoder."""
    values = np.asarray(values)
    i_msb, i_lsb = _soft_axis_bits(values.real)
    q_msb, q_lsb = _soft_axis_bits(values.imag)
    return np.stack((i_msb, i_lsb, q_msb, q_lsb), axis=-1)


# --------------------------------------------------------------------------
# Packet framing: 16-bit big-endian length, payload, CRC32, whitened.
# --------------------------------------------------------------------------

def _build_packet(payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"HF4 payload is {len(payload)} bytes; the maximum is "
            f"{MAX_PAYLOAD_BYTES}")
    packet = bytearray(PACKET_BYTES)
    packet[0:LENGTH_BYTES] = len(payload).to_bytes(LENGTH_BYTES, "big")
    packet[LENGTH_BYTES:LENGTH_BYTES + len(payload)] = payload
    crc_at = LENGTH_BYTES + len(payload)
    packet[crc_at:crc_at + CRC_BYTES] = (
        binascii.crc32(payload) & 0xFFFFFFFF).to_bytes(CRC_BYTES, "big")
    return bytes(packet)


def _puncture(mother_bits: np.ndarray) -> np.ndarray:
    """Keep `PUNCTURE_KEEP` of every `PUNCTURE_MOTHER_BITS`-bit period."""
    grid = mother_bits.reshape(-1, PUNCTURE_MOTHER_BITS)
    return grid[:, PUNCTURE_KEEP].reshape(-1)


def _depuncture_to_soft(kept_soft: np.ndarray) -> np.ndarray:
    """Inverse of `_puncture`, for soft values: zero (no information, an
    erasure) at every position that was not transmitted."""
    periods = len(kept_soft) // FEC_N
    grid = np.zeros((periods, PUNCTURE_MOTHER_BITS), dtype=np.float64)
    grid[:, PUNCTURE_KEEP] = kept_soft.reshape(periods, FEC_N)
    return grid.reshape(-1)


def _packet_to_data_values(packet: bytes) -> np.ndarray:
    """`PACKET_BYTES`-long packet -> `(DATA_SYMBOLS, CARRIER_COUNT)` 16-QAM
    values, through whitening, convolutional coding, puncturing, and
    interleaving. Factored out of `_build_burst` so tests can inject a
    corrupted packet through the exact same coding path the real encoder
    uses (see `test_hf4.py`)."""
    packet_bits = np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
    whitened = (packet_bits ^ _WHITENER).astype(np.uint8)
    encoder_input = np.concatenate((
        whitened,
        np.zeros(PAD_BITS, dtype=np.uint8),
        np.zeros(FEC_TAIL_BITS, dtype=np.uint8),
    ))
    assert len(encoder_input) == ENCODER_INPUT_BITS
    mother_bits = FEC_CODE.encode(encoder_input)
    coded_bits = _puncture(mother_bits)
    assert len(coded_bits) == RAW_BITS
    grid_bits = _to_symbol_grid(coded_bits).reshape(
        DATA_SYMBOLS, CARRIER_COUNT, BITS_PER_CARRIER)
    return bits_to_16qam(grid_bits)


def _data_values_to_packet_bits(data_values: np.ndarray,
                                carrier_weights: np.ndarray | None = None
                                ) -> np.ndarray:
    """Inverse of `_packet_to_data_values`'s coding chain, from soft 16-QAM
    axis metrics through de-interleaving, depuncturing, and Viterbi
    decoding, back to the (still-whitened) packet bits.

    `carrier_weights`, when given, is a `(CARRIER_COUNT,)` per-carrier
    reliability multiplier (see `whale.dsp.equalize.carrier_weights`)
    applied to every soft bit before decoding: a carrier the header fit
    found to be in a deep fade contributes low-confidence (near-erasure)
    soft bits instead of full-confidence ones that happen to be wrong,
    which is what actually lets the code correct a bad carrier -- see the
    comment on `INTERLEAVER` and `test_one_dead_carrier_still_decodes`.
    """
    soft_grid = soft_16qam_bits(data_values)  # (DATA_SYMBOLS, CARRIER_COUNT, 4)
    if carrier_weights is not None:
        soft_grid = soft_grid * np.asarray(carrier_weights)[None, :, None]
    interleaved_soft = soft_grid.reshape(DATA_SYMBOLS, RAW_BITS // DATA_SYMBOLS)
    coded_soft = _from_symbol_grid(interleaved_soft)
    mother_soft = _depuncture_to_soft(coded_soft)
    decoded = FEC_CODE.decode_soft(mother_soft)
    message = decoded[:MESSAGE_CAPACITY_BITS]  # drop the FEC_TAIL_BITS termination
    whitened = message[:len(_WHITENER)]  # drop the PAD_BITS byte-alignment pad
    return (whitened ^ _WHITENER).astype(np.uint8)


def _apply_edge_window(core_audio: np.ndarray) -> np.ndarray:
    """Taper the burst's on-air edges without touching a single decoded bit.

    The straightforward way to fight OFDM edge splatter -- multiply the
    first/last samples of the modulated block by a raised-cosine ramp --
    corrupts exactly those samples for anyone trying to decode them,
    because they are the tail of the first/last real symbol's own core.
    Instead, a tapered *copy* of the first and last `EDGE_WINDOW_SAMPLES`
    is prepended/appended around the untouched core audio: the burst still
    ramps smoothly up out of, and back down into, silence, but every
    sample a decoder analyzes is exactly what `build_symbol` produced.
    """
    n = EDGE_WINDOW_SAMPLES
    ramp_in = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, n)))
    prefix = core_audio[:n] * ramp_in
    suffix = core_audio[-n:] * ramp_in[::-1]
    return np.concatenate((prefix, core_audio, suffix))


def _build_burst(payload: bytes) -> np.ndarray:
    """The RX-rate (12 kHz) real burst: lead silence, symbols, tail silence."""
    packet = _build_packet(bytes(payload))
    data_values = _packet_to_data_values(packet)  # (DATA_SYMBOLS, CARRIER_COUNT)

    symbols: list[np.ndarray] = []
    symbols.extend(SYNC_VALUES.copy() for _ in range(SYNC_SYMBOLS))
    symbols.extend(HEADER_VALUES[i] for i in range(HEADER_SYMBOLS))
    for kind, index in PAYLOAD_LAYOUT:
        symbols.append(data_values[index] if kind == "data" else PILOT_VALUES)

    core_chunks = [_ofdm.build_symbol(GEOMETRY, values) for values in symbols]
    core_audio = np.concatenate(core_chunks)
    core_audio = _apply_edge_window(core_audio)

    return np.concatenate((
        np.zeros(LEAD_SAMPLES, dtype=np.float64),
        core_audio,
        np.zeros(TAIL_SAMPLES, dtype=np.float64),
    ))


def modulate(payload: bytes) -> np.ndarray:
    """Encode `payload` (<= MAX_PAYLOAD_BYTES) to 48 kHz real TX audio."""
    burst = _build_burst(payload)
    tx_audio = resample_poly(burst, _TX_UPSAMPLE, 1)
    return tx_audio.astype(np.float32)


def lead_in_samples() -> int:
    """RX-rate samples of intentional silence at the very start of a frame."""
    return LEAD_SAMPLES


def demodulate(audio: np.ndarray, **_ignored) -> dict:
    """Decode 12 kHz real RX audio, returning the common decode-result dict."""
    audio = np.asarray(audio)
    if (audio.ndim != 1 or audio.size == 0
            or not np.issubdtype(audio.dtype, np.number)
            or not np.all(np.isfinite(audio))):
        return {"synced": False, "payload": None, "confidence": 0.0}
    audio = audio.astype(np.float64)

    analytic = hilbert(audio)
    start, score = _acquire.acquire(
        GEOMETRY, analytic, sync_symbols=SYNC_SYMBOLS,
        proposal_threshold=ACQUISITION_THRESHOLD)
    result = {"synced": False, "payload": None, "confidence": float(score or 0.0),
              "start_index": start}
    if start is None or score < ACQUISITION_THRESHOLD:
        return result

    symbol_samples = GEOMETRY.symbol_samples
    header_start = start + SYNC_SYMBOLS * symbol_samples
    trailing_symbols = HEADER_SYMBOLS + PAYLOAD_SYMBOLS
    if header_start < 0 or (header_start + trailing_symbols * symbol_samples
                             > len(analytic)):
        return result

    # No per-symbol sample-clock tracking: this design's target envelope
    # (benign/static, SPEED_LADDERS.md) has negligible drift over one
    # ~4-second frame, acquisition already locates the symbol boundary to
    # the sample, and this waveform's 12-sample cyclic prefix is too short
    # for `whale.dsp.timing`'s search window (tuned for the much longer
    # guards other HF modes use) to fit a reliable per-symbol shift from --
    # trying to anyway measurably *hurt* decode accuracy in testing rather
    # than helping it. Carrier-frequency offset is still corrected: it is
    # a per-frame constant this waveform's own header can estimate cleanly
    # regardless of guard length.
    coarse_hz = _freq.coarse_offset_hz(
        GEOMETRY, analytic, header_start, np.arange(HEADER_SYMBOLS))
    derotated = _freq.derotate(analytic, coarse_hz, GEOMETRY.sample_rate)

    bank = _ofdm.carrier_bank(GEOMETRY, derotated, header_start, trailing_symbols)
    if bank is None:
        return result
    fine_hz = _freq.fine_offset_hz(GEOMETRY, bank[:HEADER_SYMBOLS], HEADER_VALUES)
    total_hz = coarse_hz + fine_hz
    derotated = _freq.derotate(analytic, total_hz, GEOMETRY.sample_rate)

    bank = _ofdm.carrier_bank(GEOMETRY, derotated, header_start, trailing_symbols)
    if bank is None:
        return result
    header_bank = bank[:HEADER_SYMBOLS]
    payload_bank = bank[HEADER_SYMBOLS:]

    channel = _equalize.fit_header(header_bank, HEADER_VALUES)
    result["carrier_snr_db"] = channel.snr_db.tolist()
    result["carriers_present"] = channel.present_carriers()
    result["freq_offset_hz"] = float(total_hz)

    equalized_payload = channel.equalize(payload_bank)
    equalized_header = channel.equalize(header_bank)

    corrected, _phase = _equalize.pilot_phase(
        equalized_payload, PILOT_INDICES,
        np.tile(PILOT_VALUES, (PILOT_SYMBOLS, 1)),
        equalized_header[-1], HEADER_VALUES[-1])

    data_values = corrected[DATA_ROWS]
    # 2026-09-01 dense-carrier redesign: tightened from low=0.05. The
    # stronger rate-8/9 code (see FEC_K/FEC_N) is more sensitive to a
    # "confidently wrong" carrier than the old rate-19/20 code was -- a
    # dead carrier weighted at 0.05 occasionally still misled the Viterbi
    # metric enough to fail (found by test_one_dead_carrier_still_decodes
    # during this redesign); 0.02 leaves comfortably more margin.
    weights = _equalize.carrier_weights(channel.snr_db, low=0.02, high=4.0)
    packet_bits = _data_values_to_packet_bits(data_values, carrier_weights=weights)
    packet_bytes = np.packbits(packet_bits).tobytes()

    length = int.from_bytes(packet_bytes[0:LENGTH_BYTES], "big")
    result["synced"] = True
    result["decoded_length"] = length
    if length > MAX_PAYLOAD_BYTES:
        result["crc_ok"] = False
        return result
    payload = packet_bytes[LENGTH_BYTES:LENGTH_BYTES + length]
    crc_at = LENGTH_BYTES + length
    received_crc = int.from_bytes(packet_bytes[crc_at:crc_at + CRC_BYTES], "big")
    computed_crc = binascii.crc32(payload) & 0xFFFFFFFF
    result["crc_ok"] = received_crc == computed_crc
    result["payload"] = payload if result["crc_ok"] else None
    return result


def airtime(payload_len: int | None = None) -> float:
    del payload_len  # HF4 is a fixed-length frame like the project's other OFDM modes
    return FRAME_SECONDS
