"""HR1-A: standalone guarded 16-MFSK robustness experiment.

This module intentionally does not register a production mode.  It implements
the ordinary :class:`whale.waveform.WaveformMode` surface so the matched HR1
benchmark can exercise a real, bounded receiver while the wire format remains
free to change.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass
from functools import cached_property

import numpy as np

from whale import framing, rx_audio, waveform
from whale.dsp.bits import pn_bits
from whale.dsp.interleave import multiplicative


# Signalling geometry.  Every frequency is an integer bin of one 8 ms
# observation.  A coded symbol repeats its tone for three observations; the
# receiver discards the first as a 7 ms multipath guard and noncoherently adds
# the final two.
TX_SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE
OBSERVATION_SECONDS = 0.008
OBS_TX_SAMPLES = 384
OBS_RX_SAMPLES = 96
OBSERVATIONS_PER_SYMBOL = 3
SYMBOL_TX_SAMPLES = OBS_TX_SAMPLES * OBSERVATIONS_PER_SYMBOL
SYMBOL_RX_SAMPLES = OBS_RX_SAMPLES * OBSERVATIONS_PER_SYMBOL
SYMBOL_SECONDS = 0.024
TONE_COUNT = 16
BITS_PER_SYMBOL = 4
TONE_HZ = 375.0 + 125.0 * np.arange(TONE_COUNT)
RX_TONE_BINS = np.arange(3, 19)
TX_AMPLITUDE = 0.13 * np.sqrt(2.0)

# Fixed full class.  The other class words are generated and recognized so a
# wrong class cannot be confused with this body, but point 3 implements only
# the required 64-byte physical frame.
TINY_CLASS = 0
SHORT_CLASS = 1
FULL_CLASS = 2
CLASS_SEEDS = (0x15201, 0x15202, 0x15203)
CLASS_SYMBOLS = 80
MAX_PHYSICAL_PAYLOAD_BYTES = 64
CHUNK_SIZE = MAX_PHYSICAL_PAYLOAD_BYTES - framing.AIR_HEADER_BYTES
PACKET_BYTES = 2 + MAX_PHYSICAL_PAYLOAD_BYTES + 4
FEC_INPUT_BITS = PACKET_BYTES * 8 + 8
CODED_BITS = FEC_INPUT_BITS * 3
DATA_SYMBOLS = CODED_BITS // BITS_PER_SYMBOL
PILOT_INTERVAL = 31
PILOT_SYMBOLS = (DATA_SYMBOLS - 1) // PILOT_INTERVAL
BODY_SYMBOLS = DATA_SYMBOLS + PILOT_SYMBOLS
INTERLEAVER_STRIDE = 851
WHITENER_SEED = 0x0C4B1
HOPPING_SEED = 0x0A6D1 ^ FULL_CLASS

LEAD_SECONDS = 0.128
LEAD_TX_SAMPLES = int(LEAD_SECONDS * TX_SAMPLE_RATE)
LEAD_RX_SAMPLES = int(LEAD_SECONDS * RX_SAMPLE_RATE)
TAIL_TX_SAMPLES = 960
TAIL_RX_SAMPLES = TAIL_TX_SAMPLES // (TX_SAMPLE_RATE // RX_SAMPLE_RATE)
FRAME_TX_SAMPLES = (LEAD_TX_SAMPLES
                    + (CLASS_SYMBOLS + BODY_SYMBOLS) * SYMBOL_TX_SAMPLES
                    + TAIL_TX_SAMPLES)
FRAME_SECONDS = FRAME_TX_SAMPLES / TX_SAMPLE_RATE

# The coarse search exactly follows DESIGN.md: 2 ms timing cells and 15.625
# Hz CFO cells from -13 through +13.  Work is bounded independently of input
# length.  Refinement examines 0.25 ms timing and half-CFO-bin offsets.
COARSE_TIMING_SAMPLES = 24
FINE_TIMING_SAMPLES = 3
CFO_STEP_HZ = 15.625
CFO_HYPOTHESES = np.arange(-13, 14, dtype=np.int64) * CFO_STEP_HZ
MAX_ACQUISITION_START_SAMPLES = 2 * RX_SAMPLE_RATE
MAX_CAPTURE_SAMPLES = 16 * RX_SAMPLE_RATE
MAX_CANDIDATES = 16
RAW_CANDIDATES_PER_CLASS_CFO = 24
ACQUISITION_THRESHOLD = 0.012
REFINEMENT_TIMING_CELLS = (2 * OBS_RX_SAMPLES) // FINE_TIMING_SAMPLES + 1
REFINEMENT_CFO_CELLS = 3
REFINEMENT_CELLS_PER_CANDIDATE = (
    REFINEMENT_TIMING_CELLS * REFINEMENT_CFO_CELLS)

# An experiment-only identifier.  It is deliberately outside the production
# registry and is not a stable on-air assignment.
EXPERIMENTAL_MODE_ID = 240


def _class_word(seed: int) -> np.ndarray:
    bits = pn_bits(5 * 16 * 17, seed).reshape(5, 16, 17)
    weights = 1 << np.arange(16, -1, -1, dtype=np.int64)
    keys = bits.astype(np.int64) @ weights
    blocks = []
    tones = np.arange(16, dtype=np.int64)
    for block_keys in keys:
        # lexsort makes the documented tone-number tie break explicit.
        blocks.append(tones[np.lexsort((tones, block_keys))])
    return np.concatenate(blocks)


CLASS_WORDS = tuple(_class_word(seed) for seed in CLASS_SEEDS)


def _gray(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.int64)
    return values ^ (values >> 1)


GRAY = _gray(np.arange(TONE_COUNT))
UNGRAY = np.argsort(GRAY)
INTERLEAVER = multiplicative(CODED_BITS, INTERLEAVER_STRIDE)
WHITENER = pn_bits(PACKET_BYTES * 8, WHITENER_SEED)
HOP_MASKS = np.packbits(
    pn_bits(DATA_SYMBOLS * BITS_PER_SYMBOL, HOPPING_SEED).reshape(-1, 4),
    axis=1, bitorder="big").reshape(-1) >> 4


@dataclass(frozen=True)
class RateThirdK7:
    """Terminated K=7 convolutional code with three output polynomials."""

    polynomials: tuple[int, int, int] = (0o171, 0o133, 0o165)
    constraint: int = 7

    @property
    def states(self) -> int:
        return 1 << (self.constraint - 1)

    @property
    def tail_bits(self) -> int:
        return self.constraint - 1

    @staticmethod
    def _parity(value: int) -> int:
        return value.bit_count() & 1

    def encode(self, input_bits: np.ndarray) -> np.ndarray:
        values = np.asarray(input_bits, dtype=np.uint8).reshape(-1)
        out = np.empty(3 * len(values), dtype=np.uint8)
        state = 0
        register_mask = (1 << self.constraint) - 1
        state_mask = self.states - 1
        for at, bit in enumerate(values):
            register = ((state << 1) | int(bit)) & register_mask
            out[3 * at:3 * at + 3] = [
                self._parity(register & polynomial)
                for polynomial in self.polynomials
            ]
            state = register & state_mask
        return out

    @cached_property
    def _butterfly(self):
        states = self.states
        predecessors = np.empty((2, states), dtype=np.intp)
        weights = np.empty((2, states, 3), dtype=np.float64)
        input_bits = np.empty(states, dtype=np.uint8)
        register_mask = (1 << self.constraint) - 1
        for next_state in range(states):
            bit = next_state & 1
            input_bits[next_state] = bit
            for branch in (0, 1):
                state = (next_state >> 1) | (branch * (states >> 1))
                predecessors[branch, next_state] = state
                register = ((state << 1) | bit) & register_mask
                emitted = [self._parity(register & p)
                           for p in self.polynomials]
                weights[branch, next_state] = 2 * np.asarray(emitted) - 1
        return predecessors, weights, input_bits

    def decode_soft(self, soft_bits: np.ndarray) -> tuple[np.ndarray, dict]:
        soft = np.asarray(soft_bits, dtype=np.float64).reshape(-1)
        if len(soft) % 3:
            raise ValueError("rate-1/3 soft input must divide into triples")
        received = soft.reshape(-1, 3)
        predecessors, weights, input_bits = self._butterfly
        metrics = np.full(self.states, np.inf)
        metrics[0] = 0.0
        previous = np.empty((len(received), self.states), dtype=np.uint8)
        for at, values in enumerate(received):
            branch0 = metrics[predecessors[0]] + weights[0] @ values
            branch1 = metrics[predecessors[1]] + weights[1] @ values
            take1 = branch1 < branch0
            metrics = np.where(take1, branch1, branch0)
            # Keeping metrics near zero avoids pointless dynamic-range growth.
            metrics -= np.min(metrics)
            previous[at] = np.where(take1, predecessors[1], predecessors[0])
        decoded = np.empty(len(received), dtype=np.uint8)
        state = 0
        for at in range(len(received) - 1, -1, -1):
            decoded[at] = input_bits[state]
            state = int(previous[at, state])
        return decoded, {
            "viterbi_steps": len(received),
            "viterbi_states": self.states,
            "viterbi_branch_metrics": len(received) * self.states * 2,
        }

    def decode_hard(self, coded_bits: np.ndarray) -> tuple[np.ndarray, dict]:
        hard = np.asarray(coded_bits, dtype=np.uint8).reshape(-1)
        return self.decode_soft(1.0 - 2.0 * hard)


CODE = RateThirdK7()


def encode_coded_bits(payload: bytes) -> np.ndarray:
    """Build, whiten, encode, and interleave one fixed full-class packet."""

    payload = bytes(payload)
    if len(payload) > MAX_PHYSICAL_PAYLOAD_BYTES:
        raise ValueError(
            f"payload is {len(payload)} bytes; maximum is "
            f"{MAX_PHYSICAL_PAYLOAD_BYTES}")
    packet = bytearray(PACKET_BYTES)
    packet[:2] = len(payload).to_bytes(2, "big")
    packet[2:2 + len(payload)] = payload
    crc_at = 2 + len(payload)
    packet[crc_at:crc_at + 4] = (
        binascii.crc32(payload) & 0xFFFFFFFF).to_bytes(4, "big")
    information = np.zeros(FEC_INPUT_BITS, dtype=np.uint8)
    information[:PACKET_BYTES * 8] = (
        np.unpackbits(np.frombuffer(packet, dtype=np.uint8)) ^ WHITENER)
    return INTERLEAVER.spread(CODE.encode(information))


def decode_coded_soft(soft_bits: np.ndarray) -> tuple[bytes | None, dict]:
    """Gather and check length, CRC, zero-fill, and all eight tail/pad bits."""

    values = np.asarray(soft_bits, dtype=np.float64).reshape(-1)
    if len(values) != CODED_BITS:
        raise ValueError(f"expected {CODED_BITS} soft bits, got {len(values)}")
    information, work = CODE.decode_soft(INTERLEAVER.gather(values))
    tail_ok = not np.any(information[-8:])
    packet = np.packbits(
        information[:PACKET_BYTES * 8] ^ WHITENER).tobytes()
    length = int.from_bytes(packet[:2], "big")
    meta = {**work, "decoded_length": length, "crc_ok": False,
            "fec_tail_ok": bool(tail_ok), "zero_fill_ok": False}
    if not tail_ok:
        meta["failure"] = "FEC termination/pad mismatch"
        return None, meta
    if length > MAX_PHYSICAL_PAYLOAD_BYTES:
        meta["failure"] = "invalid length"
        return None, meta
    payload = packet[2:2 + length]
    crc_at = 2 + length
    received_crc = int.from_bytes(packet[crc_at:crc_at + 4], "big")
    expected_crc = binascii.crc32(payload) & 0xFFFFFFFF
    zero_fill_ok = not any(packet[crc_at + 4:])
    crc_ok = received_crc == expected_crc
    meta.update(received_crc32=received_crc, computed_crc32=expected_crc,
                crc_ok=crc_ok, zero_fill_ok=zero_fill_ok)
    if not crc_ok:
        meta["failure"] = "CRC mismatch"
        return None, meta
    if not zero_fill_ok:
        meta["failure"] = "non-zero packet fill"
        return None, meta
    return payload, meta


def data_tones(payload: bytes) -> np.ndarray:
    bits = encode_coded_bits(payload).reshape(DATA_SYMBOLS, BITS_PER_SYMBOL)
    labels = (bits[:, 0].astype(np.int64) << 3
              | bits[:, 1].astype(np.int64) << 2
              | bits[:, 2].astype(np.int64) << 1
              | bits[:, 3].astype(np.int64))
    return UNGRAY[labels ^ HOP_MASKS]


def body_tones(payload: bytes) -> np.ndarray:
    """Insert a cycling known tone after each complete 31-data-symbol run."""

    data = data_tones(payload)
    body = []
    pilots = 0
    for at, tone in enumerate(data, 1):
        body.append(int(tone))
        if at % PILOT_INTERVAL == 0 and at != len(data):
            body.append(pilots % TONE_COUNT)
            pilots += 1
    return np.asarray(body, dtype=np.int64)


def _lead_tones(samples: int) -> np.ndarray:
    observations = samples // OBS_TX_SAMPLES
    return np.arange(observations, dtype=np.int64) % TONE_COUNT


def _modulate_observations(tones: np.ndarray, observation_samples: int,
                           sample_rate: int) -> np.ndarray:
    tones = np.asarray(tones, dtype=np.int64).reshape(-1)
    index = np.arange(observation_samples, dtype=np.float64)
    table = np.cos(2.0 * np.pi * TONE_HZ[:, None] * index[None, :]
                   / sample_rate)
    return (TX_AMPLITUDE * table[tones]).reshape(-1)


def _modulate_frame(payload: bytes, *, class_id: int = FULL_CLASS,
                    lead_samples: int = LEAD_TX_SAMPLES) -> np.ndarray:
    if not 0 <= class_id < len(CLASS_WORDS):
        raise ValueError("unknown HR1 frame class")
    if lead_samples < 0 or lead_samples % OBS_TX_SAMPLES:
        raise ValueError("lead must be a non-negative whole 8 ms observation")
    lead = _modulate_observations(
        _lead_tones(lead_samples), OBS_TX_SAMPLES, TX_SAMPLE_RATE)
    if len(lead):
        fade = min(240, len(lead))
        lead[:fade] *= np.linspace(0.0, 1.0, fade, endpoint=True)
    symbols = np.concatenate((CLASS_WORDS[class_id], body_tones(payload)))
    repeated = np.repeat(symbols, OBSERVATIONS_PER_SYMBOL)
    body = _modulate_observations(repeated, OBS_TX_SAMPLES, TX_SAMPLE_RATE)
    return np.concatenate((lead, body, np.zeros(TAIL_TX_SAMPLES))).astype(
        np.float32)


def _base_result() -> dict:
    return {
        "synced": False, "payload": None, "confidence": 0.0,
        "start_index": None, "end_index": None, "cfo_hz": 0.0,
        "clock_offset_ppm": 0.0, "candidate_limit": MAX_CANDIDATES,
        "candidates_tried": 0, "candidate_count": 0,
        "search_cells_evaluated": 0, "refinement_cells_evaluated": 0,
        "total_viterbi_branch_metrics": 0,
        "body_tone_correlations": 0,
    }


def _tone_energies(audio: np.ndarray, start: int, symbols: int,
                   cfo_hz: float, *, observations: slice = slice(1, 3)
                   ) -> np.ndarray | None:
    span = symbols * SYMBOL_RX_SAMPLES
    if start < 0 or start + span > len(audio):
        return None
    segment = np.asarray(audio[start:start + span], dtype=np.float64)
    absolute = start + np.arange(span, dtype=np.float64)
    mixed = segment * np.exp(-2j * np.pi * cfo_hz * absolute / RX_SAMPLE_RATE)
    grid = mixed.reshape(symbols, OBSERVATIONS_PER_SYMBOL, OBS_RX_SAMPLES)
    trusted = grid[:, observations, :]
    spectra = np.fft.fft(trusted, axis=-1)[..., RX_TONE_BINS]
    return np.sum(np.abs(spectra) ** 2, axis=1)


def _pattern_score(audio: np.ndarray, start: int, cfo_hz: float,
                   class_id: int) -> float:
    # The coarse detector uses only the two post-guard observations.  Fine
    # timing deliberately scores all three: without the first observation a
    # constant 24 ms dwell has a nearly 8 ms timing plateau and cannot reveal
    # the actual transition boundary.
    energy = _tone_energies(audio, start, CLASS_SYMBOLS, cfo_hz,
                            observations=slice(0, 3))
    if energy is None:
        return -1.0
    expected = CLASS_WORDS[class_id]
    hit = float(np.sum(energy[np.arange(CLASS_SYMBOLS), expected]))
    total = float(np.sum(energy))
    ratio = hit / max(total, 1e-30)
    # Noise has expectation 1/16 and a perfect word has score one.
    return (ratio - 1.0 / TONE_COUNT) / (1.0 - 1.0 / TONE_COUNT)


@dataclass(frozen=True)
class _Candidate:
    score: float
    start: int
    cfo_hz: float
    class_id: int


def _coarse_acquire(audio: np.ndarray) -> tuple[list[_Candidate], int]:
    """Bounded 2 ms / 15.625 Hz class-word search using zero-padded FFTs."""

    preamble_span = CLASS_SYMBOLS * SYMBOL_RX_SAMPLES
    maximum_start = min(MAX_ACQUISITION_START_SAMPLES,
                        len(audio) - preamble_span)
    if maximum_start < 0:
        return [], 0
    starts = np.arange(0, maximum_start + 1, COARSE_TIMING_SAMPLES,
                       dtype=np.int64)
    window_count = ((maximum_start + preamble_span - OBS_RX_SAMPLES)
                    // COARSE_TIMING_SAMPLES + 1)
    if window_count <= 0:
        return [], 0
    view = np.lib.stride_tricks.sliding_window_view(audio, OBS_RX_SAMPLES)
    windows = view[:window_count * COARSE_TIMING_SAMPLES:
                   COARSE_TIMING_SAMPLES]
    spectra = np.fft.rfft(windows, n=768, axis=1)
    power = np.abs(spectra) ** 2
    start_cells = starts // COARSE_TIMING_SAMPLES
    observation_offsets = np.stack((
        np.arange(CLASS_SYMBOLS) * 12 + 4,
        np.arange(CLASS_SYMBOLS) * 12 + 8,
    ), axis=1)
    rows: list[_Candidate] = []
    for cfo_index, cfo in zip(range(-13, 14), CFO_HYPOTHESES):
        bins = 24 + cfo_index + 8 * np.arange(TONE_COUNT)
        bank = power[:, bins]
        picked = bank[start_cells[:, None, None] + observation_offsets]
        total = np.sum(picked, axis=(1, 2, 3))
        for class_id, pattern in enumerate(CLASS_WORDS):
            expected = np.take_along_axis(
                picked, pattern[None, :, None, None], axis=3)[..., 0]
            hit = np.sum(expected, axis=(1, 2))
            ratio = hit / np.maximum(total, 1e-30)
            scores = ((ratio - 1.0 / TONE_COUNT)
                      / (1.0 - 1.0 / TONE_COUNT))
            keep = min(RAW_CANDIDATES_PER_CLASS_CFO, len(scores))
            if keep:
                best = np.argpartition(scores, -keep)[-keep:]
                rows.extend(_Candidate(float(scores[at]), int(starts[at]),
                                       float(cfo), class_id)
                            for at in best
                            if scores[at] >= ACQUISITION_THRESHOLD)
    rows.sort(key=lambda row: row.score, reverse=True)
    distinct: list[_Candidate] = []
    for row in rows:
        if any(row.class_id == old.class_id
               and abs(row.start - old.start) <= COARSE_TIMING_SAMPLES
               and abs(row.cfo_hz - old.cfo_hz) <= CFO_STEP_HZ
               for old in distinct):
            continue
        distinct.append(row)
        if len(distinct) == MAX_CANDIDATES:
            break
    search_cells = len(starts) * len(CFO_HYPOTHESES) * len(CLASS_WORDS)
    return distinct, search_cells


def _refine_candidate(audio: np.ndarray, candidate: _Candidate
                      ) -> tuple[_Candidate, int]:
    choices = []
    for start in range(max(0, candidate.start - OBS_RX_SAMPLES),
                       candidate.start + OBS_RX_SAMPLES + 1,
                       FINE_TIMING_SAMPLES):
        for delta in (-CFO_STEP_HZ / 2.0, 0.0, CFO_STEP_HZ / 2.0):
            cfo = candidate.cfo_hz + delta
            score = _pattern_score(audio, start, cfo, candidate.class_id)
            choices.append(_Candidate(score, start, cfo,
                                      candidate.class_id))
    return max(choices, key=lambda row: row.score), len(choices)


def _soft_bits_from_energy(energy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    # Column x is the energy of the tone that original four-bit label x maps
    # to after XOR hopping and Gray labeling.
    labels = np.arange(TONE_COUNT, dtype=np.int64)
    mapped = UNGRAY[labels[None, :] ^ HOP_MASKS[:, None]]
    label_energy = np.take_along_axis(energy, mapped, axis=1)
    normalized = label_energy / np.maximum(
        np.mean(label_energy, axis=1, keepdims=True), 1e-30)
    soft = np.empty((DATA_SYMBOLS, BITS_PER_SYMBOL), dtype=np.float64)
    for bit in range(BITS_PER_SYMBOL):
        set_bits = (labels >> (BITS_PER_SYMBOL - 1 - bit)) & 1
        soft[:, bit] = (np.max(normalized[:, set_bits == 0], axis=1)
                        - np.max(normalized[:, set_bits == 1], axis=1))
    winning_label = np.argmax(label_energy, axis=1)
    hard = ((winning_label[:, None]
             >> np.arange(3, -1, -1, dtype=np.int64)[None, :]) & 1)
    return soft.reshape(-1), hard.astype(np.uint8).reshape(-1)


def _split_body_energy(energy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    data_indices = []
    pilot_indices = []
    body_at = 0
    pilots = 0
    for data_at in range(1, DATA_SYMBOLS + 1):
        data_indices.append(body_at)
        body_at += 1
        if data_at % PILOT_INTERVAL == 0 and data_at != DATA_SYMBOLS:
            pilot_indices.append(body_at)
            body_at += 1
            pilots += 1
    assert body_at == BODY_SYMBOLS and pilots == PILOT_SYMBOLS
    return energy[data_indices], energy[pilot_indices]


def _decode_at(audio: np.ndarray, start: int, cfo_hz: float,
               confidence: float, *, combine_observations: int = 2) -> dict:
    result = _base_result()
    result.update(confidence=float(confidence), start_index=int(start),
                  cfo_hz=float(cfo_hz), synced=True,
                  sync_end_index=start + CLASS_SYMBOLS * SYMBOL_RX_SAMPLES)
    observations = slice(0, 3) if combine_observations == 3 else slice(1, 3)
    body_start = result["sync_end_index"]
    energy = _tone_energies(audio, body_start, BODY_SYMBOLS, cfo_hz,
                            observations=observations)
    if energy is None:
        result["failure"] = "frame truncated"
        return result
    data_energy, pilot_energy = _split_body_energy(energy)
    soft, hard = _soft_bits_from_energy(data_energy)
    payload, meta = decode_coded_soft(soft)
    power = data_energy
    best = np.max(power, axis=1)
    rest = (np.sum(power, axis=1) - best) / (TONE_COUNT - 1)
    result["tone_snr_db"] = float(10.0 * np.log10(
        np.mean(best) / max(float(np.mean(rest)), 1e-30)))
    expected_pilots = np.arange(PILOT_SYMBOLS) % TONE_COUNT
    result["pilot_correct"] = int(np.count_nonzero(
        np.argmax(pilot_energy, axis=1) == expected_pilots))
    result["pilot_symbols"] = PILOT_SYMBOLS
    result["raw_payload_bits"] = hard
    result.update(meta)
    result["payload"] = payload
    result["end_index"] = min(
        len(audio), body_start + BODY_SYMBOLS * SYMBOL_RX_SAMPLES
        + TAIL_RX_SAMPLES)
    return result


def decode_aligned(audio: np.ndarray, *, preamble_start: int,
                   cfo_hz: float = 0.0, combine_observations: int = 2) -> dict:
    """Oracle/aligned diagnosis path; no timing, CFO, or class search."""

    try:
        samples = np.asarray(audio)
        valid = samples.ndim == 1 and np.all(np.isfinite(samples))
        if valid:
            samples = samples.astype(np.float64, copy=False)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        result = _base_result()
        result["failure"] = "audio must be finite and one-dimensional"
        return result
    if combine_observations not in (2, 3):
        raise ValueError("combine_observations must be 2 or 3")
    return _decode_at(samples, preamble_start, cfo_hz, 1.0,
                      combine_observations=combine_observations)


def decode(audio: np.ndarray, **_kwargs) -> dict:
    """Bounded real receiver with frozen candidate and input-size limits."""

    result = _base_result()
    try:
        samples = np.asarray(audio)
    except (TypeError, ValueError):
        result["failure"] = "audio must be finite and one-dimensional"
        return result
    if samples.ndim != 1:
        result["failure"] = "audio must be finite and one-dimensional"
        return result
    if len(samples) > MAX_CAPTURE_SAMPLES:
        samples = samples[:MAX_CAPTURE_SAMPLES]
        result["capture_truncated_to_limit"] = True
    try:
        samples = samples.astype(np.float64, copy=False)
        finite = np.all(np.isfinite(samples))
    except (TypeError, ValueError):
        finite = False
    if not finite:
        result["failure"] = "audio must be finite and one-dimensional"
        return result
    if len(samples) < CLASS_SYMBOLS * SYMBOL_RX_SAMPLES:
        result["failure"] = "capture shorter than class word"
        return result
    if not np.any(samples):
        result["failure"] = "silence"
        return result
    candidates, search_cells = _coarse_acquire(samples)
    result["search_cells_evaluated"] = search_cells
    result["candidate_count"] = len(candidates)
    if not candidates:
        result["failure"] = "class word not found"
        return result
    result["confidence"] = float(candidates[0].score)
    total_viterbi_work = 0
    total_tone_correlations = 0
    for rank, coarse in enumerate(candidates, 1):
        if coarse.class_id != FULL_CLASS:
            continue
        refined, refinement_cells = _refine_candidate(samples, coarse)
        result["candidates_tried"] += 1
        result["refinement_cells_evaluated"] += refinement_cells
        decoded = _decode_at(samples, refined.start, refined.cfo_hz,
                             refined.score)
        total_viterbi_work += int(decoded.get("viterbi_branch_metrics", 0))
        if decoded.get("viterbi_steps"):
            total_tone_correlations += BODY_SYMBOLS * 2 * TONE_COUNT
        decoded.update(candidate_limit=MAX_CANDIDATES,
                       candidate_count=len(candidates),
                       candidates_tried=result["candidates_tried"],
                       refinement_cells_evaluated=(
                           result["refinement_cells_evaluated"]),
                       candidate_rank=rank,
                       search_cells_evaluated=search_cells,
                       total_viterbi_branch_metrics=total_viterbi_work,
                       body_tone_correlations=total_tone_correlations,
                       acquired_class=refined.class_id)
        if decoded.get("payload") is not None:
            decoded["head_seconds_received"] = max(
                0.0, refined.start / RX_SAMPLE_RATE)
            return decoded
        # Keep the most informative checked-body failure.
        result.update(decoded)
    result["payload"] = None
    result["confidence"] = float(candidates[0].score)
    result.setdefault("failure", "no full-class candidate passed integrity")
    return result


def encode(payload: bytes, *, include_head: bool = True,
           head_seconds: float = LEAD_SECONDS) -> np.ndarray:
    if head_seconds < 0:
        raise ValueError("head duration must not be negative")
    lead_samples = 0 if not include_head else max(
        LEAD_TX_SAMPLES,
        int(np.ceil(head_seconds / OBSERVATION_SECONDS)) * OBS_TX_SAMPLES)
    return _modulate_frame(bytes(payload), lead_samples=lead_samples)


@dataclass(frozen=True)
class Hr1Mode:
    name: str = "hr1-a-exp"
    mode_id: int = EXPERIMENTAL_MODE_ID
    chunk_size: int = CHUNK_SIZE
    confidence_threshold: float = ACQUISITION_THRESHOLD
    tx_sample_rate: int = TX_SAMPLE_RATE
    rx_sample_rate: int = RX_SAMPLE_RATE

    @property
    def baud(self) -> float:
        return 1.0 / SYMBOL_SECONDS

    @property
    def head_match_allowance_seconds(self) -> float:
        return OBSERVATION_SECONDS

    def encode(self, payload: bytes, *, include_head: bool = True,
               head_seconds: float = LEAD_SECONDS) -> np.ndarray:
        return encode(payload, include_head=include_head,
                      head_seconds=head_seconds)

    def decode(self, audio: np.ndarray, **kwargs) -> dict:
        return decode(audio, **kwargs)

    def airtime(self, payload_len: int) -> float:
        if payload_len > MAX_PHYSICAL_PAYLOAD_BYTES:
            raise ValueError("payload exceeds the full HR1-A class")
        return FRAME_SECONDS


HR1 = Hr1Mode()


def _check_constants() -> None:
    assert isinstance(HR1, waveform.WaveformMode)
    assert OBS_TX_SAMPLES / TX_SAMPLE_RATE == OBSERVATION_SECONDS
    assert OBS_RX_SAMPLES / RX_SAMPLE_RATE == OBSERVATION_SECONDS
    assert FEC_INPUT_BITS == 568 and CODED_BITS == 1704
    assert DATA_SYMBOLS == 426 and PILOT_SYMBOLS == 13 and BODY_SYMBOLS == 439
    assert FRAME_TX_SAMPLES == 604_992 and FRAME_SECONDS == 12.604
    assert INTERLEAVER.is_valid()
    assert all(np.array_equal(np.sort(word.reshape(5, 16), axis=1),
                              np.tile(np.arange(16), (5, 1)))
               for word in CLASS_WORDS)
    assert len(np.unique(CLASS_WORDS, axis=0)) == 3


_check_constants()
