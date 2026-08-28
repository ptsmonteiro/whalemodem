"""HC0: the rung that has to get through.

HC1 (`whale/modes/hc1.py`) is an OFDM frame, and it is a good one -- 10/10
byte-for-byte on the bench's strong leg with no bit errors at all.  On the
bench's *weak* leg it decoded nothing, and measuring why is what this mode
came out of:

    HC1 payload, acquisition handed the true start   works to  -4 dB
    HC1 as actually decoded                          works to +3.5 dB
    the weak leg delivers                            about   -8 dB

Two separate problems, and the second one is the instructive one.  HC1's
confidence is the normalized self-correlation of its repeated sync symbols,
whose expected value is exactly `SNR/(SNR+1)` -- so the 0.70 threshold it
inherited from VF3 *is* a 3.7 dB SNR floor, and no amount of preamble moves
it, because lengthening the correlation shrinks its variance and not its
mean.  Everything downstream then depends on a carrier-offset estimate that
is itself unusable at low SNR: correcting by a bad estimate destroyed a
coherent header match that would otherwise have worked 12 dB further down.

The answer is not a better phase estimator.  It is to stop needing phase.

HC0 is non-coherent 16-ary FSK.  Information is which of 16 tones is
present, measured as energy, so no part of the receive path holds a phase
reference: not the demodulator, not the synchronizer, and not the frequency
estimator, which measures the offset but never gates on it.  Sync is a
correlation against a known tone *pattern*, whose processing gain grows
with its length in the ordinary way.  Behind the tone detector sits exactly
the same interleaver, rate-1/2 K=7 convolutional code and length/CRC32
packet the OFDM modes use, unchanged.

Measured against HC1 at equal transmitted RMS, white noise across the full
band, 74-byte frames:

    HC1  (OFDM DQPSK, 0.70 s)     fails below  +3.5 dB
    HC0  (16-FSK,     3.27 s)     decodes to    -16 dB

19.5 dB, of which about 7 is spending time and the rest is not paying for
coherence.  And that is at equal *RMS*: HC0's waveform is
constant-envelope, crest factor 1.41 against HC1's 3.9, so through the same
peak-limited transmitter it delivers roughly 8 dB more average power again.

    [head][24 sync symbols][283 payload symbols][tail]

What it costs is throughput -- 54 payload bytes per 3.3 s keying -- which is
the correct trade for the mode every control frame rides and the one a
struggling link falls back to.  HC1 stays in the ladder above it for when
the channel can carry it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

import numpy as _np
from scipy.signal import hilbert as _hilbert

from .. import dsp
from ..dsp import freq as _freq, head as _head, mfsk as _mfsk

# -- geometry -------------------------------------------------------------

SAMPLE_RATE = 48_000

#: 512 samples: 10.667 ms, so 93.75 baud and 93.75 Hz of tone spacing.
#: Slow enough that a symbol carries real energy, fast enough that 283 of
#: them still fit in a keying an operator will tolerate.
SYMBOL_SAMPLES = 512

#: 16 tones from bin 8, i.e. 750 Hz to 2,156.25 Hz.  1.5 kHz of occupied
#: bandwidth, centred at 1,453 Hz -- comfortably inside a 2.4 kHz SSB data
#: filter with both skirts clear, so nothing depends on where the receiver
#: happens to have placed its passband.
FIRST_BIN = 8
TONE_COUNT = 16

BANK = _mfsk.ToneBank(sample_rate=SAMPLE_RATE, symbol_samples=SYMBOL_SAMPLES,
                      first_bin=FIRST_BIN, tone_count=TONE_COUNT)

BITS_PER_SYMBOL = BANK.bits_per_symbol
TONE_HZ = BANK.tone_hz
SPACING_HZ = BANK.spacing_hz

#: 24 symbols of known tones, 256 ms.
#:
#: Sized against the payload rather than picked: the payload gives out at
#: about -16 dB, and at -16 dB a 24-symbol correlation scores 0.20 where
#: the worst false peak anywhere in a ten-second buffer scores 0.063.  A
#: 16-symbol preamble closes that to 2.1x and a 32-symbol one only widens
#: it to 3.7x, so 24 is where the preamble stops being the thing that
#: limits the mode.
SYNC_SYMBOLS = 24

#: 283 symbols: the smallest payload grid that carries a whole number of
#: packet bytes with no bits stranded and still leaves room for the largest
#: control frame the link builds.  See `_check_constants`.
PAYLOAD_SYMBOLS = 283
TOTAL_SYMBOLS = SYNC_SYMBOLS + PAYLOAD_SYMBOLS
PAYLOAD_BITS = PAYLOAD_SYMBOLS * BITS_PER_SYMBOL

#: Constant-envelope, so this is both the RMS and (over sqrt(2)) the peak.
#: The same 0.13 the OFDM modes ask for, which means HC0 is transmitted
#: with a peak 2.8x lower rather than being driven harder -- the headroom
#: is left to the operator's audio level rather than taken here.
TX_AMPLITUDE = 0.13 * np.sqrt(2.0)
MAX_SAMPLE = 0.95

#: Detection threshold on `correlate`'s score.
#:
#: Measured, both halves.  A genuine preamble scores 0.35 at -10 dB, 0.20 at
#: -16 dB and 0.15 at -18 dB; the loudest false peak over a whole buffer of
#: noise, of off-air hiss, or of a bare carrier sits at 0.06-0.08 and does
#: not climb with SNR because it is not measuring signal at all.  0.12 sits
#: in that gap with the detector still working two dB past where the
#: payload stops.
ACQUISITION_THRESHOLD = 0.12

# -- the adaptive head ----------------------------------------------------
#
# The link negotiates a leading guard per direction and adjusts it during a
# transfer (see whale/framing.py's HEAD_PAD_SECONDS and ADAPTIVE_TIMING.md).
# HC0's is a repeat of a fixed four-symbol tone block, which `_head.measure`
# counts backwards from the preamble.
#
# It does *not* have to avoid resembling the preamble the way VF3's and
# HC1's heads have to avoid resembling their sync symbols. Those modes
# acquire by correlating the capture against itself, so any repeat anywhere
# near the header widens the peak into a plateau; HC0 correlates against a
# known pattern instead, and a head built from a different pattern simply
# does not score. One more thing that stops being a problem when detection
# stops being blind.

HEAD_BLOCK_SYMBOLS = 4
HEAD_BLOCK_SAMPLES = HEAD_BLOCK_SYMBOLS * SYMBOL_SAMPLES

#: Two blocks, 85.3 ms.  SSB has no squelch to blank the start of a
#: transmission, so this floor is only what ramps the transmitter and the
#: sound card up.
LEAD_IN_BLOCKS = 2
LEAD_IN_SAMPLES = LEAD_IN_BLOCKS * HEAD_BLOCK_SAMPLES
LEAD_IN_FADE_SAMPLES = 240
TAIL_SAMPLES = 960

DEFAULT_HEAD_SECONDS = LEAD_IN_SAMPLES / SAMPLE_RATE

#: The longest head the link will ever ask for (whale/link.py's
#: HEAD_MAX_SECONDS), plus a block, which is as far back as there is any
#: point looking.
MAX_HEAD_SAMPLES = SAMPLE_RATE + HEAD_BLOCK_SAMPLES

#: Two samples of block-to-block alignment drift, because the head is
#: measured on frequency-corrected audio; see `_measure_head`.
HEAD_PHASE_TOLERANCE = 2


def lead_in_samples(head_seconds: float | None = None) -> int:
    """Leading head samples for a requested duration, whole blocks.

    Rounded up to a whole block because `_head.measure` counts blocks, and
    a partial one at the transmitter would be reported short by the
    receiver for the life of the session.
    """
    if head_seconds is None:
        wanted = LEAD_IN_SAMPLES
    elif head_seconds < 0:
        raise ValueError("head duration must not be negative")
    else:
        wanted = max(LEAD_IN_SAMPLES, int(round(head_seconds * SAMPLE_RATE)))
    blocks = -(-wanted // HEAD_BLOCK_SAMPLES)
    return blocks * HEAD_BLOCK_SAMPLES


def frame_samples(head_seconds: float = DEFAULT_HEAD_SECONDS) -> int:
    return (lead_in_samples(head_seconds)
            + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES)


def frame_seconds(head_seconds: float = DEFAULT_HEAD_SECONDS) -> float:
    return frame_samples(head_seconds) / SAMPLE_RATE


FRAME_SAMPLES = frame_samples()
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

# -- reference patterns and the payload codec -----------------------------

#: 12 PN-drawn tones, each sent twice.
#:
#: The repeat is not padding.  `_mfsk.offset_hz` measures the carrier
#: offset from the phase step between two symbols, and a symbol's phase
#: also carries a symbol-timing term that depends on which tone it used --
#: so between two *different* tones a timing error leaks straight into the
#: frequency estimate, and 48 samples of it (inside what the detector
#: shrugs off) turned a zero offset into -13.5 Hz.  Across a pair sharing a
#: tone that term cancels, which makes the estimate exact at any timing.
#: It costs the detector nothing: 12 distinct tones over 24 symbols still
#: score 0.20 at -16 dB against a 0.06 noise floor.
SYNC_PATTERN = np.repeat(
    BANK.symbols_from_bits(
        dsp.bits.pn_bits((SYNC_SYMBOLS // 2) * BITS_PER_SYMBOL, 0x0A73D)), 2)
HEAD_PATTERN = BANK.symbols_from_bits(
    dsp.bits.pn_bits(HEAD_BLOCK_SYMBOLS * BITS_PER_SYMBOL, 0x136E9))

CODEC = dsp.PacketCodec(
    payload_bits=PAYLOAD_BITS,
    interleaver=dsp.interleave.multiplicative(PAYLOAD_BITS, 693),
    whitener_seed=0x0C4B1,
    code=dsp.K7,
)

FEC_INPUT_BITS = CODEC.information_bits
PACKET_BYTES = CODEC.packet_bytes
UNUSED_INFO_BITS = CODEC.unused_information_bits
MAX_PAYLOAD_BYTES = CODEC.max_payload_bytes

encode_payload_bits = CODEC.encode
decode_payload_soft = CODEC.decode_soft


# -- modulation -----------------------------------------------------------

def head_block() -> np.ndarray:
    """The repeating block the head is built from."""
    return _mfsk.modulate(BANK, HEAD_PATTERN, TX_AMPLITUDE)


def modulate(payload: bytes, *,
             head_seconds: float = DEFAULT_HEAD_SECONDS) -> np.ndarray:
    tones = np.concatenate((
        SYNC_PATTERN,
        BANK.symbols_from_bits(encode_payload_bits(payload)),
    ))
    body = _mfsk.modulate(BANK, tones, TX_AMPLITUDE)
    lead = np.resize(head_block(), lead_in_samples(head_seconds)).copy()
    fade = LEAD_IN_FADE_SAMPLES
    lead[:fade] *= np.linspace(0.0, 1.0, fade, endpoint=True)
    audio = np.concatenate((lead, body, np.zeros(TAIL_SAMPLES)))
    if len(audio) != frame_samples(head_seconds):
        raise AssertionError(f"internal frame length error: {len(audio)}")
    peak = float(np.max(np.abs(audio)))
    if peak > MAX_SAMPLE:
        audio *= MAX_SAMPLE / peak
    return audio.astype(np.float32)


# -- demodulation ---------------------------------------------------------

def _measure_head(samples: np.ndarray, start: int,
                  offset_hz: float) -> tuple[int, float]:
    """How much of the transmitted head survived, in whole 4-symbol blocks.

    Takes the *frequency-corrected* audio, and the correction is why this
    runs after `_mfsk.offset_hz` rather than before it.  Everything else in
    this mode detects energy and does not care about phase; the head is the
    one exception, because it is matched against a reference *waveform*,
    and the bench's own 8 Hz turns a 42.7 ms block by a third of a turn.
    Measured before this was ordered correctly: a 1 s head, arriving
    perfectly, reported as one block -- so the link kept transmitting a
    full second of it for the whole session, a quarter of every keying
    spent on padding that was already known to be arriving.

    Only the last second is examined, which is all the link will ever ask
    for, so the analytic signal is computed over a slice rather than the
    whole receive buffer.
    """
    span = min(start, MAX_HEAD_SAMPLES)
    if span < HEAD_BLOCK_SAMPLES:
        return 0, 0.0
    window = _np.asarray(samples[start - span:start], dtype=_np.float64)
    corrected = _np.real(
        _freq.derotate(_hilbert(window), offset_hz, SAMPLE_RATE))
    return _head.measure(corrected, len(corrected), head_block(),
                         phase_tolerance=HEAD_PHASE_TOLERANCE)


def _base_result() -> dict:
    return {
        "synced": False, "payload": None, "confidence": 0.0,
        "start_index": None, "cfo_hz": 0.0, "raw_payload_bits": None,
    }


def _acquire(audio: np.ndarray) -> tuple[int | None, float]:
    """Best sync-pattern match in `audio`, refined to 8 samples."""
    scores, step = _mfsk.correlate(BANK, audio, SYNC_PATTERN)
    if not len(scores):
        return None, 0.0
    coarse = int(np.argmax(scores))
    if scores[coarse] < ACQUISITION_THRESHOLD:
        return coarse * step, float(scores[coarse])
    start = _mfsk.refine(BANK, audio, SYNC_PATTERN, coarse * step, radius=step)
    score = _mfsk.pattern_score(BANK, audio, SYNC_PATTERN, start)
    return start, float(max(score, scores[coarse]))


def demodulate(audio: np.ndarray, *,
               head_seconds: float = DEFAULT_HEAD_SECONDS) -> dict:
    """Decode one HC0 frame out of `audio`.

    `head_seconds` is accepted for symmetry with `modulate` and with the
    `WaveformMode` contract; acquisition locks on the preamble wherever the
    head happened to end.

    Alongside HC0's own diagnostics the result carries the three keys the
    link's receive loop reads: `confidence`, `sync_end_index` and
    `end_index`.  `end_index` is present only once the frame has been seen
    through to its end -- its absence, with confidence above threshold, is
    how the caller is told to wait for more audio rather than consume what
    it has.  See whale/link.py's _decode_one.
    """
    del head_seconds  # acquisition finds the preamble, not the head
    result = _base_result()
    samples = np.asarray(audio, dtype=np.float64).reshape(-1)
    if len(samples) < SYNC_SYMBOLS * SYMBOL_SAMPLES:
        result["failure"] = "capture shorter than the preamble"
        return result

    start, confidence = _acquire(samples)
    result.update(confidence=confidence, start_index=start)
    if start is None or confidence < ACQUISITION_THRESHOLD:
        result["failure"] = "preamble not found"
        return result
    result["sync_end_index"] = start + SYNC_SYMBOLS * SYMBOL_SAMPLES

    # Measured, never gated on: a bad estimate costs accuracy, not the
    # frame, because nothing in the detector needed the phase it came from.
    offset = _mfsk.offset_hz(BANK, samples, start, SYNC_PATTERN)
    result["cfo_hz"] = offset

    head_blocks, head_score = _measure_head(samples, start, offset)
    result["head_blocks_received"] = head_blocks
    result["head_match"] = head_score

    values = _mfsk.analyze(BANK, samples, result["sync_end_index"],
                           PAYLOAD_SYMBOLS, offset)
    if values is None:
        # Still arriving.  Deliberately no end_index: the caller must keep
        # this audio and try again rather than consume a partial frame.
        result["failure"] = "frame truncated"
        return result
    result["end_index"] = min(
        len(samples),
        start + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES)

    magnitudes = np.abs(values)
    result["tone_snr_db"] = _tone_snr_db(magnitudes)
    result["raw_payload_bits"] = BANK.bits_from_symbols(
        np.argmax(magnitudes, axis=1))
    payload, meta = CODEC.decode_soft(_mfsk.soft_bits(BANK, magnitudes))
    result.update(meta)
    result.update(payload=payload, synced=True, tone_magnitudes=magnitudes)
    return result


def _tone_snr_db(magnitudes: np.ndarray) -> float:
    """Winning tone against the mean of the losers, in dB.

    The mode's own health number, and the one to read on a bench: with 16
    orthogonal tones the fifteen that were not sent are a direct noise
    measurement taken in the same instant as the signal, so this needs no
    reference and no separate quiet period to calibrate against.
    """
    power = magnitudes ** 2
    best = np.max(power, axis=1)
    rest = (np.sum(power, axis=1) - best) / (magnitudes.shape[1] - 1)
    return float(10.0 * np.log10(np.mean(best) / max(np.mean(rest), 1e-30)))


def demodulate_debug(audio: np.ndarray, reference_payload: bytes | None = None,
                     *, head_seconds: float = DEFAULT_HEAD_SECONDS) -> dict:
    result = demodulate(audio, head_seconds=head_seconds)
    if reference_payload is None or result.get("raw_payload_bits") is None:
        return result
    expected = encode_payload_bits(reference_payload)
    errors = result["raw_payload_bits"] != expected
    result["total_bit_errors"] = int(np.count_nonzero(errors))
    result["ber"] = float(np.mean(errors))
    return result


def describe() -> str:
    return (f"hc0: {TONE_COUNT}-FSK {TONE_HZ[0]:.1f}-{TONE_HZ[-1]:.1f} Hz, "
            f"{BANK.symbol_rate:.2f} baud, {TOTAL_SYMBOLS} symbols, "
            f"{MAX_PAYLOAD_BYTES} B + CRC32 in {FRAME_SECONDS:.3f} s, "
            f"offset tolerance +-{BANK.offset_limit_hz:.2f} Hz")


def _check_constants() -> None:
    assert SYMBOL_SAMPLES == 512 and TONE_COUNT == 16
    assert BITS_PER_SYMBOL == 4
    assert SPACING_HZ == 93.75 and BANK.symbol_rate == 93.75
    assert TONE_HZ[0] == 750.0 and TONE_HZ[-1] == 2156.25
    assert BANK.bandwidth_hz == 1500.0
    assert TOTAL_SYMBOLS == 307 and PAYLOAD_BITS == 1_132
    assert FEC_INPUT_BITS == 566
    # No stranded bits: the coded grid divides exactly into whole packet
    # bytes plus the trellis tail.  This is what picked 283 payload symbols.
    assert PACKET_BYTES == 70 and UNUSED_INFO_BITS == 0
    assert MAX_PAYLOAD_BYTES == 64
    assert FRAME_SAMPLES == 162_240 and FRAME_SECONDS == 3.38
    # Every deliberate pair; a draw that happens to repeat a tone across
    # neighbouring pairs would give more, which is only more of the same
    # measurement.
    assert len(_mfsk.repeated_pairs(SYNC_PATTERN)) >= SYNC_SYMBOLS // 2
    assert CODEC.interleaver.is_valid()


_check_constants()
