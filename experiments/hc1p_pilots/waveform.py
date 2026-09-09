"""Parameterised HC1W-geometry waveform with in-frame pilot re-anchoring.

Built to answer one question: does HC1W's Watterson envelope come from its
5 s frame length, or from what it does *inside* the frame?

HC1W fits its per-carrier channel once from the 13-symbol header
(`whale/modes/hc1w.py`, `_eq.fit_header`) and then applies that single fit to
all 352 payload symbols -- 4.8 s.  Two things ride on that fit and go stale
together:

  * the per-carrier *decision reference*, which differential detection in time
    already makes irrelevant: a drifting channel phase cancels in
    ``v[i] * conj(v[i-1])``, leaving only the phase change across one 13.3 ms
    symbol.  Slow fading does not move a differential decision.

  * the per-carrier *reliability weight*, which nothing rescues.
    ``_eq.carrier_weights(channel.snr_db)`` is computed from the header and
    applied to the whole frame, so a carrier that is clean at the header and
    sits in a null three seconds later keeps its confident weight and feeds
    the Viterbi decoder assured noise.  Under `mid_latitude_moderate` -- 0.5 Hz
    spread, coherence time of order 2 s, 1 ms delay spread against a 2 kHz
    band -- that is exactly what the channel does.

So the hypothesis this module exists to test is that the stale *weight*, not
the stale reference and not the frame length, is what costs HC1W its fading
margin.  The sweep confirmed it: see `RESULTS.md`.  Holding the anchors but
freezing the weights recovers only about a quarter of the gain, and the
coherent arms -- which the stale *reference* would have favoured -- lose under
fading at every stride, because differential detection already absorbs the
median 12 radians of drift the pilots measure across one frame.

Geometry, acquisition, frequency and timing are HC1W's own -- imported from
the shipped module, not copied -- so every arm shares them bit for bit and any
measured difference is in the payload path alone.  `TOTAL_SYMBOLS` stays 365
and the frame stays 5.015 s in every arm, so frame length is held fixed rather
than confounded with the thing being measured.

Pilots go into payload slots rather than being appended, as VF6 does
(`whale/modes/vf6.py`).  Capacity is the only thing that moves.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import cached_property
import re
from math import gcd
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np
from scipy.signal import hilbert

from whale import dsp, framing
from whale.dsp import (differential as _diff, equalize as _eq, freq as _freq,
                       ofdm as _ofdm, timing as _timing)
from whale.modes import hc1w

# -- HC1W's geometry, unchanged and shared by every arm -------------------

SAMPLE_RATE = hc1w.SAMPLE_RATE
RX_SAMPLE_RATE = hc1w.RX_SAMPLE_RATE
N_CARRIERS = hc1w.N_CARRIERS
BITS_PER_SYMBOL = hc1w.BITS_PER_SYMBOL
SYNC_SYMBOLS = hc1w.SYNC_SYMBOLS
HEADER_SYMBOLS = hc1w.HEADER_SYMBOLS
PAYLOAD_SYMBOLS = hc1w.PAYLOAD_SYMBOLS
TOTAL_SYMBOLS = hc1w.TOTAL_SYMBOLS
SYMBOL_SAMPLES = hc1w.SYMBOL_SAMPLES
RX_SYMBOL_SAMPLES = hc1w.RX_SYMBOL_SAMPLES
TAIL_SAMPLES = hc1w.TAIL_SAMPLES
HEADER_VALUES = hc1w.HEADER_VALUES
ACQUISITION_THRESHOLD = hc1w.ACQUISITION_THRESHOLD

#: The coherent arm's constellation, in `dsp.bits.qpsk_from_bits` order, so
#: `_diff.soft_bits` can serve both arms by taking the points/labels pair it
#: already accepts rather than growing a second LLR implementation.
COHERENT_LABELS = np.array([[0, 0], [0, 1], [1, 0], [1, 1]], dtype=np.uint8)
COHERENT_POINTS = dsp.bits.qpsk_from_bits(COHERENT_LABELS.reshape(-1))

#: Weight clip, matching `_eq.carrier_weights`: discount a faded carrier
#: without letting a strong one dominate the codeword.
WEIGHT_LOW = 0.5
WEIGHT_HIGH = 2.0


def _coprime_stride(size: int, base: int = 811) -> int:
    """The first stride at or above `base` that is a permutation of `size`.

    HC1W uses 811 against its 16,192-bit grid.  Each arm has its own payload
    size, so each needs its own coprime; deriving it rather than hand-picking
    one keeps the arms comparable and the choice reproducible.
    """
    stride = base
    while gcd(stride, size) != 1:
        stride += 1
    return stride


@dataclass(frozen=True)
class Variant:
    """One arm of the sweep.

    `stride` of 0 means no pilots, which reproduces HC1W's payload layout
    exactly and is the control.  `detection` selects differential-in-time
    (HC1W's own) or coherent-against-the-tracked-channel.
    """

    stride: int
    detection: str = "differential"
    track_weights: bool = True
    anchor_pilots: bool = True
    #: Override the derived interleaver stride.  HC1W ships 811 against its
    #: 16,192-bit grid, which spreads one OFDM symbol's 46 bits only 20
    #: positions apart in the code; this exists to test better ones.
    interleaver_stride: int = 0

    def __post_init__(self) -> None:
        if self.stride < 0:
            raise ValueError("pilot stride must not be negative")
        if self.stride and self.stride > PAYLOAD_SYMBOLS:
            raise ValueError("pilot stride exceeds the payload")
        if self.detection not in ("differential", "coherent"):
            raise ValueError(f"unknown detection {self.detection!r}")
        if self.detection == "coherent" and not self.stride:
            raise ValueError("coherent detection needs pilots to track")
        if not self.anchor_pilots and self.detection == "coherent":
            raise ValueError("coherent detection cannot use blind pilots")

    @property
    def name(self) -> str:
        if not self.stride:
            return ("baseline" if not self.interleaver_stride
                    else f"baseline-i{self.interleaver_stride}")
        suffix = "" if self.track_weights else "-flatweight"
        if not self.anchor_pilots:
            suffix += "-blind"
        if self.interleaver_stride:
            suffix += f"-i{self.interleaver_stride}"
        return f"{self.detection[:4]}-s{self.stride}{suffix}"

    # -- payload layout ---------------------------------------------------

    @cached_property
    def pilot_positions(self) -> np.ndarray:
        """Pilot slots, centred so the gaps at both frame ends are half a stride.

        The first anchor is the last header symbol at payload position -1, so
        starting at `stride // 2` leaves that opening gap the same size as
        every interior one instead of doubling it.
        """
        if not self.stride:
            return np.zeros(0, dtype=np.int64)
        return np.arange(self.stride // 2, PAYLOAD_SYMBOLS, self.stride,
                         dtype=np.int64)

    @cached_property
    def data_positions(self) -> np.ndarray:
        return np.setdiff1d(np.arange(PAYLOAD_SYMBOLS, dtype=np.int64),
                            self.pilot_positions)

    @cached_property
    def pilot_values(self) -> np.ndarray:
        """Known QPSK pilot symbols, at the payload's own symbol energy."""
        count = len(self.pilot_positions)
        if not count:
            return np.zeros((0, N_CARRIERS), dtype=np.complex128)
        bits = dsp.bits.pn_bits(count * BITS_PER_SYMBOL, 0x0B3E9)
        return dsp.bits.qpsk_from_bits(bits).reshape(count, N_CARRIERS)

    @cached_property
    def payload_bits(self) -> int:
        return len(self.data_positions) * BITS_PER_SYMBOL

    @cached_property
    def codec(self) -> dsp.PacketCodec:
        return dsp.PacketCodec(
            payload_bits=self.payload_bits,
            interleaver=dsp.interleave.multiplicative(
                self.payload_bits,
                self.interleaver_stride or _coprime_stride(self.payload_bits)),
            whitener_seed=0x1A5C7,
            code=dsp.K9,
        )

    @property
    def max_payload_bytes(self) -> int:
        return self.codec.max_payload_bytes

    @property
    def chunk_size(self) -> int:
        return self.max_payload_bytes - framing.AIR_HEADER_BYTES

    @property
    def capacity_loss(self) -> float:
        """Fraction of HC1W's payload capacity spent on pilots."""
        return 1.0 - self.max_payload_bytes / hc1w.MAX_PAYLOAD_BYTES

    # -- modulation -------------------------------------------------------

    def payload_grid(self, payload: bytes) -> np.ndarray:
        """The 352-symbol payload constellation, pilots written into place."""
        coded = self.codec.encode(bytes(payload))
        symbols = dsp.bits.qpsk_from_bits(coded).reshape(-1, N_CARRIERS)
        grid = np.empty((PAYLOAD_SYMBOLS, N_CARRIERS), dtype=np.complex128)
        if len(self.pilot_positions):
            grid[self.pilot_positions] = self.pilot_values
        if self.detection == "coherent":
            grid[self.data_positions] = symbols
            return grid
        # Differential: accumulate phase increments along the whole payload,
        # with each pilot re-anchoring the chain absolutely.  A data symbol
        # that follows a pilot references that pilot, so the recovered
        # increment is correct at every data position regardless of what
        # preceded it -- and a symbol error can no longer propagate past the
        # next pilot.
        increments = _diff.POINTS[
            (coded.reshape(-1, N_CARRIERS, 2).astype(np.int64)
             @ np.array([2, 1], dtype=np.int64))]
        blind = _diff.POINTS[dsp.bits.pn_bits(
            len(self.pilot_positions) * N_CARRIERS * 2, 0x1C4D3).reshape(
                -1, N_CARRIERS, 2).astype(np.int64)
            @ np.array([2, 1], dtype=np.int64)]
        previous = HEADER_VALUES[-1]
        data_at = 0
        pilot_at = 0
        pilots = set(int(p) for p in self.pilot_positions)
        for index in range(PAYLOAD_SYMBOLS):
            if index in pilots:
                if self.anchor_pilots:
                    previous = grid[index]
                else:
                    # The slot is spent either way; only the *known absolute
                    # reference* is withheld, so this separates the capacity
                    # the pilots cost from the anchoring they provide.
                    previous = previous * blind[pilot_at]
                    grid[index] = previous
                pilot_at += 1
                continue
            previous = previous * increments[data_at]
            grid[index] = previous
            data_at += 1
        return grid

    def modulate(self, payload: bytes, *,
                 head_seconds: float = hc1w.DEFAULT_HEAD_SECONDS
                 ) -> np.ndarray:
        values = np.vstack((HEADER_VALUES, self.payload_grid(payload)))
        symbols = np.concatenate([hc1w.build_symbol(row) for row in values])
        lead = np.resize(hc1w.sync_core(),
                         hc1w.lead_in_samples(head_seconds)).copy()
        fade = hc1w.LEAD_IN_FADE_SAMPLES
        lead[:fade] *= np.linspace(0.0, 1.0, fade, endpoint=True)
        audio = np.concatenate((lead, symbols, np.zeros(TAIL_SAMPLES)))
        peak = float(np.max(np.abs(audio)))
        if peak > hc1w.MAX_SAMPLE:
            audio *= hc1w.MAX_SAMPLE / peak
        return audio.astype(np.float32)

    # -- demodulation -----------------------------------------------------

    def _anchors(self, payload_grid: np.ndarray, header_last: np.ndarray
                 ) -> tuple[np.ndarray, np.ndarray]:
        """Observed-over-expected ratio at every known symbol in the frame.

        Position -1 is the last header symbol; the rest are the pilots.  This
        is the whole picture of what the channel did after the header fit was
        taken, which is what both the phase track and the weight track read.
        """
        positions = np.concatenate((np.array([-1], dtype=np.int64),
                                    self.pilot_positions))
        ratios = np.vstack((
            (header_last / HEADER_VALUES[-1])[None, :],
            payload_grid[self.pilot_positions] / self.pilot_values,
        ))
        return positions, ratios

    def _weight_grid(self, payload_grid: np.ndarray, header_last: np.ndarray,
                     header_snr_db: np.ndarray) -> np.ndarray:
        """Per-symbol, per-carrier soft-bit weights.

        HC1W derives one weight per carrier from the header and holds it for
        the whole frame.  This interpolates each carrier's *power* between the
        known symbols instead, so a carrier that fades away mid-frame loses
        its say in the codeword at the point it actually fades rather than
        keeping the header's confident opinion to the end.

        Normalized per symbol against the median carrier, matching
        `_eq.carrier_weights`: what matters is which carriers to believe
        relative to their neighbours, not the absolute level.
        """
        header_weights = _eq.carrier_weights(header_snr_db, WEIGHT_LOW,
                                             WEIGHT_HIGH)
        if not self.stride or not self.track_weights or not self.anchor_pilots:
            return np.tile(header_weights, (len(self.data_positions), 1))
        positions, ratios = self._anchors(payload_grid, header_last)
        power = np.abs(ratios) ** 2
        tracked = np.empty((PAYLOAD_SYMBOLS, N_CARRIERS))
        grid_positions = np.arange(PAYLOAD_SYMBOLS)
        for carrier in range(N_CARRIERS):
            tracked[:, carrier] = np.interp(grid_positions, positions,
                                            power[:, carrier])
        median = np.maximum(np.median(tracked, axis=1, keepdims=True), 1e-30)
        weights = np.clip(tracked / median, WEIGHT_LOW, WEIGHT_HIGH)
        # The header fit still knows about a carrier that was notched from
        # the start, which no in-frame ratio can reveal; keep both opinions.
        return (weights * header_weights[None, :])[self.data_positions]

    def _track_phase(self, payload_grid: np.ndarray, header_last: np.ndarray
                     ) -> tuple[np.ndarray, np.ndarray]:
        """Remove the channel phase the pilots saw drift after the header."""
        if not self.stride or not self.anchor_pilots:
            return payload_grid, np.zeros_like(payload_grid, dtype=np.float64)
        return _eq.pilot_phase(payload_grid, self.pilot_positions,
                               self.pilot_values, header_last,
                               HEADER_VALUES[-1])

    def demodulate(self, audio: np.ndarray, *,
                   head_seconds: float = hc1w.DEFAULT_HEAD_SECONDS) -> dict:
        """Decode one frame.  Acquisition and synchronisation are HC1W's."""
        del head_seconds
        result = {
            "synced": False, "payload": None, "confidence": 0.0,
            "start_index": None, "cfo_hz": 0.0, "raw_payload_bits": None,
            "carrier_snr_db": np.full(N_CARRIERS, -np.inf),
        }
        samples = np.asarray(audio, dtype=np.float64).reshape(-1)
        if len(samples) < HEADER_SYMBOLS * RX_SYMBOL_SAMPLES:
            result["failure"] = "capture shorter than header"
            return result

        analytic = hilbert(samples)
        start, confidence = hc1w._acquire(analytic)
        result.update(confidence=confidence, start_index=start)
        if start is None or confidence < ACQUISITION_THRESHOLD:
            result["failure"] = "header not found"
            return result
        result["sync_end_index"] = start + HEADER_SYMBOLS * RX_SYMBOL_SAMPLES

        coarse_hz = hc1w._coarse_offset(analytic, start)
        corrected = _freq.derotate(analytic, coarse_hz, RX_SAMPLE_RATE)
        fit = _timing.estimate(hc1w.RX_GEOMETRY, corrected, start,
                               hc1w._TIMING_SYMBOLS)
        carriers = _ofdm.carrier_bank(hc1w.RX_GEOMETRY, corrected, start,
                                      TOTAL_SYMBOLS, fit.intercept, fit.slope,
                                      hc1w.RX_FFT_OFFSET)
        if carriers is None:
            result["failure"] = "frame truncated"
            return result
        result["end_index"] = min(
            len(samples),
            start + TOTAL_SYMBOLS * RX_SYMBOL_SAMPLES + hc1w.RX_TAIL_SAMPLES)

        shifts = np.array([fit.shift_at(i) for i in range(TOTAL_SYMBOLS)])
        fine_hz = _freq.fine_offset_hz(hc1w.RX_GEOMETRY,
                                       carriers[:HEADER_SYMBOLS], HEADER_VALUES)
        carriers = hc1w._remove_residual_offset(carriers, fine_hz, start, shifts)
        result["cfo_hz"] = coarse_hz + fine_hz

        channel = _eq.fit_header(carriers[:HEADER_SYMBOLS], HEADER_VALUES)
        present = channel.present_carriers(hc1w.CARRIER_FLOOR_DB)
        result["present_carriers"] = present
        if present < hc1w.MIN_PRESENT_CARRIERS:
            result["failure"] = f"header has only {present}/{N_CARRIERS} carriers"
            return result
        result["carrier_snr_db"] = channel.snr_db

        equalised = channel.equalize(carriers)
        header_last = equalised[HEADER_SYMBOLS - 1]
        payload_grid = equalised[HEADER_SYMBOLS:]
        weights = self._weight_grid(payload_grid, header_last, channel.snr_db)

        if self.detection == "coherent":
            tracked, phase = self._track_phase(payload_grid, header_last)
            values = tracked[self.data_positions]
            points, labels = COHERENT_POINTS, COHERENT_LABELS
        else:
            # Differential cancels the channel phase in the difference, so
            # the phase track is measured for the record but not applied.
            _, phase = self._track_phase(payload_grid, header_last)
            observed = _diff.observations(payload_grid, header_last)
            values = observed[self.data_positions]
            points, labels = _diff.POINTS, _diff.LABELS

        result["phase_drift_rad"] = float(
            np.max(np.abs(phase)) if phase.size else 0.0)
        result["raw_payload_bits"] = _diff.hard_bits(values, points, labels)
        soft = _diff.soft_bits(values, np.ones(N_CARRIERS), points, labels)
        soft = (soft.reshape(len(self.data_positions), N_CARRIERS, 2)
                * weights[:, :, None]).reshape(-1)
        payload, meta = self.codec.decode_soft(soft)
        result.update(meta)
        result.update(payload=payload, synced=True, soft_payload_bits=soft)
        return result

    def describe(self) -> str:
        return (f"{self.name}: {len(self.pilot_positions)} pilots, "
                f"{len(self.data_positions)} data symbols, "
                f"{self.max_payload_bytes} B "
                f"({self.capacity_loss * 100:.1f}% of HC1W's capacity), "
                f"{self.detection} detection")


class VariantMode:
    """`Variant` in the shape `whale.qualification.run_frame_trial` expects.

    The trial runner only needs encode/decode plus the identity and rate
    attributes, so the sweep reuses the shipped Monte Carlo path rather than
    growing its own.  The lead is HC1W's, prepended exactly as
    `whale/modes/hc1w_mode.py` does it, so acquisition sees what it sees on
    the air.
    """

    tx_sample_rate = SAMPLE_RATE
    rx_sample_rate = RX_SAMPLE_RATE
    confidence_threshold = ACQUISITION_THRESHOLD

    def __init__(self, variant: Variant, mode_id: int = 18) -> None:
        from whale.modes import hf_lead
        self.variant = variant
        self.mode_id = mode_id
        self.name = variant.name
        self.chunk_size = variant.chunk_size
        self._hf_lead = hf_lead

    @property
    def baud(self) -> float:
        return SAMPLE_RATE / SYMBOL_SAMPLES

    def encode(self, payload: bytes, *, include_head=True,
               head_seconds=hc1w.DEFAULT_HEAD_SECONDS) -> np.ndarray:
        if not include_head:
            head_seconds = hc1w.DEFAULT_HEAD_SECONDS
        body = self.variant.modulate(payload)[hc1w.lead_in_samples():]
        lead = self._hf_lead.modulate(self._hf_lead.HC1W_LABEL, head_seconds)
        return np.concatenate((lead, body))

    def decode(self, audio, **kwargs) -> dict:
        return self.variant.demodulate(audio, **kwargs)

    def airtime(self, payload_len: int) -> float:
        del payload_len
        return ((self._hf_lead.MIN_SAMPLES + TOTAL_SYMBOLS * SYMBOL_SAMPLES
                 + TAIL_SAMPLES) / SAMPLE_RATE)


#: The sweep's arms.  Strides bracket the coherence time from both sides:
#: 24 symbols is 0.32 s, well inside `mid_latitude_moderate`'s ~2 s and
#: `mid_latitude_disturbed`'s ~1 s, so 16 and 48 should show where anchors
#: stop being dense enough and where the capacity they cost stops paying.
STRIDES = (16, 24, 32, 48)
ARMS = (
    (Variant(stride=0),)
    + tuple(Variant(stride=s, detection="differential") for s in STRIDES)
    + tuple(Variant(stride=s, detection="coherent") for s in STRIDES)
)


#: The ablation.  Two mechanisms ride on the pilots at once -- they re-anchor
#: the differential chain (so a symbol error cannot propagate past the next
#: pilot) and they drive time-varying soft-bit weights.  Naming an arm
#: `-flatweight` keeps the first and drops the second, holding HC1W's
#: header-derived weights for the whole frame, which is the only way to say
#: which of the two the measured gain came from.
_NAME_PATTERN = re.compile(
    r"^(diff|cohe)-s(\d+)(-flatweight)?(-blind)?(?:-i(\d+))?$")
_BASELINE_PATTERN = re.compile(r"^baseline-i(\d+)$")


def arm_by_name(name: str) -> Variant:
    for variant in ARMS:
        if variant.name == name:
            return variant
    match = _BASELINE_PATTERN.match(name)
    if match:
        return Variant(stride=0, interleaver_stride=int(match.group(1)))
    match = _NAME_PATTERN.match(name)
    if match:
        kind, stride, flat, blind, inter = match.groups()
        return Variant(
            stride=int(stride),
            detection="differential" if kind == "diff" else "coherent",
            track_weights=flat is None,
            anchor_pilots=blind is None,
            interleaver_stride=int(inter) if inter else 0)
    raise KeyError(f"no arm named {name!r}; have "
                   f"{', '.join(v.name for v in ARMS)} (or any of those with "
                   f"a -flatweight suffix)")


if __name__ == "__main__":
    print(f"HC1W control: {hc1w.MAX_PAYLOAD_BYTES} B, "
          f"{hc1w.FRAME_SECONDS:.3f} s, {TOTAL_SYMBOLS} symbols")
    for variant in ARMS:
        print(" ", variant.describe())
