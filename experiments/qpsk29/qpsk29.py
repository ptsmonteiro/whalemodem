"""A 29-carrier QPSK OFDM frame: 214 symbols, 5.200 s, 512-periodic symbols.

This is a from-scratch implementation of a handed-down frame specification. It
deliberately shares no DSP with experiments/ofdm/ and does not obey the shipped
modem's 3.0 s keying cap -- one frame keys the transmitter for 5.2 s.

    | parameter          | value                                          |
    |--------------------|------------------------------------------------|
    | frame duration     | 5.200 s (45 ms lead-in + 214 symbols + tail)   |
    | symbol period      | 1152 samples @ 48 kHz = 24 ms, 41.667 sym/s    |
    | guard interval     | 128 samples = 2.667 ms                         |
    | modulation interval| 512 samples = 10.667 ms, sent twice per symbol |
    | carrier spacing    | 93.75 Hz (= 1 / 10.667 ms)                     |
    | carriers           | 29, 468.75 Hz ... 3093.75 Hz inclusive         |
    | constellation      | QPSK, constant modulus, every carrier          |
    | header             | symbols 0-14 (360 ms), unscrambled coherent    |
    | payload            | symbols 15-213 (199 symbols)                   |
    | gross rate         | 29 x 41.667 x 2 = 2416.7 bit/s                 |

The symbol is one continuous 512-sample-periodic waveform, 2.25 periods long:

    [ 128 guard ][ 512 core ][ 512 core ]   = 1152 samples = 24 ms
       2.667 ms    10.667 ms   10.667 ms

Only 44% of the symbol time carries new information. The band could hold about
58 carriers at this symbol rate and we use 29. What the repeat buys is timing
tolerance and a free frequency-offset estimator.

Two consequences of that structure drive the whole receiver, and getting them
backwards is the easiest way to build something that almost works:

  * The guard and the placement window are ONE budget, not two. With a channel
    impulse response of length L, a single 512-point FFT at offset d is clean
    for d in [L, 640] -- delay spread eats timing tolerance one-for-one. The
    prefix's job is to widen that window from 512 to 640, not to serve as a
    separate ISI guard beside it.

  * The 3 dB combining gain and the 640-sample window CANNOT both be had.
    Averaging both cores needs windows at d and d+512 to both be clean, i.e.
    d + 1024 <= 1152, so d in [L, 128]. Combining collapses the placement
    freedom from 640 samples to 128. Small d is the normal operating point;
    the 640-sample window is graceful degradation for poor acquisition, not
    where the receiver should live. See COMBINE_MAX_OFFSET.

Note that "128 guard + 1024 useful with every other bin nulled" and "128 guard
+ 512 core sent twice" generate bit-identical samples, so a capture cannot
distinguish the two descriptions.

The 45 ms lead-in plus 214 symbols is 248 688 samples = 5.181 s, which is
short of the specified 5.195-5.205 s. The 912-sample (19 ms) ramp-down and
settle tail closes that to exactly 249 600 samples = 5.200 s. That is our
choice for what fills the gap, not something the specification stated.

Run:
    python experiments/qpsk29/test_qpsk29.py
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import binascii
import sys

import numpy as np
from scipy.signal import hilbert

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ldpc  # noqa: E402  (sibling module, see sys.path above)

# -- waveform geometry -----------------------------------------------------

SAMPLE_RATE = 48_000

CORE_SAMPLES = 512            # the 512-periodic modulation interval
GUARD_SAMPLES = 128           # cyclic prefix
SYMBOL_SAMPLES = 1152         # 128 + 512 + 512, one 2.25-period slice

CARRIER_SPACING = SAMPLE_RATE / CORE_SAMPLES        # 93.75 Hz
FIRST_BIN, LAST_BIN = 5, 33
CARRIER_BINS = tuple(range(FIRST_BIN, LAST_BIN + 1))  # 29 carriers
N_CARRIERS = len(CARRIER_BINS)
CARRIER_HZ = tuple(b * CARRIER_SPACING for b in CARRIER_BINS)  # 468.75 .. 3093.75

BITS_PER_CARRIER = 2          # QPSK
BITS_PER_SYMBOL = N_CARRIERS * BITS_PER_CARRIER     # 58

HEADER_SYMBOLS = 15           # symbols 0..14, unscrambled coherent QPSK
PAYLOAD_SYMBOLS = 199         # symbols 15..213
TOTAL_SYMBOLS = HEADER_SYMBOLS + PAYLOAD_SYMBOLS    # 214

LEAD_IN_SAMPLES = 2_160       # 45 ms
TAIL_SAMPLES = 912            # 19 ms; see module docstring
FRAME_SAMPLES = LEAD_IN_SAMPLES + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE          # 5.200 s exactly

SYMBOL_RATE = SAMPLE_RATE / SYMBOL_SAMPLES           # 41.667 symbol/s
GROSS_BITRATE = BITS_PER_SYMBOL * SYMBOL_RATE        # 2416.7 bit/s

# -- FFT window placement --------------------------------------------------
#
# A single-core FFT is clean for offset in [L, SINGLE_MAX_OFFSET]. Combining
# both cores additionally requires offset <= COMBINE_MAX_OFFSET, because the
# second window must also fit inside the symbol. These are the two numbers the
# receiver trades against each other; see the module docstring.

SINGLE_MAX_OFFSET = SYMBOL_SAMPLES - CORE_SAMPLES            # 640
COMBINE_MAX_OFFSET = SYMBOL_SAMPLES - 2 * CORE_SAMPLES       # 128

# Where we aim when acquisition is good: far enough in to absorb a plausible
# audio-filter impulse response, far enough from COMBINE_MAX_OFFSET to absorb
# timing error in the other direction, and still inside the combining window.
DEFAULT_OFFSET = 64

# -- payload geometry ------------------------------------------------------

PAYLOAD_BITS = PAYLOAD_SYMBOLS * BITS_PER_SYMBOL     # 11 542

# Exactly this many LDPC codewords, always, at every rate. N is fixed at 648
# and only INFORMATION_BITS[rate] changes, so frame geometry, interleaver and
# decode loop are identical across 1/2, 2/3 and 3/4.
#
# This is the whole reason the prior experiment's LDPC cost problem does not
# apply here. There, the de-interleaver needed the codeword count before it
# could run, but the count came from a length field living inside the first
# codeword -- so the receiver brute-forced every plausible count, and a wrong
# guess was the MOST expensive case because it never cleared the syndrome and
# burned all 30 min-sum iterations before failing. Here the count is a
# compile-time constant: one hypothesis, no search, and the 16-bit length
# field is purely informational rather than a search key.
CODEWORDS = PAYLOAD_BITS // ldpc.N                   # 17
CODED_BITS = CODEWORDS * ldpc.N                      # 11 016

# What is left over after the codewords. Rather than transmit these as zeros,
# the interleaver scatters them across the time/frequency grid and they serve
# as known references for the phase tracker -- free scattered pilots on a
# waveform that specifies none. 4.6% of the payload symbols.
PAD_BITS = PAYLOAD_BITS - CODED_BITS                 # 526
PILOT_SYMBOLS = PAD_BITS // BITS_PER_CARRIER         # 263

LENGTH_FIELD_BITS = 16
CRC_BITS = 16
OVERHEAD_BITS = LENGTH_FIELD_BITS + CRC_BITS         # 32

FEC_RATES = ("1/2", "2/3", "3/4")

# Order-17 PN whitener over the payload field. Not FEC -- it exists so a
# payload of repeated bytes does not become a low-PAPR periodic waveform, and
# so a run of zeros cannot look like silence to the acquisition metric.
WHITENING_ORDER = 17
WHITENING_TAPS = (1, 15)
WHITENING_SEED = 1

# Header values are a fixed constant sequence, NOT whitened payload -- that is
# what "unscrambled" means in the specification. The receiver correlates
# against it for timing and divides by it for the channel estimate, so it must
# be identical at both ends and must never depend on the payload.
HEADER_SEED = 0x1ACE

CONFIDENCE_THRESHOLD = 0.6

# Longest capture we will search for a frame. Keeps a long RX buffer from
# turning acquisition into an unbounded cost.
MAX_SEARCH_SECONDS = 12.0


def information_bits(fec):
    """Information bits carried by the 17 codewords at this rate.

    `fec` is None (uncoded) or one of FEC_RATES.
    """
    if fec is None:
        return PAYLOAD_BITS
    return CODEWORDS * ldpc.INFORMATION_BITS[fec]


def max_payload(fec):
    """Largest payload in bytes at this rate, after length field and CRC."""
    return (information_bits(fec) - OVERHEAD_BITS) // 8


@dataclass(frozen=True)
class Qpsk29Profile:
    """One configuration of the frame. The waveform geometry is fixed by the
    specification and lives in module constants; only these knobs vary."""

    name: str = "qpsk29"
    fec: str | None = "2/3"        # None (diagnostic) or one of FEC_RATES
    papr_db: float | None = 9.0    # None disables transmit clipping
    amplitude: float = 0.9
    confidence_threshold: float = CONFIDENCE_THRESHOLD

    def __post_init__(self):
        if self.fec is not None and self.fec not in FEC_RATES:
            raise ValueError(f"unknown fec {self.fec!r}; have {FEC_RATES} or None")

    @property
    def max_payload(self):
        return max_payload(self.fec)

    @property
    def information_bits(self):
        return information_bits(self.fec)

    @property
    def payload_bitrate(self):
        """Net user bit/s over the whole 5.2 s keying."""
        return self.max_payload * 8 / FRAME_SECONDS

    @property
    def frame_seconds(self):
        return FRAME_SECONDS


DEFAULT = Qpsk29Profile()
UNCODED = Qpsk29Profile(name="qpsk29-uncoded", fec=None)


# -- interleaver -----------------------------------------------------------
#
# One constant permutation, built once at import. It carries two jobs:
#
#   1. Spread every codeword across the complete time/frequency grid, so a
#      dead carrier or a squelch blackout damages all 17 codewords a little
#      instead of destroying one outright.
#   2. Scatter the PAD_BITS pilots in both time and frequency.
#
# Both are tested in test_qpsk29.py; neither is safe to assume.


@lru_cache(maxsize=1)
def interleave_map():
    """Grid position -> source bit index. Shape (PAYLOAD_BITS,), int32.

    Source bit indices [0, CODED_BITS) are codeword bits, laid out codeword-
    major: index c * ldpc.N + i is bit i of codeword c. Indices
    [CODED_BITS, PAYLOAD_BITS) are the pad/pilot bits.
    """
    # Preserve QPSK bit pairs.  The final 526 source bits are 263 complete
    # known QPSK points; splitting either bit away from its mate would turn a
    # scattered pilot into a half-known data point.  The coded symbols use a
    # fixed random permutation while the pilots use a balanced lattice.
    rng = np.random.default_rng(0x29_4A_17)
    qpsk_symbols = PAYLOAD_BITS // BITS_PER_CARRIER
    coded_symbols = CODED_BITS // BITS_PER_CARRIER
    source_symbols = np.empty(qpsk_symbols, dtype=np.int32)
    # Place known points at an even cadence in the row-major grid.  Because
    # that cadence is not a divisor of 29, it walks across carriers as well
    # as time.  The original fully-random placement left one carrier with no
    # pilot after payload symbol 70; on the first five-second HF capture its
    # clock-drift phase then ran 1.6 rad beyond the last anchor.
    pilot_grid = np.floor((np.arange(PILOT_SYMBOLS) + 0.5)
                          * qpsk_symbols / PILOT_SYMBOLS).astype(np.int32)
    source_symbols[pilot_grid] = np.arange(coded_symbols, qpsk_symbols)
    data_grid = np.setdiff1d(np.arange(qpsk_symbols), pilot_grid,
                             assume_unique=True)
    source_symbols[data_grid] = rng.permutation(coded_symbols)
    mapping = np.empty(PAYLOAD_BITS, dtype=np.int32)
    mapping[0::2] = 2 * source_symbols
    mapping[1::2] = 2 * source_symbols + 1
    mapping.flags.writeable = False
    return mapping


@lru_cache(maxsize=1)
def codeword_of_grid_bit():
    """Which codeword each grid position carries. Shape (PAYLOAD_BITS,), int8.

    -1 marks a pad/pilot position.
    """
    source = interleave_map()
    out = np.where(source < CODED_BITS, source // ldpc.N, -1).astype(np.int8)
    out.flags.writeable = False
    return out


@lru_cache(maxsize=1)
def pilot_positions():
    """Grid indices carrying known pilot bits. Shape (PAD_BITS,), int32."""
    out = np.flatnonzero(interleave_map() >= CODED_BITS).astype(np.int32)
    out.flags.writeable = False
    return out


@lru_cache(maxsize=1)
def pilot_bits():
    """The known pilot bit values, aligned with pilot_positions(). (PAD_BITS,)."""
    bits = _pn_bits(PAD_BITS, 0x15555)
    # pilot_positions() is in grid order, while the PN sequence is naturally
    # indexed in source order.
    source = interleave_map()[pilot_positions()] - CODED_BITS
    out = bits[source]
    out.flags.writeable = False
    return out


# -- symbol level ----------------------------------------------------------


@lru_cache(maxsize=1)
def header_values():
    """The fixed, unscrambled header constellation. Shape (15, 29), complex.

    Constant for the life of the mode. Both ends must agree exactly.
    """
    bits = _pn_bits(HEADER_SYMBOLS * BITS_PER_SYMBOL, HEADER_SEED)
    out = _qpsk(bits).reshape(HEADER_SYMBOLS, N_CARRIERS)
    out.flags.writeable = False
    return out


def build_symbol(values):
    """One 1152-sample symbol from 29 complex carrier values.

    Fills a 512-point spectrum at CARRIER_BINS, Hermitian-mirrors it, inverse
    transforms to a real 512-sample core, then returns

        concat(core[-128:], core, core)

    which is a contiguous slice of the infinite 512-periodic extension of
    `core` -- so block[n] == core[(n - 128) % 512] for all 1152 samples, and
    the guard is a true cyclic prefix rather than a separately-generated pad.
    """
    values = np.asarray(values, dtype=np.complex128).reshape(-1)
    if values.shape != (N_CARRIERS,):
        raise ValueError(f"expected {N_CARRIERS} carrier values")
    spectrum = np.zeros(CORE_SAMPLES, dtype=np.complex128)
    spectrum[np.asarray(CARRIER_BINS)] = values
    spectrum[-np.asarray(CARRIER_BINS)] = np.conj(values)
    # Unit-energy carrier values produce a unit-RMS real core.  The complete
    # keying is peak-normalised after deliberate PAPR clipping in modulate().
    core = np.fft.ifft(spectrum).real * np.sqrt(CORE_SAMPLES / (2 * N_CARRIERS))
    return np.concatenate((core[-GUARD_SAMPLES:], core, core))


def symbol_carriers(symbol_audio, offset=DEFAULT_OFFSET, combine=True):
    """Recover 29 complex carrier values from one symbol's worth of audio.

    `offset` places the 512-point FFT window inside the symbol. `combine`
    averages both cores in the time domain for ~3 dB against independent
    noise, which requires the second window to fit too.

    Raises ValueError if offset is outside [0, SINGLE_MAX_OFFSET], or if
    combine is set and offset exceeds COMBINE_MAX_OFFSET -- silently
    returning garbage there is the failure mode this check exists to prevent.

    The caller is responsible for having corrected carrier frequency offset
    BEFORE calling with combine=True: the two cores are separated by 512
    samples, so an uncorrected offset rotates them apart and averaging them
    destroys the gain it was meant to provide.
    """
    if not 0 <= offset <= SINGLE_MAX_OFFSET:
        raise ValueError(f"offset must be in [0, {SINGLE_MAX_OFFSET}]")
    if combine and offset > COMBINE_MAX_OFFSET:
        raise ValueError(f"two-core combining needs offset <= {COMBINE_MAX_OFFSET}")
    audio = np.asarray(symbol_audio)
    if len(audio) < SYMBOL_SAMPLES:
        raise ValueError(f"a complete {SYMBOL_SAMPLES}-sample symbol is required")
    core = audio[offset:offset + CORE_SAMPLES]
    if combine:
        core = 0.5 * (core + audio[offset + CORE_SAMPLES:
                                 offset + 2 * CORE_SAMPLES])
    spectrum = np.fft.fft(core)
    values = spectrum[np.asarray(CARRIER_BINS)]
    core_index = (offset - GUARD_SAMPLES) % CORE_SAMPLES
    undo_shift = np.exp(-2j * np.pi * np.asarray(CARRIER_BINS) * core_index
                        / CORE_SAMPLES)
    return (values * undo_shift
            / np.sqrt(CORE_SAMPLES / (2 * N_CARRIERS)))


# -- packet, constellation and receiver helpers ---------------------------


def _pn_bits(count, seed):
    state = int(seed) & ((1 << WHITENING_ORDER) - 1)
    if state == 0:
        raise ValueError("PN seed must be non-zero")
    out = np.empty(count, dtype=np.uint8)
    for i in range(count):
        out[i] = state & 1
        feedback = ((state >> (WHITENING_TAPS[0] - 1))
                    ^ (state >> (WHITENING_TAPS[1] - 1))) & 1
        state = (state >> 1) | (feedback << (WHITENING_ORDER - 1))
    return out


def _qpsk(bits):
    bits = np.asarray(bits, dtype=np.uint8).reshape(-1, 2)
    return ((1.0 - 2.0 * bits[:, 0])
            + 1j * (1.0 - 2.0 * bits[:, 1])) / np.sqrt(2.0)


def _hard_bits(values):
    values = np.asarray(values)
    return np.stack((values.real < 0.0, values.imag < 0.0), axis=-1).astype(
        np.uint8).reshape(-1)


def _crc16(payload):
    return binascii.crc_hqx(bytes(payload), 0xFFFF)


def _information_packet(payload, capacity):
    packet = (len(payload).to_bytes(2, "big") + payload
              + _crc16(payload).to_bytes(2, "big"))
    bits = np.zeros(capacity, dtype=np.uint8)
    packed = np.unpackbits(np.frombuffer(packet, dtype=np.uint8))
    if len(packed) > capacity:
        raise ValueError("packet exceeds information capacity")
    bits[:len(packed)] = packed
    return bits ^ _pn_bits(capacity, WHITENING_SEED)


def _encode_source_bits(payload, profile):
    if profile.fec is None:
        return _information_packet(payload, PAYLOAD_BITS)
    k = ldpc.INFORMATION_BITS[profile.fec]
    information = _information_packet(payload, CODEWORDS * k).reshape(
        CODEWORDS, k)
    coded = np.concatenate([ldpc.encode(row, profile.fec)
                            for row in information])
    return np.concatenate((coded, _pn_bits(PAD_BITS, 0x15555)))


def _unpack_information(information):
    information = (np.asarray(information, dtype=np.uint8)
                   ^ _pn_bits(len(information), WHITENING_SEED))
    data = np.packbits(information).tobytes()
    length = int.from_bytes(data[:2], "big") if len(data) >= 2 else 0x10000
    maximum = max(0, len(data) - 4)
    meta = {"decoded_length": length, "crc_ok": False}
    if length > maximum:
        meta["failure"] = "invalid length"
        return None, meta
    payload = data[2:2 + length]
    received = int.from_bytes(data[2 + length:4 + length], "big")
    computed = _crc16(payload)
    meta.update(received_crc16=received, computed_crc16=computed,
                crc_ok=received == computed)
    if received != computed:
        meta["failure"] = "CRC mismatch"
        return None, meta
    return payload, meta


def _clip_to_papr(audio, papr_db):
    audio = np.asarray(audio, dtype=np.float64).copy()
    if papr_db is None:
        return audio
    target = float(papr_db)
    for _ in range(8):
        rms = float(np.sqrt(np.mean(audio * audio)))
        if rms <= 0.0:
            break
        measured = 20.0 * np.log10(max(float(np.max(np.abs(audio))), 1e-30) / rms)
        if measured <= target + 0.2:
            break
        limit = rms * 10.0 ** ((target - 0.8) / 20.0)
        audio = np.clip(audio, -limit, limit)
        # Remove clipping splatter while retaining a 100 Hz transition beyond
        # the outer carriers.  Re-filtering grows peaks, hence the loop.
        freq = np.fft.rfftfreq(len(audio), 1.0 / SAMPLE_RATE)
        mask = np.ones(len(freq))
        low0, low1 = CARRIER_HZ[0] - 200.0, CARRIER_HZ[0] - 100.0
        high0, high1 = CARRIER_HZ[-1] + 100.0, CARRIER_HZ[-1] + 200.0
        mask[freq <= low0] = 0.0
        rise = (freq > low0) & (freq < low1)
        mask[rise] = 0.5 - 0.5 * np.cos(np.pi * (freq[rise] - low0)
                                       / (low1 - low0))
        fall = (freq > high0) & (freq < high1)
        mask[fall] = 0.5 + 0.5 * np.cos(np.pi * (freq[fall] - high0)
                                       / (high1 - high0))
        mask[freq >= high1] = 0.0
        audio = np.fft.irfft(np.fft.rfft(audio) * mask, n=len(audio))
    return audio


def _rolling_sum(values, width):
    prefix = np.concatenate((np.zeros(1, dtype=values.dtype),
                             np.cumsum(values)))
    return prefix[width:] - prefix[:-width]


def _proposal_groups(analytic, threshold):
    if len(analytic) < ((HEADER_SYMBOLS - 1) * SYMBOL_SAMPLES
                        + 2 * CORE_SAMPLES):
        return []
    left, right = analytic[:-CORE_SAMPLES], analytic[CORE_SAMPLES:]
    cross = _rolling_sum(right * np.conj(left), CORE_SAMPLES)
    e0 = _rolling_sum(np.abs(left) ** 2, CORE_SAMPLES).real
    e1 = _rolling_sum(np.abs(right) ** 2, CORE_SAMPLES).real
    count = len(cross) - (HEADER_SYMBOLS - 1) * SYMBOL_SAMPLES
    total = np.zeros(count, dtype=np.complex128)
    power0 = np.zeros(count)
    power1 = np.zeros(count)
    for i in range(HEADER_SYMBOLS):
        sl = slice(i * SYMBOL_SAMPLES, i * SYMBOL_SAMPLES + count)
        total += cross[sl]
        power0 += e0[sl]
        power1 += e1[sl]
    scores = np.abs(total) / np.sqrt(np.maximum(power0 * power1, 1e-30))
    energy = np.sqrt((power0 + power1) / (2 * HEADER_SYMBOLS * CORE_SAMPLES))
    if not len(scores) or np.max(energy) <= 0.0:
        return []
    selected = np.flatnonzero((scores >= threshold)
                              & (energy >= 0.03 * np.max(energy)))
    if not len(selected):
        best = int(np.argmax(scores))
        return [(best, best, float(scores[best]))]
    breaks = np.flatnonzero(np.diff(selected) > 1)
    groups = []
    for group in np.split(selected, breaks + 1):
        at = int(group[np.argmax(scores[group])])
        groups.append((int(group[0]), int(group[-1]), float(scores[at])))
    # Every data symbol has the same two-core repetition, so the numerically
    # strongest groups are often near the end of the payload.  Acquisition
    # needs the earliest plausible groups; the known header fit ranks them.
    return groups[:32]


def _coarse_cfo(analytic, start):
    total = 0.0j
    for i in range(HEADER_SYMBOLS):
        at = start + i * SYMBOL_SAMPLES + DEFAULT_OFFSET
        if at < 0 or at + 2 * CORE_SAMPLES > len(analytic):
            continue
        total += np.vdot(analytic[at:at + CORE_SAMPLES],
                         analytic[at + CORE_SAMPLES:at + 2 * CORE_SAMPLES])
    if total == 0.0j:
        return 0.0
    return float(np.angle(total) * SAMPLE_RATE / (2 * np.pi * CORE_SAMPLES))


def _derotate(audio, hz):
    n = np.arange(len(audio), dtype=np.float64)
    return audio * np.exp(-2j * np.pi * hz * n / SAMPLE_RATE)


def _carrier_bank(analytic, start, count, combine=True):
    bank = np.empty((count, N_CARRIERS), dtype=np.complex128)
    for i in range(count):
        at = start + i * SYMBOL_SAMPLES
        if at < 0 or at + SYMBOL_SAMPLES > len(analytic):
            return None
        bank[i] = symbol_carriers(analytic[at:at + SYMBOL_SAMPLES],
                                  DEFAULT_OFFSET, combine=combine)
    return bank


def _fine_cfo(observed):
    stripped = observed * np.conj(header_values())
    steps = stripped[1:] * np.conj(stripped[:-1])
    total = np.sum(steps)
    if total == 0.0j:
        return 0.0
    return float(np.angle(total) * SAMPLE_RATE / (2 * np.pi * SYMBOL_SAMPLES))


def _fit_header(observed):
    reference = header_values()
    gain = np.mean(observed * np.conj(reference), axis=0)
    fitted = reference * gain[None, :]
    residual = observed - fitted
    noise = np.mean(np.abs(residual) ** 2, axis=0)
    snr = 10.0 * np.log10(np.maximum(np.abs(gain) ** 2, 1e-30)
                             / np.maximum(noise, 1e-30))
    numerator = np.abs(np.sum(observed * np.conj(fitted), axis=0))
    denominator = np.sqrt(np.sum(np.abs(observed) ** 2, axis=0)
                          * np.sum(np.abs(fitted) ** 2, axis=0))
    confidence = float(np.median(numerator / np.maximum(denominator, 1e-30)))
    return gain, noise, snr, confidence


def _evaluate_start(analytic, start):
    coarse = _coarse_cfo(analytic, start)
    stop = start + HEADER_SYMBOLS * SYMBOL_SAMPLES
    if start < 0 or stop > len(analytic):
        return None
    # Candidate ranking can involve hundreds of prefix-wide positions.  Only
    # derotate the 360 ms header under test, not the whole multi-second
    # capture for every hypothesis.
    corrected = _derotate(analytic[start:stop], coarse)
    header = _carrier_bank(corrected, 0, HEADER_SYMBOLS)
    if header is None:
        return None
    fine = _fine_cfo(header)
    phase = np.exp(-2j * np.pi * fine * np.arange(HEADER_SYMBOLS)
                   * SYMBOL_SAMPLES / SAMPLE_RATE)
    header = header * phase[:, None]
    gain, noise, snr, confidence = _fit_header(header)
    return confidence, coarse, fine, header, gain, noise, snr


def _acquire(analytic, threshold):
    best = None
    for low, high, repeat_score in _proposal_groups(analytic, threshold * 0.8):
        # The repetition metric is a prefix-wide plateau.  Search across its
        # leading edge; the known varying header decides the sample alignment.
        lo = max(0, low - GUARD_SAMPLES)
        hi = min(len(analytic) - TOTAL_SYMBOLS * SYMBOL_SAMPLES,
                 min(high + GUARD_SAMPLES, low + 3 * GUARD_SAMPLES))
        if hi < lo:
            continue
        # Header confidence is broad inside the repeated-core timing window;
        # a 32-sample first pass followed by sample-accurate refinement gives
        # the same locks as testing every eighth sample at one quarter of the
        # FFT work on multi-second captures.
        coarse_starts = list(range(lo, hi + 1, 32))
        local = []
        for start in coarse_starts:
            evaluated = _evaluate_start(analytic, start)
            if evaluated is not None:
                local.append((evaluated[0], start, evaluated))
        if not local:
            continue
        _, coarse_start, _ = max(local)
        for start in range(max(lo, coarse_start - 16),
                           min(hi, coarse_start + 16) + 1):
            evaluated = _evaluate_start(analytic, start)
            if evaluated is None:
                continue
            key = (evaluated[0], repeat_score, -start)
            if best is None or key > best[0]:
                best = (key, start, evaluated)
    return None if best is None else (best[1], best[2])


def _track_pilot_phase(payload_values):
    corrected = np.asarray(payload_values, dtype=np.complex128).copy()
    phase = np.zeros_like(corrected.real)
    q_positions = pilot_positions()[0::2] // 2
    known = _qpsk(pilot_bits())
    rows = q_positions // N_CARRIERS
    carriers = q_positions % N_CARRIERS
    for carrier in range(N_CARRIERS):
        use = np.flatnonzero(carriers == carrier)
        if not len(use):
            continue
        anchor_rows = np.concatenate((np.array([-1]), rows[use]))
        anchor_phase = np.concatenate((np.array([0.0]), np.angle(
            corrected[rows[use], carrier] / known[use])))
        anchor_phase = np.unwrap(anchor_phase)
        # On a five-second sound-card path the dominant motion is affine:
        # residual carrier offset plus sample-clock drift.  A line fit uses
        # all scattered pilots and cannot let one noisy anchor rotate the
        # following twenty symbols by pi, which interpolation did on the
        # first IC-7300 -> IC-705 capture (one otherwise-clean 656 Hz
        # carrier accumulated 63 false bit errors).
        slope, intercept = np.polyfit(anchor_rows, anchor_phase, 1)
        residual = anchor_phase - (slope * anchor_rows + intercept)
        median = np.median(residual)
        mad = np.median(np.abs(residual - median))
        keep = np.abs(residual - median) <= max(0.35, 4.0 * mad)
        if np.count_nonzero(keep) >= 3:
            slope, intercept = np.polyfit(anchor_rows[keep], anchor_phase[keep], 1)
        phase[:, carrier] = slope * np.arange(PAYLOAD_SYMBOLS) + intercept
        corrected[:, carrier] *= np.exp(-1j * phase[:, carrier])
    pilot_residual = corrected[rows, carriers] - known
    return corrected, phase, float(np.sqrt(np.mean(np.abs(pilot_residual) ** 2)))


def _decode_values(values, snr_db, profile):
    linear = 10.0 ** (np.asarray(snr_db) / 10.0)
    # A radio filter can remove an edge carrier entirely.  Its equalized
    # samples then have huge magnitude but no information; a 0.25 floor let
    # those confident random values defeat three LDPC blocks on the first HF
    # capture.  Near-zero weight correctly presents such a carrier as an
    # erasure, while the upper clamp still stops one hot carrier dominating.
    weights = np.clip(linear / max(float(np.median(linear)), 1e-30), 0.01, 4.0)
    pair_llr = np.stack((values.real, values.imag), axis=-1)
    pair_llr *= weights[None, :, None]
    grid_llr = pair_llr.reshape(-1)
    source_llr = np.empty(PAYLOAD_BITS, dtype=float)
    source_llr[interleave_map()] = grid_llr
    if profile.fec is None:
        info = (source_llr < 0.0).astype(np.uint8)
        payload, meta = _unpack_information(info)
        return payload, meta, source_llr, np.zeros(0, dtype=int), np.ones(0, dtype=bool)
    blocks = source_llr[:CODED_BITS].reshape(CODEWORDS, ldpc.N)
    info, iterations, ok = ldpc.decode_batch(blocks, rate=profile.fec)
    payload, meta = _unpack_information(info.reshape(-1))
    meta["ldpc_ok"] = bool(np.all(ok))
    if payload is None and not np.all(ok):
        meta["failure"] = "LDPC syndrome did not clear"
    return payload, meta, source_llr, iterations, ok


def _base_result():
    return {
        "synced": False, "payload": None, "confidence": 0.0,
        "start_index": None, "cfo_hz": 0.0,
        "carrier_snr_db": np.full(N_CARRIERS, -np.inf),
        "symbol_evm_db": np.full(TOTAL_SYMBOLS, np.inf),
        "timing_drift_samples": 0.0,
        "window_offset": np.full(TOTAL_SYMBOLS, DEFAULT_OFFSET, dtype=np.int32),
        "combined": np.ones(TOTAL_SYMBOLS, dtype=bool),
    }


def _demodulate(audio, profile, reference_payload, debug):
    result = _base_result()
    samples = np.asarray(audio, dtype=np.float64).reshape(-1)
    if len(samples) > int(MAX_SEARCH_SECONDS * SAMPLE_RATE):
        samples = samples[-int(MAX_SEARCH_SECONDS * SAMPLE_RATE):]
    if len(samples) < TOTAL_SYMBOLS * SYMBOL_SAMPLES:
        result["failure"] = "capture is shorter than a frame"
        return result
    analytic = hilbert(samples)
    acquired = _acquire(analytic, profile.confidence_threshold)
    if acquired is None:
        result["failure"] = "no header candidate"
        return result
    start, evaluated = acquired
    confidence, coarse, fine, _, _, _, _ = evaluated
    result.update(start_index=start, confidence=confidence,
                  cfo_hz=coarse + fine)
    if confidence < profile.confidence_threshold:
        result["failure"] = "header confidence below threshold"
        return result

    corrected_audio = _derotate(analytic, coarse)
    carriers = _carrier_bank(corrected_audio, start, TOTAL_SYMBOLS)
    if carriers is None:
        result["failure"] = "frame has not fully arrived"
        return result
    fine_phase = np.exp(-2j * np.pi * fine * np.arange(TOTAL_SYMBOLS)
                        * SYMBOL_SAMPLES / SAMPLE_RATE)
    carriers *= fine_phase[:, None]
    header = carriers[:HEADER_SYMBOLS]
    gain, noise, snr, confidence = _fit_header(header)
    result.update(confidence=confidence, carrier_snr_db=snr,
                  synced=True, channel=gain, noise_variance=noise)
    safe_gain = np.where(np.abs(gain) > 1e-12, gain, 1e-12)
    equalized = carriers / safe_gain[None, :]
    if profile.fec is None:
        # In the diagnostic uncoded profile every grid point carries data;
        # the 263 scattered pilots exist only in the fixed-codeword layouts.
        payload_values = equalized[HEADER_SYMBOLS:].copy()
        pilot_phase = np.zeros_like(payload_values.real)
        pilot_error = 0.0
    else:
        payload_values, pilot_phase, pilot_error = _track_pilot_phase(
            equalized[HEADER_SYMBOLS:])
    payload, meta, source_llr, iterations, ldpc_ok = _decode_values(
        payload_values, snr, profile)
    result.update(meta)
    result["payload"] = payload

    sliced_header = header_values()
    sliced_payload = _qpsk(_hard_bits(payload_values)).reshape(
        PAYLOAD_SYMBOLS, N_CARRIERS)
    decisions = np.vstack((sliced_header, sliced_payload))
    error = equalized.copy()
    error[HEADER_SYMBOLS:] = payload_values
    evm = np.sqrt(np.mean(np.abs(error - decisions) ** 2, axis=1))
    result["symbol_evm_db"] = 20.0 * np.log10(np.maximum(evm, 1e-15))

    impulse = np.fft.ifft(np.pad(gain, (0, CORE_SAMPLES - len(gain))))
    energy = np.abs(impulse) ** 2
    cumulative = np.cumsum(energy) / max(float(np.sum(energy)), 1e-30)
    delay_samples = int(np.searchsorted(cumulative, 0.99))
    result.update(
        phase_track=np.concatenate((np.zeros(HEADER_SYMBOLS),
                                    np.median(pilot_phase, axis=1))),
        delay_spread_ms=delay_samples * 1000.0 / SAMPLE_RATE,
        ldpc_iterations=iterations, ldpc_ok=ldpc_ok,
        pilot_error_rms=pilot_error,
    )
    if debug:
        result["constellation"] = np.vstack((equalized[:HEADER_SYMBOLS],
                                               payload_values))
        result["soft_payload_bits"] = source_llr
    if reference_payload is not None:
        expected_source = _encode_source_bits(bytes(reference_payload), profile)
        expected_grid = expected_source[interleave_map()]
        observed_grid = _hard_bits(payload_values)
        errors = observed_grid != expected_grid
        grid = errors.reshape(PAYLOAD_SYMBOLS, N_CARRIERS, BITS_PER_CARRIER)
        carrier_errors = np.sum(grid, axis=(0, 2))
        symbol_errors = np.zeros(TOTAL_SYMBOLS, dtype=int)
        symbol_errors[HEADER_SYMBOLS:] = np.sum(grid, axis=(1, 2))
        result.update(carrier_bit_errors=carrier_errors,
                      symbol_bit_errors=symbol_errors,
                      total_bit_errors=int(np.sum(errors)),
                      ber=float(np.mean(errors)))
    return result


# -- public API ------------------------------------------------------------


def modulate(payload: bytes, profile=DEFAULT):
    """One complete frame. Returns float32, exactly FRAME_SAMPLES long."""
    payload = bytes(payload)
    if len(payload) > profile.max_payload:
        raise ValueError(f"payload is {len(payload)} bytes; maximum is "
                         f"{profile.max_payload}")
    source_bits = _encode_source_bits(payload, profile)
    grid_bits = source_bits[interleave_map()]
    values = _qpsk(grid_bits).reshape(PAYLOAD_SYMBOLS, N_CARRIERS)
    constellation = np.vstack((header_values(), values))
    symbols = np.concatenate([build_symbol(row) for row in constellation])

    # The lead is a continuation of the first known header core.  SSB has no
    # squelch blackout, so 45 ms is enough to settle the transmitter while
    # remaining useful signal for AGC and coarse frequency acquisition.
    first_core = build_symbol(header_values()[0])[GUARD_SAMPLES:
                                                     GUARD_SAMPLES + CORE_SAMPLES]
    lead = np.resize(first_core, LEAD_IN_SAMPLES)
    body = np.concatenate((lead, symbols))
    body = _clip_to_papr(body, profile.papr_db)
    peak = float(np.max(np.abs(body))) or 1.0
    body = body * (profile.amplitude / peak)
    ramp = min(240, len(body))
    body[:ramp] *= np.sin(np.linspace(0.0, np.pi / 2.0, ramp)) ** 2
    tail = np.zeros(TAIL_SAMPLES, dtype=float)
    fade = min(TAIL_SAMPLES, len(body))
    tail[:fade] = body[-1] * np.cos(np.linspace(0.0, np.pi / 2.0, fade)) ** 2
    audio = np.concatenate((body, tail)).astype(np.float32)
    assert len(audio) == FRAME_SAMPLES
    return audio


def demodulate(audio, profile=DEFAULT):
    """Search `audio` for one frame.

    Always returns a dict with at least:

        synced                bool
        payload               bytes, or None on any failure
        confidence            float, 0..1
        start_index           int sample index of symbol 0, or None
        cfo_hz                float, estimated carrier frequency offset
        carrier_snr_db        (29,) float, from the header
        symbol_evm_db         (214,) float
        timing_drift_samples  float, estimated drift across the frame
        window_offset         (214,) int, the FFT placement chosen per symbol
        combined              (214,) bool, whether both cores were averaged
    """
    return _demodulate(audio, profile, reference_payload=None, debug=False)


def demodulate_debug(audio, profile=DEFAULT, reference_payload=None):
    """demodulate(), plus everything needed to explain a failure.

    Adds to the dict returned by demodulate():

        constellation      (214, 29) complex, after equalisation
        channel            (29,) complex, the header channel estimate
        noise_variance     (29,) float, per-carrier
        phase_track        (214,) float, radians
        delay_spread_ms    float, estimated L
        ldpc_iterations    (17,) int
        ldpc_ok            (17,) bool
        pilot_error_rms    float, residual on the scattered pilots

    When `reference_payload` is given, also:

        carrier_bit_errors (29,) int
        symbol_bit_errors  (214,) int
        total_bit_errors   int
    """
    return _demodulate(audio, profile, reference_payload=reference_payload,
                       debug=True)


def describe(profile=DEFAULT):
    """One-line summary, the form printed by every script in this experiment."""
    fec = profile.fec or "uncoded"
    return (f"{profile.name}: {N_CARRIERS} carriers "
            f"{CARRIER_HZ[0]:.2f}-{CARRIER_HZ[-1]:.2f}Hz QPSK, "
            f"fec {fec}, {profile.max_payload}B/frame, "
            f"{profile.payload_bitrate:.0f}bit/s over {FRAME_SECONDS:.3f}s")


# -- arithmetic self-check -------------------------------------------------
#
# Every number above is derived, but several are also quoted in the README,
# in the plan and in the specification we were handed. If a constant is edited
# and the derived figures stop agreeing, that should fail loudly at import
# rather than quietly change what the sweeps measure.

def _check():
    assert CARRIER_SPACING == 93.75, CARRIER_SPACING
    assert N_CARRIERS == 29, N_CARRIERS
    assert CARRIER_HZ[0] == 468.75 and CARRIER_HZ[-1] == 3093.75, CARRIER_HZ
    assert SYMBOL_SAMPLES == GUARD_SAMPLES + 2 * CORE_SAMPLES
    assert abs(SYMBOL_RATE - 41.6667) < 1e-3, SYMBOL_RATE
    assert abs(GROSS_BITRATE - 2416.7) < 0.1, GROSS_BITRATE
    assert FRAME_SAMPLES == 249_600, FRAME_SAMPLES
    assert FRAME_SECONDS == 5.2, FRAME_SECONDS
    assert PAYLOAD_BITS == 11_542, PAYLOAD_BITS
    assert CODEWORDS == 17, CODEWORDS
    assert PAD_BITS == 526, PAD_BITS
    assert PILOT_SYMBOLS == 263, PILOT_SYMBOLS
    assert SINGLE_MAX_OFFSET == 640 and COMBINE_MAX_OFFSET == 128
    assert DEFAULT_OFFSET <= COMBINE_MAX_OFFSET
    # The codeword count must not depend on the rate.
    for rate in FEC_RATES:
        assert PAYLOAD_BITS // ldpc.N == CODEWORDS, rate
    assert max_payload(None) == 1438, max_payload(None)
    assert max_payload("3/4") == 1028, max_payload("3/4")
    assert max_payload("2/3") == 914, max_payload("2/3")
    assert max_payload("1/2") == 684, max_payload("1/2")


_check()
