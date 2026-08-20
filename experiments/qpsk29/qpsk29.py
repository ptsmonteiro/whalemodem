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
from pathlib import Path
import sys

import numpy as np

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


def interleave_map():
    """Grid position -> source bit index. Shape (PAYLOAD_BITS,), int32.

    Source bit indices [0, CODED_BITS) are codeword bits, laid out codeword-
    major: index c * ldpc.N + i is bit i of codeword c. Indices
    [CODED_BITS, PAYLOAD_BITS) are the pad/pilot bits.
    """
    raise NotImplementedError


def codeword_of_grid_bit():
    """Which codeword each grid position carries. Shape (PAYLOAD_BITS,), int8.

    -1 marks a pad/pilot position.
    """
    raise NotImplementedError


def pilot_positions():
    """Grid indices carrying known pilot bits. Shape (PAD_BITS,), int32."""
    raise NotImplementedError


def pilot_bits():
    """The known pilot bit values, aligned with pilot_positions(). (PAD_BITS,)."""
    raise NotImplementedError


# -- symbol level ----------------------------------------------------------


def header_values():
    """The fixed, unscrambled header constellation. Shape (15, 29), complex.

    Constant for the life of the mode. Both ends must agree exactly.
    """
    raise NotImplementedError


def build_symbol(values):
    """One 1152-sample symbol from 29 complex carrier values.

    Fills a 512-point spectrum at CARRIER_BINS, Hermitian-mirrors it, inverse
    transforms to a real 512-sample core, then returns

        concat(core[-128:], core, core)

    which is a contiguous slice of the infinite 512-periodic extension of
    `core` -- so block[n] == core[(n - 128) % 512] for all 1152 samples, and
    the guard is a true cyclic prefix rather than a separately-generated pad.
    """
    raise NotImplementedError


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
    raise NotImplementedError


# -- public API ------------------------------------------------------------


def modulate(payload: bytes, profile=DEFAULT):
    """One complete frame. Returns float32, exactly FRAME_SAMPLES long."""
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
