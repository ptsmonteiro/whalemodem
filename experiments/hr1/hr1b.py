"""HR1-B oracle candidate: guarded coded 16-MFSK with symbol metrics.

This remains experiment-local.  Compared with HR1-A it transmits silence in
the discarded 8 ms guard, uses a shorter balanced class word, and replaces
independent bit metrics with a GF(16) convolutional inner code.  The full
class adds a shortened RS(96,70) outer code; the tiny ACK class uses a faster
inner code so a clean stop-and-wait projection remains above 18 bit/s.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass
from functools import cached_property

import numpy as np
from scipy.special import i0e

from experiments.hr1 import hr1
from whale import framing, rx_audio, waveform
from whale.dsp.bits import pn_bits


TX_SAMPLE_RATE = hr1.TX_SAMPLE_RATE
RX_SAMPLE_RATE = hr1.RX_SAMPLE_RATE
OBS_TX_SAMPLES = hr1.OBS_TX_SAMPLES
OBS_RX_SAMPLES = hr1.OBS_RX_SAMPLES
SYMBOL_TX_SAMPLES = hr1.SYMBOL_TX_SAMPLES
SYMBOL_RX_SAMPLES = hr1.SYMBOL_RX_SAMPLES
SYMBOL_SECONDS = hr1.SYMBOL_SECONDS
TONE_COUNT = hr1.TONE_COUNT
TONE_HZ = hr1.TONE_HZ
RX_TONE_BINS = hr1.RX_TONE_BINS
TX_AMPLITUDE = hr1.TX_AMPLITUDE
ACTIVE_RAMP_TX_SAMPLES = 24  # 0.5 ms raised-cosine-squared edge
LEAD_TX_SAMPLES = hr1.LEAD_TX_SAMPLES
LEAD_RX_SAMPLES = hr1.LEAD_RX_SAMPLES
TAIL_TX_SAMPLES = hr1.TAIL_TX_SAMPLES
TAIL_RX_SAMPLES = hr1.TAIL_RX_SAMPLES
MAX_PHYSICAL_PAYLOAD_BYTES = 64
CHUNK_SIZE = MAX_PHYSICAL_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES
TINY_MAX_PAYLOAD_BYTES = 12
TINY_CLASS = 0
FULL_CLASS = 1
CLASS_SYMBOLS = 32
CLASS_SEEDS = (0x16201, 0x16202)
WHITENER_SEED = 0x0C4B1
HOPPING_SEEDS = (0x0B6D1, 0x0B6D2)
RS_PARITY_BYTES = 26
FULL_PACKET_BYTES = 2 + MAX_PHYSICAL_PAYLOAD_BYTES + 4
FULL_RS_BYTES = FULL_PACKET_BYTES + RS_PARITY_BYTES
TINY_PACKET_BYTES = 2 + TINY_MAX_PAYLOAD_BYTES + 4
FULL_INPUT_SYMBOLS = 2 * FULL_RS_BYTES + 2
TINY_INPUT_SYMBOLS = 2 * TINY_PACKET_BYTES + 2
FULL_INNER_RATE_DENOMINATOR = 4
TINY_INNER_RATE_DENOMINATOR = 3
FULL_BODY_SYMBOLS = FULL_INPUT_SYMBOLS * FULL_INNER_RATE_DENOMINATOR
TINY_BODY_SYMBOLS = TINY_INPUT_SYMBOLS * TINY_INNER_RATE_DENOMINATOR
FULL_FRAME_TX_SAMPLES = (LEAD_TX_SAMPLES
                         + (CLASS_SYMBOLS + FULL_BODY_SYMBOLS)
                         * SYMBOL_TX_SAMPLES + TAIL_TX_SAMPLES)
TINY_FRAME_TX_SAMPLES = (LEAD_TX_SAMPLES
                         + (CLASS_SYMBOLS + TINY_BODY_SYMBOLS)
                         * SYMBOL_TX_SAMPLES + TAIL_TX_SAMPLES)
FULL_FRAME_SECONDS = FULL_FRAME_TX_SAMPLES / TX_SAMPLE_RATE
TINY_FRAME_SECONDS = TINY_FRAME_TX_SAMPLES / TX_SAMPLE_RATE
CLEAN_SESSION_SECONDS = FULL_FRAME_SECONDS + TINY_FRAME_SECONDS + 0.6
CLEAN_SESSION_RATE = 54 * 8 / CLEAN_SESSION_SECONDS

COARSE_TIMING_SAMPLES = hr1.COARSE_TIMING_SAMPLES
FINE_TIMING_SAMPLES = hr1.FINE_TIMING_SAMPLES
CFO_STEP_HZ = hr1.CFO_STEP_HZ
CFO_HYPOTHESES = hr1.CFO_HYPOTHESES
MAX_ACQUISITION_START_SAMPLES = hr1.MAX_ACQUISITION_START_SAMPLES
MAX_CAPTURE_SAMPLES = 24 * RX_SAMPLE_RATE
MAX_CANDIDATES = hr1.MAX_CANDIDATES
RAW_CANDIDATES_PER_CLASS_CFO = hr1.RAW_CANDIDATES_PER_CLASS_CFO
ACQUISITION_THRESHOLD = 0.012
EXPERIMENTAL_MODE_ID = 241


def _class_word(seed: int) -> np.ndarray:
    bits = pn_bits(2 * 16 * 17, seed).reshape(2, 16, 17)
    weights = 1 << np.arange(16, -1, -1, dtype=np.int64)
    tones = np.arange(16, dtype=np.int64)
    return np.concatenate([
        tones[np.lexsort((tones, block.astype(np.int64) @ weights))]
        for block in bits
    ])


CLASS_WORDS = tuple(_class_word(seed) for seed in CLASS_SEEDS)


# GF(16), primitive polynomial x^4+x+1.  The two-memory rate-1/n code emits
# u + a_j*s1 + b_j*s2.  A bounded design-time search over 500 coefficient
# pairs found free symbol distance 12 for n=4; the first three outputs give
# the tiny class its deliberately faster rate.
GF16_MUL = np.zeros((16, 16), dtype=np.uint8)
for _left in range(16):
    for _right in range(16):
        _a, _b, _value = _left, _right, 0
        while _b:
            if _b & 1:
                _value ^= _a
            _b >>= 1
            _a <<= 1
            if _a & 0x10:
                _a ^= 0x13
        GF16_MUL[_left, _right] = _value


@dataclass(frozen=True)
class Gf16ConvolutionalCode:
    denominator: int
    a: tuple[int, ...] = (15, 14, 1, 13)
    b: tuple[int, ...] = (13, 9, 2, 10)

    def __post_init__(self):
        if self.denominator not in (3, 4):
            raise ValueError("GF(16) inner denominator must be 3 or 4")

    @cached_property
    def outputs(self) -> np.ndarray:
        states = np.arange(256)
        s1, s2 = states >> 4, states & 15
        result = np.empty((16, 16, 16, self.denominator), dtype=np.uint8)
        aa = np.asarray(self.a[:self.denominator])
        bb = np.asarray(self.b[:self.denominator])
        for value in range(16):
            flat = (value ^ GF16_MUL[aa[:, None], s1].T
                    ^ GF16_MUL[bb[:, None], s2].T)
            result[value] = flat.reshape(16, 16, self.denominator)
        return result

    def encode(self, information: np.ndarray) -> np.ndarray:
        values = np.asarray(information, dtype=np.uint8).reshape(-1)
        if np.any(values > 15):
            raise ValueError("GF(16) symbols must be in 0..15")
        output = np.empty(len(values) * self.denominator, dtype=np.uint8)
        s1 = s2 = 0
        aa = np.asarray(self.a[:self.denominator])
        bb = np.asarray(self.b[:self.denominator])
        for at, value in enumerate(values):
            output[at * self.denominator:(at + 1) * self.denominator] = (
                int(value) ^ GF16_MUL[aa, s1] ^ GF16_MUL[bb, s2])
            s2, s1 = s1, int(value)
        return output

    def decode(self, symbol_costs: np.ndarray) -> tuple[np.ndarray, dict]:
        costs = np.asarray(symbol_costs, dtype=np.float64)
        if costs.ndim != 2 or costs.shape[1] != 16:
            raise ValueError("symbol costs must have shape (N,16)")
        if len(costs) % self.denominator:
            raise ValueError("symbol costs do not contain whole trellis steps")
        steps = len(costs) // self.denominator
        metrics = np.full((16, 16), np.inf)
        metrics[0, 0] = 0.0
        previous_s2 = np.empty((steps, 16, 16), dtype=np.uint8)
        columns = np.arange(self.denominator)[None, None, :]
        for at in range(steps):
            block = costs[at * self.denominator:(at + 1) * self.denominator]
            updated = np.empty((16, 16), dtype=np.float64)
            for value in range(16):
                branch = block[columns, self.outputs[value]].sum(axis=2)
                candidates = metrics + branch
                updated[value] = np.min(candidates, axis=1)
                previous_s2[at, value] = np.argmin(candidates, axis=1)
            updated -= np.min(updated)
            metrics = updated
        decoded = np.empty(steps, dtype=np.uint8)
        value = s1 = 0
        for at in range(steps - 1, -1, -1):
            decoded[at] = value
            old_s2 = int(previous_s2[at, value, s1])
            value, s1 = s1, old_s2
        return decoded, {
            "gf16_viterbi_steps": steps,
            "gf16_viterbi_states": 256,
            "gf16_viterbi_branches": steps * 256 * 16,
        }


FULL_CODE = Gf16ConvolutionalCode(4)
TINY_CODE = Gf16ConvolutionalCode(3)


# Shortened RS(96,70), primitive polynomial 0x11d, correcting 13 bytes.
_GF_EXP = np.zeros(510, dtype=np.uint8)
_GF_LOG = np.zeros(256, dtype=np.int16)
_gf_value = 1
for _gf_index in range(255):
    _GF_EXP[_gf_index] = _gf_value
    _GF_LOG[_gf_value] = _gf_index
    _gf_value <<= 1
    if _gf_value & 0x100:
        _gf_value ^= 0x11D
_GF_EXP[255:] = _GF_EXP[:255]


def _gf_mul(left: int, right: int) -> int:
    if left == 0 or right == 0:
        return 0
    return int(_GF_EXP[int(_GF_LOG[left]) + int(_GF_LOG[right])])


def _gf_div(left: int, right: int) -> int:
    if right == 0:
        raise ZeroDivisionError("GF(256) division by zero")
    if left == 0:
        return 0
    return int(_GF_EXP[(int(_GF_LOG[left]) - int(_GF_LOG[right])) % 255])


def _gf_pow2(power: int) -> int:
    return int(_GF_EXP[power % 255])


def _poly_mul(left: list[int], right: list[int]) -> list[int]:
    product = [0] * (len(left) + len(right) - 1)
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            product[i + j] ^= _gf_mul(a, b)
    return product


def _poly_eval_high(coefficients, x: int) -> int:
    result = 0
    for coefficient in coefficients:
        result = _gf_mul(result, x) ^ int(coefficient)
    return result


def _rs_generator(parity_bytes: int) -> list[int]:
    generator = [1]
    for i in range(parity_bytes):
        generator = _poly_mul(generator, [1, _gf_pow2(i)])
    return generator


_RS_GENERATOR = _rs_generator(RS_PARITY_BYTES)


def _rs_encode(data: bytes) -> bytes:
    if len(data) != FULL_PACKET_BYTES:
        raise ValueError("full RS input has the wrong length")
    work = bytearray(data) + bytearray(RS_PARITY_BYTES)
    for i in range(FULL_PACKET_BYTES):
        coefficient = work[i]
        if coefficient:
            for j in range(1, len(_RS_GENERATOR)):
                work[i + j] ^= _gf_mul(_RS_GENERATOR[j], coefficient)
    return bytes(data) + bytes(work[-RS_PARITY_BYTES:])


def _error_locator(syndromes: list[int]) -> list[int]:
    count = len(syndromes)
    locator = [1] + [0] * count
    previous = [1] + [0] * count
    degree, age, scale0 = 0, 1, 1
    for n in range(count):
        discrepancy = syndromes[n]
        for i in range(1, degree + 1):
            discrepancy ^= _gf_mul(locator[i], syndromes[n - i])
        if discrepancy == 0:
            age += 1
            continue
        saved = locator.copy()
        scale = _gf_div(discrepancy, scale0)
        for i in range(count + 1 - age):
            locator[i + age] ^= _gf_mul(scale, previous[i])
        if 2 * degree <= n:
            degree = n + 1 - degree
            previous, scale0, age = saved, discrepancy, 1
        else:
            age += 1
    if degree * 2 > count:
        raise ValueError("too many RS errors")
    return locator[:degree + 1]


def _solve_gf(matrix: list[list[int]], vector: list[int]) -> list[int]:
    size = len(vector)
    augmented = [matrix[row][:] + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = next((row for row in range(column, size)
                      if augmented[row][column]), None)
        if pivot is None:
            raise ValueError("singular RS error system")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for j in range(column, size + 1):
            augmented[column][j] = _gf_div(augmented[column][j], divisor)
        for row in range(size):
            if row == column or augmented[row][column] == 0:
                continue
            scale = augmented[row][column]
            for j in range(column, size + 1):
                augmented[row][j] ^= _gf_mul(scale, augmented[column][j])
    return [augmented[row][-1] for row in range(size)]


def _rs_decode(codeword: bytes) -> tuple[bytes, int]:
    if len(codeword) != FULL_RS_BYTES:
        raise ValueError("full RS codeword has the wrong length")
    syndromes = [_poly_eval_high(codeword, _gf_pow2(i))
                 for i in range(RS_PARITY_BYTES)]
    if not any(syndromes):
        return codeword[:FULL_PACKET_BYTES], 0
    locator = _error_locator(syndromes)
    error_count = len(locator) - 1
    positions = []
    for position in range(FULL_RS_BYTES):
        power = FULL_RS_BYTES - 1 - position
        x = _gf_pow2(-power)
        value = 0
        for coefficient in reversed(locator):
            value = _gf_mul(value, x) ^ coefficient
        if value == 0:
            positions.append(position)
    if len(positions) != error_count:
        raise ValueError("could not locate all RS errors")
    matrix = [[_gf_pow2(i * (FULL_RS_BYTES - 1 - position))
               for position in positions] for i in range(error_count)]
    magnitudes = _solve_gf(matrix, syndromes[:error_count])
    corrected = bytearray(codeword)
    for position, magnitude in zip(positions, magnitudes):
        corrected[position] ^= magnitude
    if any(_poly_eval_high(corrected, _gf_pow2(i))
           for i in range(RS_PARITY_BYTES)):
        raise ValueError("RS correction did not clear syndromes")
    return bytes(corrected[:FULL_PACKET_BYTES]), error_count


def _packet(payload: bytes, packet_bytes: int) -> bytes:
    result = bytearray(packet_bytes)
    result[:2] = len(payload).to_bytes(2, "big")
    result[2:2 + len(payload)] = payload
    at = 2 + len(payload)
    result[at:at + 4] = (binascii.crc32(payload) & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(result)


def _check_packet(packet: bytes, maximum: int) -> tuple[bytes | None, dict]:
    length = int.from_bytes(packet[:2], "big")
    meta = {"decoded_length": length, "crc_ok": False, "zero_fill_ok": False}
    if length > maximum:
        meta["failure"] = "invalid length"
        return None, meta
    payload = packet[2:2 + length]
    at = 2 + length
    got = int.from_bytes(packet[at:at + 4], "big")
    want = binascii.crc32(payload) & 0xFFFFFFFF
    meta["crc_ok"] = got == want
    meta["zero_fill_ok"] = not any(packet[at + 4:])
    if not meta["crc_ok"]:
        meta["failure"] = "CRC mismatch"
        return None, meta
    if not meta["zero_fill_ok"]:
        meta["failure"] = "non-zero packet fill"
        return None, meta
    return payload, meta


def _whitener(byte_count: int) -> np.ndarray:
    return np.packbits(pn_bits(byte_count * 8, WHITENER_SEED)).tobytes()


def _xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def _hop_masks(count: int, class_id: int) -> np.ndarray:
    return np.packbits(
        pn_bits(count * 4, HOPPING_SEEDS[class_id]).reshape(-1, 4),
        axis=1, bitorder="big").reshape(-1) >> 4


def encode_tones(payload: bytes) -> tuple[int, np.ndarray]:
    payload = bytes(payload)
    if len(payload) <= TINY_MAX_PAYLOAD_BYTES:
        class_id, packet_bytes, code = TINY_CLASS, TINY_PACKET_BYTES, TINY_CODE
        protected = _xor_bytes(_packet(payload, packet_bytes),
                               _whitener(packet_bytes))
    elif len(payload) <= MAX_PHYSICAL_PAYLOAD_BYTES:
        class_id, packet_bytes, code = FULL_CLASS, FULL_PACKET_BYTES, FULL_CODE
        whitened = _xor_bytes(_packet(payload, packet_bytes),
                              _whitener(packet_bytes))
        protected = _rs_encode(whitened)
    else:
        raise ValueError("payload exceeds HR1-B full class")
    nibbles = np.empty(2 * len(protected) + 2, dtype=np.uint8)
    raw = np.frombuffer(protected, dtype=np.uint8)
    nibbles[:-2:2], nibbles[1:-2:2] = raw >> 4, raw & 15
    nibbles[-2:] = 0
    tones = code.encode(nibbles)
    return class_id, tones ^ _hop_masks(len(tones), class_id)


def _decode_tones(costs: np.ndarray, class_id: int) -> tuple[bytes | None, dict]:
    count = TINY_BODY_SYMBOLS if class_id == TINY_CLASS else FULL_BODY_SYMBOLS
    unhop = np.empty_like(costs)
    masks = _hop_masks(count, class_id)
    for at, mask in enumerate(masks):
        unhop[at] = costs[at, np.arange(16) ^ mask]
    code = TINY_CODE if class_id == TINY_CLASS else FULL_CODE
    symbols, meta = code.decode(unhop)
    meta["fec_tail_ok"] = bool(not np.any(symbols[-2:]))
    if not meta["fec_tail_ok"]:
        meta["failure"] = "GF(16) termination mismatch"
        return None, meta
    raw = ((symbols[:-2:2] << 4) | symbols[1:-2:2]).astype(np.uint8).tobytes()
    if class_id == FULL_CLASS:
        try:
            raw, corrected = _rs_decode(raw)
        except ValueError as exc:
            meta.update(rs_ok=False, rs_corrected_bytes=None, failure=str(exc))
            return None, meta
        meta.update(rs_ok=True, rs_corrected_bytes=corrected)
        packet_bytes, maximum = FULL_PACKET_BYTES, MAX_PHYSICAL_PAYLOAD_BYTES
    else:
        meta.update(rs_ok=None, rs_corrected_bytes=0)
        packet_bytes, maximum = TINY_PACKET_BYTES, TINY_MAX_PAYLOAD_BYTES
    packet = _xor_bytes(raw, _whitener(packet_bytes))
    payload, checked = _check_packet(packet, maximum)
    meta.update(checked)
    return payload, meta


def _modulated_observation(tone: int) -> np.ndarray:
    index = np.arange(OBS_TX_SAMPLES, dtype=np.float64)
    return TX_AMPLITUDE * np.cos(2 * np.pi * TONE_HZ[tone] * index
                                 / TX_SAMPLE_RATE)


def _modulate_symbols(tones: np.ndarray) -> np.ndarray:
    # The peak-limited energy is concentrated in the two observations the
    # receiver can trust after the repository's 7 ms maximum delay.
    output = np.zeros(len(tones) * SYMBOL_TX_SAMPLES, dtype=np.float64)
    for at, tone in enumerate(np.asarray(tones, dtype=np.int64)):
        active = np.tile(_modulated_observation(int(tone)), 2)
        ramp = np.sin(np.linspace(0, np.pi / 2, ACTIVE_RAMP_TX_SAMPLES)) ** 2
        active[:ACTIVE_RAMP_TX_SAMPLES] *= ramp
        active[-ACTIVE_RAMP_TX_SAMPLES:] *= ramp[::-1]
        start = at * SYMBOL_TX_SAMPLES + OBS_TX_SAMPLES
        output[start:start + 2 * OBS_TX_SAMPLES] = active
    return output


def encode(payload: bytes, *, include_head: bool = True,
           head_seconds: float = hr1.LEAD_SECONDS) -> np.ndarray:
    if head_seconds < 0:
        raise ValueError("head duration must not be negative")
    lead_samples = 0 if not include_head else max(
        LEAD_TX_SAMPLES,
        int(np.ceil(head_seconds / hr1.OBSERVATION_SECONDS)) * OBS_TX_SAMPLES)
    class_id, tones = encode_tones(payload)
    lead = hr1._modulate_observations(
        hr1._lead_tones(lead_samples), OBS_TX_SAMPLES, TX_SAMPLE_RATE)
    if len(lead):
        fade = min(240, len(lead))
        lead[:fade] *= np.linspace(0, 1, fade)
    frame = np.concatenate((lead, _modulate_symbols(CLASS_WORDS[class_id]),
                            _modulate_symbols(tones),
                            np.zeros(TAIL_TX_SAMPLES)))
    return frame.astype(np.float32)


def _correlations(audio: np.ndarray, start: int, symbols: int,
                  cfo_hz: float, *, all_observations: bool = False):
    span = symbols * SYMBOL_RX_SAMPLES
    if start < 0 or start + span > len(audio):
        return None
    segment = np.asarray(audio[start:start + span], dtype=np.float64)
    absolute = start + np.arange(span)
    mixed = segment * np.exp(-2j * np.pi * cfo_hz * absolute / RX_SAMPLE_RATE)
    grid = mixed.reshape(symbols, 3, OBS_RX_SAMPLES)
    selected = grid if all_observations else grid[:, 1:, :]
    return np.fft.fft(selected, axis=-1)[..., RX_TONE_BINS]


def _symbol_costs(correlations: np.ndarray,
                  reference: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    # The two active observations are phase-continuous in AWGN.  Coherent
    # pair combining recovers the 3 dB that independent noncoherent products
    # throw away at this very low rate.  The 8 ms pair is still much shorter
    # than HR1-A's 24 ms coherent warning; Watterson smoke tests decide whether
    # this remains acceptable at the 30 Hz registry extreme.
    combined = np.sum(correlations, axis=1)
    source0 = correlations if reference is None else reference
    source = np.sum(source0, axis=1)
    # Off-tone preamble bins give an AWGN-aligned complex-bin noise estimate.
    noise2 = float(np.median(np.abs(source) ** 2) / np.log(2.0))
    maxima = np.max(np.abs(source) ** 2, axis=-1)
    signal2 = max(float(np.median(maxima)) - noise2, noise2 * 1e-3)
    scale = 2.0 * np.sqrt(signal2) / max(noise2, 1e-30)
    argument = scale * np.abs(combined)
    log_likelihood = np.log(i0e(argument)) + np.abs(argument)
    return -log_likelihood, {
        "estimated_bin_noise_power": noise2,
        "estimated_bin_signal_power": signal2,
        "likelihood_scale": scale,
        "observation_combining": "coherent_last_two_8ms_observations",
    }


def _pattern_score(audio: np.ndarray, start: int, cfo_hz: float,
                   class_id: int) -> float:
    values = _correlations(audio, start, CLASS_SYMBOLS, cfo_hz)
    if values is None:
        return -1.0
    energy = np.sum(np.abs(values) ** 2, axis=1)
    hit = float(np.sum(energy[np.arange(CLASS_SYMBOLS), CLASS_WORDS[class_id]]))
    total = float(np.sum(energy))
    ratio = hit / max(total, 1e-30)
    return (ratio - 1 / 16) / (1 - 1 / 16)


@dataclass(frozen=True)
class _Candidate:
    score: float
    start: int
    cfo_hz: float
    class_id: int


def _coarse_acquire(audio: np.ndarray) -> tuple[list[_Candidate], int]:
    span = CLASS_SYMBOLS * SYMBOL_RX_SAMPLES
    maximum_start = min(MAX_ACQUISITION_START_SAMPLES, len(audio) - span)
    if maximum_start < 0:
        return [], 0
    starts = np.arange(0, maximum_start + 1, COARSE_TIMING_SAMPLES,
                       dtype=np.int64)
    window_count = ((maximum_start + span - OBS_RX_SAMPLES)
                    // COARSE_TIMING_SAMPLES + 1)
    view = np.lib.stride_tricks.sliding_window_view(audio, OBS_RX_SAMPLES)
    windows = view[:window_count * COARSE_TIMING_SAMPLES:
                   COARSE_TIMING_SAMPLES]
    power = np.abs(np.fft.rfft(windows, n=768, axis=1)) ** 2
    start_cells = starts // COARSE_TIMING_SAMPLES
    offsets = np.stack((np.arange(CLASS_SYMBOLS) * 12 + 4,
                        np.arange(CLASS_SYMBOLS) * 12 + 8), axis=1)
    rows = []
    for cfo_index, cfo in zip(range(-13, 14), CFO_HYPOTHESES):
        bins = 24 + cfo_index + 8 * np.arange(TONE_COUNT)
        bank = power[:, bins]
        picked = bank[start_cells[:, None, None] + offsets]
        total = np.sum(picked, axis=(1, 2, 3))
        for class_id, pattern in enumerate(CLASS_WORDS):
            expected = np.take_along_axis(
                picked, pattern[None, :, None, None], axis=3)[..., 0]
            hit = np.sum(expected, axis=(1, 2))
            ratio = hit / np.maximum(total, 1e-30)
            scores = (ratio - 1 / 16) / (1 - 1 / 16)
            keep = min(RAW_CANDIDATES_PER_CLASS_CFO, len(scores))
            if keep:
                best = np.argpartition(scores, -keep)[-keep:]
                rows.extend(_Candidate(float(scores[at]), int(starts[at]),
                                       float(cfo), class_id)
                            for at in best
                            if scores[at] >= ACQUISITION_THRESHOLD)
    rows.sort(key=lambda row: row.score, reverse=True)
    distinct = []
    for row in rows:
        if any(row.class_id == old.class_id
               and abs(row.start - old.start) <= COARSE_TIMING_SAMPLES
               and abs(row.cfo_hz - old.cfo_hz) <= CFO_STEP_HZ
               for old in distinct):
            continue
        distinct.append(row)
        if len(distinct) == MAX_CANDIDATES:
            break
    return distinct, len(starts) * len(CFO_HYPOTHESES) * len(CLASS_WORDS)


def _refine(audio: np.ndarray, candidate: _Candidate) -> tuple[_Candidate, int]:
    choices = []
    for start in range(max(0, candidate.start - OBS_RX_SAMPLES),
                       candidate.start + OBS_RX_SAMPLES + 1,
                       FINE_TIMING_SAMPLES):
        for delta in (-CFO_STEP_HZ / 2, 0, CFO_STEP_HZ / 2):
            choices.append(_Candidate(
                _pattern_score(audio, start, candidate.cfo_hz + delta,
                               candidate.class_id),
                start, candidate.cfo_hz + delta, candidate.class_id))
    return max(choices, key=lambda row: row.score), len(choices)


def _base_result() -> dict:
    return {"synced": False, "payload": None, "confidence": 0.0,
            "start_index": None, "end_index": None, "cfo_hz": 0.0,
            "candidate_limit": MAX_CANDIDATES, "candidate_count": 0,
            "candidates_tried": 0, "search_cells_evaluated": 0,
            "refinement_cells_evaluated": 0,
            "total_gf16_viterbi_branches": 0}


def _decode_at(audio: np.ndarray, start: int, cfo_hz: float,
               class_id: int, confidence: float) -> dict:
    result = _base_result()
    result.update(synced=True, confidence=float(confidence),
                  start_index=int(start), cfo_hz=float(cfo_hz),
                  acquired_class=class_id)
    preamble = _correlations(audio, start, CLASS_SYMBOLS, cfo_hz)
    body_start = start + CLASS_SYMBOLS * SYMBOL_RX_SAMPLES
    count = TINY_BODY_SYMBOLS if class_id == TINY_CLASS else FULL_BODY_SYMBOLS
    body = _correlations(audio, body_start, count, cfo_hz)
    if body is None or preamble is None:
        result["failure"] = "frame truncated"
        return result
    costs, metric_meta = _symbol_costs(body, preamble)
    payload, meta = _decode_tones(costs, class_id)
    result.update(metric_meta, **meta)
    result["payload"] = payload
    result["end_index"] = min(len(audio), body_start + count * SYMBOL_RX_SAMPLES
                              + TAIL_RX_SAMPLES)
    return result


def decode_aligned(audio: np.ndarray, *, preamble_start: int,
                   class_id: int = FULL_CLASS, cfo_hz: float = 0.0) -> dict:
    samples = np.asarray(audio)
    if samples.ndim != 1 or not np.all(np.isfinite(samples)):
        result = _base_result()
        result["failure"] = "audio must be finite and one-dimensional"
        return result
    return _decode_at(samples.astype(np.float64, copy=False), preamble_start,
                      cfo_hz, class_id, 1.0)


def decode(audio: np.ndarray, **_kwargs) -> dict:
    result = _base_result()
    try:
        samples = np.asarray(audio)
        valid = samples.ndim == 1 and np.all(np.isfinite(samples))
        if valid:
            samples = samples.astype(np.float64, copy=False)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        result["failure"] = "audio must be finite and one-dimensional"
        return result
    if len(samples) > MAX_CAPTURE_SAMPLES:
        samples = samples[:MAX_CAPTURE_SAMPLES]
        result["capture_truncated_to_limit"] = True
    if len(samples) < CLASS_SYMBOLS * SYMBOL_RX_SAMPLES or not np.any(samples):
        result["failure"] = "capture too short or silent"
        return result
    candidates, cells = _coarse_acquire(samples)
    search_cells = cells
    result.update(candidate_count=len(candidates),
                  search_cells_evaluated=search_cells)
    if not candidates:
        result["failure"] = "class word not found"
        return result
    result["confidence"] = candidates[0].score
    total_branches = 0
    for rank, coarse in enumerate(candidates, 1):
        refined, cells = _refine(samples, coarse)
        result["candidates_tried"] += 1
        result["refinement_cells_evaluated"] += cells
        decoded = _decode_at(samples, refined.start, refined.cfo_hz,
                             refined.class_id, refined.score)
        total_branches += int(decoded.get("gf16_viterbi_branches", 0))
        decoded.update(candidate_limit=MAX_CANDIDATES,
                       candidate_count=len(candidates), candidate_rank=rank,
                       candidates_tried=result["candidates_tried"],
                       search_cells_evaluated=search_cells,
                       refinement_cells_evaluated=(
                           result["refinement_cells_evaluated"]),
                       total_gf16_viterbi_branches=total_branches)
        if decoded.get("payload") is not None:
            return decoded
        result.update(decoded)
    result["payload"] = None
    return result


@dataclass(frozen=True)
class Hr1BMode:
    name: str = "hr1-b-exp"
    mode_id: int = EXPERIMENTAL_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = ACQUISITION_THRESHOLD
    tx_sample_rate: int = TX_SAMPLE_RATE
    rx_sample_rate: int = RX_SAMPLE_RATE

    @property
    def baud(self) -> float:
        return 1 / SYMBOL_SECONDS

    @property
    def head_match_allowance_seconds(self) -> float:
        return hr1.OBSERVATION_SECONDS

    def encode(self, payload: bytes, *, include_head: bool = True,
               head_seconds: float = hr1.LEAD_SECONDS) -> np.ndarray:
        return encode(payload, include_head=include_head,
                      head_seconds=head_seconds)

    def decode(self, audio: np.ndarray, **kwargs) -> dict:
        return decode(audio, **kwargs)

    def airtime(self, payload_len: int) -> float:
        if payload_len <= TINY_MAX_PAYLOAD_BYTES:
            return TINY_FRAME_SECONDS
        if payload_len <= MAX_PHYSICAL_PAYLOAD_BYTES:
            return FULL_FRAME_SECONDS
        raise ValueError("payload exceeds HR1-B full class")


HR1B = Hr1BMode()


assert isinstance(HR1B, waveform.WaveformMode)
assert FULL_BODY_SYMBOLS == 776 and TINY_BODY_SYMBOLS == 114
assert FULL_FRAME_SECONDS == 19.54 and TINY_FRAME_SECONDS == 3.652
assert CLEAN_SESSION_RATE > 18.0
