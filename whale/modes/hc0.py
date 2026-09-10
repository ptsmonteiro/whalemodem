"""HC0: robust noncoherent 16-FSK for HF control and fallback data.

The receiver correlates a known tone pattern, estimates carrier offset, and
decodes an interleaved terminated rate-1/2 K=7 packet with length and CRC32.
Frames use HC0's native fixed preamble.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .. import dsp, framing, rx_audio
from ..dsp import mfsk as _mfsk

# -- geometry -------------------------------------------------------------

SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE

#: 512 samples: 10.667 ms, so 93.75 baud and 93.75 Hz of tone spacing.
#: Slow enough that a symbol carries real energy, fast enough that 283 of
#: them still fit in a keying an operator will tolerate.
SYMBOL_SAMPLES = 512
RX_SYMBOL_SAMPLES = SYMBOL_SAMPLES // rx_audio.DECIMATION

#: 16 tones from bin 8, i.e. 750 Hz to 2,156.25 Hz.  1.5 kHz of occupied
#: bandwidth, centred at 1,453 Hz -- comfortably inside a 2.4 kHz SSB data
#: filter with both skirts clear, so nothing depends on where the receiver
#: happens to have placed its passband.
FIRST_BIN = 8
TONE_COUNT = 16

BANK = _mfsk.ToneBank(sample_rate=SAMPLE_RATE, symbol_samples=SYMBOL_SAMPLES,
                      first_bin=FIRST_BIN, tone_count=TONE_COUNT)
RX_BANK = _mfsk.ToneBank(sample_rate=RX_SAMPLE_RATE,
                         symbol_samples=RX_SYMBOL_SAMPLES,
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
PAYLOAD_SYMBOLS = 432
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

HEAD_BLOCK_SYMBOLS = 4
HEAD_BLOCK_SAMPLES = HEAD_BLOCK_SYMBOLS * SYMBOL_SAMPLES
LEAD_IN_FADE_SAMPLES = 240
TAIL_SAMPLES = 960
RX_TAIL_SAMPLES = TAIL_SAMPLES // rx_audio.DECIMATION

HEAD_SAMPLES = int(np.ceil(framing.HEAD_SECONDS * SAMPLE_RATE
                           / HEAD_BLOCK_SAMPLES)) * HEAD_BLOCK_SAMPLES


def frame_samples() -> int:
    return HEAD_SAMPLES + TOTAL_SYMBOLS * SYMBOL_SAMPLES + TAIL_SAMPLES


def frame_seconds() -> float:
    return frame_samples() / SAMPLE_RATE


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
    interleaver=dsp.interleave.multiplicative(PAYLOAD_BITS, 811),
    whitener_seed=0x0C4B1,
    code=dsp.K7,
)

# HC0 originally shipped with a 64-byte payload grid.  Keep that receive
# shape here so captures made by that waveform remain useful after the
# current control-frame envelope grew to 101 bytes.  This is receive-only:
# transmitters always emit the current geometry above, and a candidate only
# wins this fallback when its CRC validates.
LEGACY_PAYLOAD_SYMBOLS = 283
LEGACY_PAYLOAD_BITS = LEGACY_PAYLOAD_SYMBOLS * BITS_PER_SYMBOL
LEGACY_CODEC = dsp.PacketCodec(
    payload_bits=LEGACY_PAYLOAD_BITS,
    interleaver=dsp.interleave.multiplicative(LEGACY_PAYLOAD_BITS, 693),
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


def modulate(payload: bytes) -> np.ndarray:
    tones = np.concatenate((
        SYNC_PATTERN,
        BANK.symbols_from_bits(encode_payload_bits(payload)),
    ))
    body = _mfsk.modulate(BANK, tones, TX_AMPLITUDE)
    lead = np.resize(head_block(), HEAD_SAMPLES).copy()
    fade = LEAD_IN_FADE_SAMPLES
    lead[:fade] *= np.linspace(0.0, 1.0, fade, endpoint=True)
    audio = np.concatenate((lead, body, np.zeros(TAIL_SAMPLES)))
    if len(audio) != frame_samples():
        raise AssertionError(f"internal frame length error: {len(audio)}")
    peak = float(np.max(np.abs(audio)))
    if peak > MAX_SAMPLE:
        audio *= MAX_SAMPLE / peak
    return audio.astype(np.float32)


# -- demodulation ---------------------------------------------------------

def _base_result() -> dict:
    return {
        "synced": False, "payload": None, "confidence": 0.0,
        "start_index": None, "cfo_hz": 0.0, "raw_payload_bits": None,
    }


def _acquire(audio: np.ndarray) -> tuple[int | None, float]:
    """Best sync-pattern match in `audio`, refined to 8 samples."""
    scores, step = _mfsk.correlate(RX_BANK, audio, SYNC_PATTERN)
    if not len(scores):
        return None, 0.0
    coarse = int(np.argmax(scores))
    if scores[coarse] < ACQUISITION_THRESHOLD:
        return coarse * step, float(scores[coarse])
    start = _mfsk.refine(RX_BANK, audio, SYNC_PATTERN, coarse * step, radius=step)
    score = _mfsk.pattern_score(RX_BANK, audio, SYNC_PATTERN, start)
    return start, float(max(score, scores[coarse]))


def demodulate(audio: np.ndarray) -> dict:
    """Decode one HC0 frame out of `audio`.

    Alongside HC0's own diagnostics the result carries the three keys the
    link's receive loop reads: `confidence`, `sync_end_index` and
    `end_index`.  `end_index` is present only once the frame has been seen
    through to its end -- its absence, with confidence above threshold, is
    how the caller is told to wait for more audio rather than consume what
    it has.  See whale/link.py's _decode_one.
    """
    result = _base_result()
    samples = np.asarray(audio, dtype=np.float64).reshape(-1)
    if len(samples) < SYNC_SYMBOLS * RX_SYMBOL_SAMPLES:
        result["failure"] = "capture shorter than the preamble"
        return result

    start, confidence = _acquire(samples)
    result.update(confidence=confidence, start_index=start)
    if start is None or confidence < ACQUISITION_THRESHOLD:
        result["failure"] = "preamble not found"
        return result
    result["sync_end_index"] = start + SYNC_SYMBOLS * RX_SYMBOL_SAMPLES

    # Measured, never gated on: a bad estimate costs accuracy, not the
    # frame, because nothing in the detector needed the phase it came from.
    offset = _mfsk.offset_hz(RX_BANK, samples, start, SYNC_PATTERN)
    result["cfo_hz"] = offset

    payload_start = result["sync_end_index"]

    def decode_candidate(codec, payload_symbols):
        values = _mfsk.analyze(RX_BANK, samples, payload_start,
                                payload_symbols, offset)
        if values is None:
            return None
        magnitudes = np.abs(values)
        payload, meta = codec.decode_soft(
            _mfsk.soft_bits(RX_BANK, magnitudes))
        return magnitudes, payload, meta

    # Prefer the current geometry whenever it is complete.  If it is not a
    # valid current frame, try the old fixed-length geometry as well; this is
    # what lets retained on-air captures survive the payload-grid expansion.
    candidate = decode_candidate(CODEC, PAYLOAD_SYMBOLS)
    legacy = False
    if candidate is None or not candidate[2].get("crc_ok", False):
        legacy_candidate = decode_candidate(LEGACY_CODEC,
                                            LEGACY_PAYLOAD_SYMBOLS)
        if (legacy_candidate is not None
                and legacy_candidate[2].get("crc_ok", False)):
            candidate = legacy_candidate
            legacy = True

    if candidate is None or not candidate[2].get("crc_ok", False):
        # Still arriving, or a complete frame with a bad CRC.  Deliberately
        # no end_index for the former: the caller must keep this audio and
        # try again rather than consume a partial current frame.
        if candidate is None:
            result["failure"] = "frame truncated"
            return result
        magnitudes, payload, meta = candidate
        result["end_index"] = min(
            len(samples),
            start + TOTAL_SYMBOLS * RX_SYMBOL_SAMPLES + RX_TAIL_SAMPLES)
        result["tone_snr_db"] = _tone_snr_db(magnitudes)
        result["raw_payload_bits"] = RX_BANK.bits_from_symbols(
            np.argmax(magnitudes, axis=1))
        result.update(meta)
        result.update(payload=payload, synced=True,
                      tone_magnitudes=magnitudes)
        return result

    magnitudes, payload, meta = candidate
    total_symbols = (LEGACY_PAYLOAD_SYMBOLS if legacy else PAYLOAD_SYMBOLS)
    result["end_index"] = min(
        len(samples),
        start + SYNC_SYMBOLS * RX_SYMBOL_SAMPLES
        + total_symbols * RX_SYMBOL_SAMPLES + RX_TAIL_SAMPLES)
    result["tone_snr_db"] = _tone_snr_db(magnitudes)
    result["raw_payload_bits"] = RX_BANK.bits_from_symbols(
        np.argmax(magnitudes, axis=1))
    result.update(meta)
    result.update(payload=payload, synced=True, tone_magnitudes=magnitudes)
    if legacy:
        result["legacy_frame"] = True
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


def demodulate_debug(audio: np.ndarray, reference_payload: bytes | None = None) -> dict:
    result = demodulate(audio)
    if reference_payload is None or result.get("raw_payload_bits") is None:
        return result
    codec = LEGACY_CODEC if result.get("legacy_frame") else CODEC
    expected = codec.encode(reference_payload)
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
    assert TOTAL_SYMBOLS == 456 and PAYLOAD_BITS == 1_728
    assert FEC_INPUT_BITS == 864
    # No stranded bits: the coded grid divides exactly into whole packet
    # bytes plus the trellis tail.  This is what picked 283 payload symbols.
    assert PACKET_BYTES == 107 and UNUSED_INFO_BITS == 2
    assert MAX_PAYLOAD_BYTES == 101
    assert FRAME_SAMPLES == 283_584 and FRAME_SECONDS == 5.908
    # Every deliberate pair; a draw that happens to repeat a tone across
    # neighbouring pairs would give more, which is only more of the same
    # measurement.
    assert len(_mfsk.repeated_pairs(SYNC_PATTERN)) >= SYNC_SYMBOLS // 2
    assert CODEC.interleaver.is_valid()


_check_constants()
