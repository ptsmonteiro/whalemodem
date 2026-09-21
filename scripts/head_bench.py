"""Settling-head bench: how short can an HF mode's head be, and what breaks.

`whale/framing.py`'s `SETTLING_HEAD_SECONDS` is one number covering two
unrelated quantities, and this script exists because they must be measured
apart:

    (A) analogue readiness -- how long after key-up the receive chain
        delivers usable signal. PTT ramp plus receiver AGC. It is a property
        of the two radios and the keying history, it is measured from the
        capture's level envelope, and **it needs no decode at all**. It
        runs 30-60 ms worst case on the bench radios and near zero in the
        fast direction; that spread is the whole reason direction is an
        axis here.

    (B) acquisition appetite -- how much signal the sync search needs in
        front of the preamble. It is a property of the DSP, not the radios,
        and `scripts/head_analyse.py --trim` measures it offline from one
        capture.

This script measures (A) on the air and records everything a later (B) sweep
needs. It never reports a single "head budget": a head short enough for the
DSP but shorter than the AGC ramp still fails, and a run that conflated the
two would blame the wrong subsystem.

WHY ONE PROCESS OWNS BOTH RADIOS
    The driver knows exactly when it keyed, which mode and head length it
    sent, and which sample of its own TX buffer the sync preamble starts at.
    Every diagnostic below is relative to that ground truth. Two cooperating
    stations could agree on none of it.

WHICH RECEIVER DECODES THE CAPTURE
    The shipped one. Each keying's capture is replayed, a poll's worth of
    audio at a time, into `whale.link_receiver._ReceiverMixin` through
    `whale/streaming.py`'s `ReceiveStream` -- the same search, the same 0.12
    candidate gate, the same first-adequate-frame-wins rule the operator's
    modem runs. See `BenchReceiver`, which follows `whale/sweep.py`'s
    precedent of mixing that receiver in and replacing only the two methods
    that are about ARQ.

    This is not a detail. A whole-capture decode takes the best-scoring
    position in a keying; the shipped receiver takes the first adequate one.
    On a multi-frame burst those are different frames -- the leading frame
    is the one the AGC ramp attenuates, so it never wins a global argmax --
    and `burst` is the scenario that answers whether the head is needed once
    per keying or once per frame. Every record carries `receive_path` so a
    log written before this changed cannot be silently compared with one
    written after.

WHAT A RECORD MEANS
    outcome         decoded      payload matched byte for byte
                    synced       acquisition fired, payload wrong or absent
                    never-seen   acquisition never fired
                    Pass/fail alone is nearly useless; these three are three
                    different bugs.
    landing         where acquisition landed relative to the true preamble.
                    `in-head` means the head out-scored the preamble (see
                    `whale/streaming.py`'s 0.12 candidate gate against the
                    0.10-0.13 in-head score the OFDM head measures -- this
                    is a live risk, so it is logged every keying, passing or
                    not). `before-onset` means it landed even earlier --
                    before RF was even detected on, so there is no head to
                    blame; a zero-length-head keying can only ever produce
                    this one, never `in-head`. `late` means the preamble was
                    missed and something later matched.
    settle_seconds  QUANTITY (A), measured directly from the envelope: time
                    from RF onset until the level stays within
                    SETTLE_TOLERANCE_DB of its steady state. This is the
                    number the head has to cover. It is null, with
                    `settle_unmeasured_reason` saying why, whenever the
                    capture cannot support it -- a keying too short or too
                    noisy to measure a ramp in reports nothing rather than
                    reporting the keying's own length as its settle time.
    raw_ber         bit errors before the FEC, against the payload we know
                    we sent. A frame failing at 1% raw BER is not a head
                    problem -- look at cfo_hz and carrier_snr_db instead.
    rx_overflows    a capture overflow calls `_clear_buffer()` and bumps the
                    generation, destroying any in-flight acquisition. A
                    keying whose `rx_overflows` moved is a USB dropout, not
                    a head measurement. Without this you will spend a day
                    blaming the settling head for a driver hiccup.

SAFETY
    The receiving station is opened `receive_only=True`, so no object in the
    process can key it. `turnaround` is the one scenario where both ends must
    key, and it therefore refuses to run without `--allow-two-way`. Ctrl-C
    stops the run with the radio down and the JSONL written for the part that
    ran.

RUN
    python scripts/head_bench.py --simulate --out logs/head/sim.jsonl
    python scripts/head_bench.py --direction ab --scenario single cold \\
        --mode hc0 hf6 --out logs/head/run1.jsonl
    python scripts/head_bench.py --direction ab --out logs/head/run1.jsonl
        (resumes: cells already in the JSONL are skipped)

Then `python scripts/head_analyse.py --aggregate logs/head/run1.jsonl`.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import queue
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

import bench
from whale import link_receiver, mode_qualification, rx_audio, sweep
from whale import policy as whale_policy
from whale.framing import AIR_HEADER_BYTES
from whale.streaming import ReceiveStream

# `whale.waveform.with_settling_head` is the single entry point for resizing
# a head. It is under active development next door; guard the import so this
# module still loads (and its pure-logic half still unit-tests) if the
# signature moves out from under us. A run that actually needs it fails loud.
try:
    from whale.waveform import with_settling_head as _with_settling_head
except ImportError:  # pragma: no cover - only while the API is in flux
    _with_settling_head = None

RX_RATE = rx_audio.DECODE_SAMPLE_RATE
TX_RATE = rx_audio.CAPTURE_SAMPLE_RATE
DECIMATION = rx_audio.DECIMATION

#: Envelope hop, in seconds. 10 ms resolves a 208 ms ramp into ~20 points,
#: which is enough to see its shape, and is long enough at 12 kHz (120
#: samples) that the RMS in one hop is a level rather than a waveform.
ENVELOPE_HOP_SECONDS = 0.010

#: "Settled" means within this much of the steady-state level. 1 dB is
#: tighter than the AGC's own residual ripple would justify reporting to
#: two decimals, and loose enough that a settled carrier does not keep
#: re-triggering. Reported alongside the number so a reader can requote it.
SETTLE_TOLERANCE_DB = 1.0

#: RF onset is where the envelope first gets this far above the pre-key
#: noise floor *and stays there* (see ONSET_RUN_HOPS). The head fades in
#: over 240 TX samples (5 ms) and the AGC then moves tens of dB, so a 6 dB
#: trigger sits on the steep part of the rise and is not sensitive to where
#: exactly it is placed.
ONSET_RISE_DB = 6.0

#: How far either side of the true preamble an acquisition still counts as
#: having landed *on* it. Two envelope hops' worth: the truth reference is
#: the detected RF onset plus the known head length, so the onset detector's
#: own quantisation is the dominant error and this must exceed it.
LANDING_TOLERANCE_SAMPLES = int(round(2 * ENVELOPE_HOP_SECONDS * RX_RATE))

#: Capture poll period. `RadioTransport.RX_BUFFER_SECONDS` is 10 s and the
#: buffer drops its oldest chunk past that, so polling must stay well inside
#: it or the run loses audio silently. 1 s leaves an order of magnitude.
CAPTURE_POLL_SECONDS = 1.0

#: Capture kept before key-up and after the frame ends. The pre-roll is what
#: the noise floor (and therefore the onset trigger) is measured from, so it
#: must be longer than a few envelope hops; the post-roll covers the PTT tail
#: and `transport.STREAM_FILL`'s drain.
PRE_ROLL_SECONDS = 0.5
POST_ROLL_SECONDS = 1.0

#: Fraction of *passing* keyings whose capture is kept, as controls. Every
#: failing keying's capture is kept unconditionally -- those are the ones the
#: diagnostic tool exists for -- but a matrix of passes with no passing
#: capture to compare against is not diagnosable either.
KEEP_PASS_FRACTION = 0.1

# -- the plan, as data ----------------------------------------------------

#: Scenario definitions. Five bespoke scripts would drift apart; a scenario
#: here is a name plus the axes that are meaningful for it, and the driver
#: reads it. `gaps` is the idle time before the keying -- the AGC's memory of
#: what came before is exactly what scenario means.
#: TEMPORARY, for the 2026-09-21 head-length campaign (see logs/head/):
#: gaps trimmed to one representative value per scenario so a head/mode/rep
#: matrix fits an air-time budget -- gap is not an axis this campaign is
#: sweeping. Revert to the historical tuples afterwards.
PLAN = {
    # One frame per keying. The baseline, and the only cell the shipped
    # head length was ever reasoned against.
    "single": {"gaps": (1.0,), "frames": 1},
    # N frames inside ONE keying. If the head is per-keying rather than
    # per-frame, a burst amortises it away entirely and the right answer for
    # bulk transfer is "key once", not "shorten the head".
    "burst": {"gaps": (1.0,), "frames": 4},
    # A keys, B answers within `turnaround`. The ARQ-realistic case: a small
    # PTT-off gap, both AGCs still slammed by the frame that just ended.
    "turnaround": {"gaps": (1.0,), "frames": 1, "turnaround": (0.2,)},
    # Mode A then mode B, keying each. Different spectra mean a different
    # level and a receiver mode-search that has to re-find the waveform.
    "alternate": {"gaps": (0.5,), "frames": 1, "alternate": True},
    # Long silence then key: worst-case AGC slam. This sets the ceiling; no
    # head shorter than what `cold` needs is safe for a first keying.
    "cold": {"gaps": (30.0,), "frames": 1},
}

#: Head lengths swept. 0.0 is in deliberately: it is the zero point that says
#: what the head buys at all, and without it a "0.2 s works" result has
#: nothing to be better than. 0.6 is the previous shipped value, kept as
#: the control the current 0.2 was chosen against.
DEFAULT_HEADS = (0.0, 0.1, 0.2, 0.3, 0.45, 0.6, 0.9)

#: Modes swept by default: the robust FSK rung, the differential-QPSK OFDM
#: rung, and one 49-carrier OFDM mode -- one member of each head-bearing
#: family, since the head is a family property (see `whale/phy/`).
DEFAULT_MODES = ("hc0", "hc1w", "hf6")

DEFAULT_REPS = 3

DIRECTIONS = ("ab", "ba")


@dataclass(frozen=True)
class Cell:
    """One keying the matrix asks for, and the key it resumes under."""

    scenario: str
    direction: str
    mode: str
    head_seconds: float
    gap_seconds: float
    rep: int
    frames: int = 1
    turnaround_seconds: float | None = None
    alt_mode: str | None = None

    @property
    def key(self) -> str:
        """Stable identity of this cell across runs.

        Everything that changes what goes on the air is in it, and nothing
        else is -- a resumed run must recognise a cell it already recorded
        even though the `run_id` and the timestamps differ.
        """
        return (f"{self.scenario}|{self.direction}|{self.mode}|"
                f"{self.head_seconds:g}|{self.gap_seconds:g}|"
                f"{self.frames}|{self.turnaround_seconds or 0:g}|"
                f"{self.alt_mode or '-'}|{self.rep}")


def expand(plan=None, scenarios=None, modes=None, directions=None,
           heads=None, reps=DEFAULT_REPS) -> list[Cell]:
    """The full matrix, as a list of cells in the order they should be run.

    Ordered scenario-major and head-minor on purpose: a run that is
    interrupted (and they all are) should have finished whole scenarios
    rather than a thin slice of every one of them.
    """
    plan = PLAN if plan is None else plan
    scenarios = tuple(plan) if scenarios is None else tuple(scenarios)
    modes = DEFAULT_MODES if modes is None else tuple(modes)
    directions = DIRECTIONS if directions is None else tuple(directions)
    heads = DEFAULT_HEADS if heads is None else tuple(heads)

    cells = []
    for scenario in scenarios:
        spec = plan[scenario]
        for direction, mode, head, gap, rep in itertools.product(
                directions, modes, heads, spec["gaps"], range(1, reps + 1)):
            alt = None
            if spec.get("alternate"):
                # The other mode in the sweep, so the receiver really does
                # have to change spectra. With one mode selected there is
                # nothing to alternate with and the scenario is skipped.
                others = [m for m in modes if m != mode]
                if not others:
                    continue
                alt = others[(modes.index(mode)) % len(others)]
            for turn in spec.get("turnaround", (None,)):
                cells.append(Cell(
                    scenario=scenario, direction=direction, mode=mode,
                    head_seconds=float(head), gap_seconds=float(gap), rep=rep,
                    frames=int(spec.get("frames", 1)),
                    turnaround_seconds=None if turn is None else float(turn),
                    alt_mode=alt))
    return cells


# -- the run log ----------------------------------------------------------

class RunLog:
    """Append-only JSONL, one record per keying, resumable.

    Appended as each keying happens rather than written at the end: this
    matrix takes hours and *will* be interrupted, and a run whose results
    only exist in memory is a run that has to start over.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.capture_dir = self.path.with_suffix("")
        self._fh = None

    def completed_keys(self) -> set[str]:
        """Cell keys already recorded, for skipping on resume.

        A truncated final line (the interrupt landed mid-write) is dropped
        rather than raising: that cell simply gets run again, which is the
        correct outcome and the only one that lets a resume merge cleanly.
        """
        keys = set()
        if not self.path.exists():
            return keys
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                keys.add(json.loads(line)["cell_key"])
            except (ValueError, KeyError):
                continue
        return keys

    def records(self) -> list[dict]:
        """Every complete record in the log, for the aggregate report."""
        out = []
        if not self.path.exists():
            return out
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def append(self, record: dict) -> None:
        if self._fh is None:
            self._fh = self.path.open("a", encoding="utf-8")
        self._fh.write(json.dumps(record, default=_jsonable) + "\n")
        # Flushed per record, not per run: an interrupted run's last keying
        # is often the interesting one.
        self._fh.flush()

    def save_capture(self, name: str, audio) -> str:
        """Save one keying's capture beside the log, and return its path.

        float32 .npy at the 12 kHz receive rate, not .wav, for three
        reasons: it is exactly the array every decoder consumes, so the
        analyser re-runs acquisition on the same bytes the radio produced;
        int16 .wav would quantise away the low-level detail the envelope
        measurement is made of; and a float32 .wav is read by fewer tools
        than np.load is.
        """
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        path = self.capture_dir / f"{name}.npy"
        np.save(path, np.asarray(audio, np.float32))
        return str(path)

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _jsonable(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"not JSON-serialisable: {type(value).__name__}")


# -- ground truth ---------------------------------------------------------

def mode_registry():
    """Every HF mode, experimental included -- this is a bench, not a link."""
    return mode_qualification.registry("hf", "experimental")


def mode_by_name(name: str):
    for mode in mode_registry().modes:
        if mode.name == name:
            return mode
    raise KeyError(f"no HF mode named {name!r}")


def headed_mode(name: str, head_seconds: float):
    """`name` wearing a head of `head_seconds`."""
    if _with_settling_head is None:  # pragma: no cover
        raise RuntimeError(
            "whale.waveform.with_settling_head is unavailable; this bench "
            "cannot vary a head without it")
    return _with_settling_head(mode_by_name(name), head_seconds)


def preamble_offset_rx(name: str, head_seconds: float) -> int:
    """Samples, at the 12 kHz receive rate, from the start of a keying's
    audio to the first sample of its sync preamble.

    The one piece of ground truth everything else is measured against, and
    the reason this bench builds its own TX buffer. Each family rounds its
    head up its own way -- FSK to whole 4-symbol blocks, OFDM to whole
    symbols -- so `head_seconds` is a request, not the answer.
    `tests/test_head_bench.py` checks this against the PHYs themselves, so a
    rounding rule that changes upstream fails a test here rather than
    silently biasing every landing measurement.
    """
    fsk = _fsk_phy(name)
    if fsk is not None:
        return fsk.settling_head_samples(head_seconds) // DECIMATION
    phy = _ofdm_phy(name)
    n_head = int(math.ceil(head_seconds * RX_RATE / phy.symbol_len))
    return n_head * phy.symbol_len


def _ofdm_phy(name: str):
    from importlib import import_module
    try:
        module = import_module(f"whale.modes.{name}_mode")
        return getattr(module, f"{name.upper()}_PHY")
    except (ImportError, AttributeError) as exc:
        raise KeyError(f"{name} has no settling head to measure") from exc


def payload_for(run_id: int, mode, seq: int) -> bytes:
    """This keying's payload, derived from (run, mode, sequence).

    Reuses `whale.sweep.frame_payload` so a frame is verified byte for byte
    rather than merely CRC-passing, and so a capture saved today can be
    re-checked tomorrow from the record alone.
    """
    return sweep.frame_payload(run_id & 0xFFFF, mode.mode_id, seq & 0xFF,
                               AIR_HEADER_BYTES + mode.chunk_size)


def run_id_from(label: str) -> int:
    """A stable 16-bit run id from a run label, so a resume derives the same
    payloads as the run it is resuming."""
    return int.from_bytes(hashlib.sha256(label.encode()).digest()[:2], "big")


# -- quantity (A): the level envelope -------------------------------------

def level_envelope(audio, rate=RX_RATE, hop_seconds=ENVELOPE_HOP_SECONDS):
    """Per-hop RMS in dBFS. Needs no decode -- this is quantity (A)'s raw
    material, and it is measurable on a capture of a mode that never
    decoded at all."""
    audio = np.asarray(audio, dtype=np.float64).reshape(-1)
    hop = max(1, int(round(hop_seconds * rate)))
    n = len(audio) // hop
    if n == 0:
        return np.zeros(0)
    block = audio[:n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(block ** 2, axis=1))
    return 20.0 * np.log10(np.maximum(rms, 1e-12))


#: Median-smoothing width applied to the envelope before the settle search,
#: in hops. Every mode's own modulation moves the short-term level -- an
#: OFDM symbol's envelope is not flat -- and without smoothing that ripple
#: alone keeps tripping a 1 dB tolerance for the whole frame, reporting a
#: "settle time" equal to the frame length. Five hops (50 ms) is long
#: relative to a symbol in every HF mode here and short relative to the
#: 208-560 ms ramps being measured, so it removes the one and not the other.
SETTLE_SMOOTHING_HOPS = 5

#: The fraction of the keyed span, at its end, that the steady-state level
#: is estimated from. See settle_measurement's caveat: this measurement
#: needs a keying several ramp time constants long, and the shortest OFDM
#: frames barely are, so the reference window is taken as late as it can be
#: while still holding enough hops for a median.
STEADY_TAIL_FRACTION = 1.0 / 3.0

#: Tolerances the settle search is also reported at. One tolerance is a
#: choice; a campaign that ends up arguing about which one should be able to
#: read the other answers off the same capture instead of re-flying it.
SETTLE_TOLERANCE_LADDER_DB = (0.5, 1.0, 2.0, 3.0)

#: Consecutive settled hops required before the level counts as settled.
#: Scoring the *last* hop outside tolerance gives one noise-driven excursion
#: late in a keying the power to rescore the whole keying as still settling
#: -- observed at 4.26 s against a true 0.57 s, same cell, same physics,
#: different noise draw. The 5-hop median smoother already erases excursions
#: shorter than three hops, so a survivor is about three hops wide; 10 hops
#: (100 ms) is comfortably longer than that and still two to five times
#: shorter than the 208-560 ms ramps being measured, so it rejects the
#: spurious touch without blunting a real ramp.
SETTLED_RUN_HOPS = 10

#: Consecutive hops above the onset threshold before RF counts as on, and
#: the same run required from the other end before it counts as off.
#: Without it a single hop clearing floor + ONSET_RISE_DB was the onset, and
#: after a long listening gap the floor is quiet enough (-65 dB, against a
#: -18 dB keyed level) that one noisy hop clears a 6 dB threshold trivially.
#: Observed on `logs/head/smoke_ba.jsonl` index 5: onset read 0.26 s against
#: a true 0.55-0.58 s rise, and the whole keying was then measured from a
#: point 300 ms before RF existed -- reported as a 340 ms AGC ramp when the
#: true one is 40 ms. Every settle time, every `keyed_seconds`, and every
#: `landing` (whose truth reference is onset plus the known head) rests on
#: this index, so it has to be an edge the signal actually sustains.
#: SETTLED_RUN_HOPS is reused rather than a second constant invented: 100 ms
#: is the same "long enough not to be a noise draw, short enough not to
#: blunt a 208-560 ms ramp" judgement, made about the same envelope at the
#: same hop, and two numbers that must move together should be one.
ONSET_RUN_HOPS = SETTLED_RUN_HOPS

#: A settle time this far into the keyed span is not a measurement. Steady
#: state is estimated from the last STEADY_TAIL_FRACTION of the same span,
#: so this is exactly where that window begins: a settle time reaching into
#: it means the level was still moving inside the very span the reference
#: was taken from, and the docstring's short-frame caveat has come true.
#: Derived from STEADY_TAIL_FRACTION rather than chosen, because the
#: condition the estimate rests on is that the two spans stay disjoint.
MAX_SETTLE_FRACTION_OF_KEYING = 1.0 - STEADY_TAIL_FRACTION

#: Smallest floor-to-steady rise the settle point is allowed to rest on.
#: ONSET_RISE_DB itself, and deliberately not a second number: the level the
#: ramp settles *to* has to clear the floor by at least what it takes to
#: call RF present in the first place, or whatever fired the onset was a
#: transient and not the steady state the tolerance is measured against.
#:
#: What this replaces is a floor-referenced SNR gate -- median keyed level
#: minus floor, refused under 10 dB -- and the reason it had to go is that
#: the floor is the thing that moves. A receiver's AGC winds its gain up
#: through a listening gap, so the NOISE FLOOR climbs with it: measured at
#: -60 dB after a 0.5 s gap, -22 dB after 5 s, saturating there. On a `cold`
#: cell that ratio therefore collapses because the receiver got louder, not
#: because the signal got weaker -- all 10 records in
#: `logs/head/cold_ab.jsonl` were refused at 6.8-8.6 dB while every one
#: carries a flat -22 dB floor, a two-hop step to -14 dB and 5.5 s of level
#: sitting inside a few tenths of a dB of it. Refusing exactly the cold-start
#: captures a cold-start head budget is read from is the opposite of what
#: the guard was for.
MIN_SETTLE_RISE_DB = ONSET_RISE_DB


def settle_measurement(envelope_db, reference_db=None,
                       hop_seconds=ENVELOPE_HOP_SECONDS,
                       tolerance_db=SETTLE_TOLERANCE_DB,
                       rise_db=ONSET_RISE_DB, floor_hops=None,
                       smoothing_hops=SETTLE_SMOOTHING_HOPS,
                       tolerance_ladder=SETTLE_TOLERANCE_LADDER_DB):
    """QUANTITY (A): when RF appeared, and when the receive gain stopped moving.

    `settle_seconds` is measured from the detected RF onset to the first hop
    *within the keyed span* from which the level stays within `tolerance_db`
    of the steady state for SETTLED_RUN_HOPS consecutive hops -- "settled
    and stayed settled", not "first touched", so a ramp that overshoots and
    comes back is not scored as settled at the overshoot, and not "last
    touched" either, so one noise-driven hop late in the keying cannot
    rescore the whole keying. The span ends where RF ends: the silence after
    the un-key is tens of dB from steady state and would otherwise make
    every keying read as settling at its very last sample.

    RF onset, and the un-key that ends the keyed span, are edges the
    envelope has to *hold* for ONSET_RUN_HOPS, not single hops over the
    threshold. Every number here is measured from the onset index, and so
    is the preamble position `landing` is scored against, so a single noisy
    hop over a quiet floor taken as the onset does not merely add noise to
    the settle time -- it refers the whole keying to a moment before RF
    exists and inflates it by the distance to the real key-up.

    `settle_seconds` is None, with `settle_unmeasured_reason` saying why,
    whenever the evidence cannot sustain a number: a steady state that does
    not clear the noise floor by MIN_SETTLE_RISE_DB, so the onset fired on a
    transient (`rise-too-small`), a steady window that cannot hold
    SETTLED_RUN_HOPS within tolerance of its own median
    (`steady-state-unstable`), no run of
    settled hops to call a steady state (`no-settled-run`), or a settle time
    occupying more than MAX_SETTLE_FRACTION_OF_KEYING of the keyed span
    (`settle-exceeds-keyed-span`, the caveat below come true). An absent
    measurement is a fact the campaign can work with; a confident wrong one
    silently poisons every aggregate it lands in.

    `keyed_snr_db` is reported and never gated on. It is the median keyed
    level over the pre-key floor, and the floor is the thing that moves: the
    receiver's AGC winds its gain up through a listening gap, so on a cold
    keying the floor rises tens of dB while the signal does not, and a guard
    made of that ratio refuses the very captures a cold-start head budget is
    read from. `steady_rise_db` is the quantity the refusal is made on.

    `reference_db` is the envelope of the audio we *transmitted*, at the
    same hop, and passing it is what makes this a measurement rather than an
    estimate. Subtracting it leaves the path gain against time, with the
    waveform's own level structure divided out -- which matters a great
    deal: an OFDM frame's envelope ripples a couple of dB all by itself,
    and `whale/phy/ofdm49.py` normalises the settling head separately from
    the frame, so a raw envelope has a step at the head/body boundary that
    is not the AGC at all. Only a process that owns the TX buffer can do
    this, which is the third reason this bench is one process.

    Without a reference it falls back to the raw envelope, which is honest
    for the constant-envelope FSK modes and coarse for the OFDM ones.

    `settle_by_tolerance_db` reports the same search at a ladder of
    tolerances. One tolerance is a choice, and a campaign that has to argue
    about which one should be able to read the other answers off the same
    capture rather than re-flying it.

    CAVEAT, and it is a real one: steady state is estimated from the end of
    the same keying, so a keying that is not several ramp time constants
    long has no settled part to estimate it from and the result is biased
    high. `keyed_seconds` is returned so this stays checkable -- and this
    function now makes that check itself rather than leaving it to a reader
    who may never make it, which is what `settle-exceeds-keyed-span` is.
    """
    env = np.asarray(envelope_db, dtype=np.float64)
    out = {"onset_seconds": None, "settle_seconds": None,
           "settle_unmeasured_reason": None,
           "settle_by_tolerance_db": {}, "referenced": reference_db is not None,
           "steady_db": None, "floor_db": None, "keyed_seconds": None,
           "keyed_snr_db": None, "steady_rise_db": None,
           "tolerance_db": float(tolerance_db),
           "peak_db": float(np.max(env)) if len(env) else None}
    if len(env) < 4:
        out["settle_unmeasured_reason"] = "capture-too-short"
        return out
    # The floor is measured before the key, from the pre-roll. Default to a
    # tenth of the capture so a slice with a short pre-roll still yields
    # something rather than dividing by nothing.
    floor_hops = max(2, len(env) // 10) if floor_hops is None else floor_hops
    floor = float(np.median(env[:floor_hops]))
    out["floor_db"] = floor

    # RF is on where the envelope gets over the threshold and *stays* over
    # it for ONSET_RUN_HOPS, and off where the last such run ends. A single
    # hop over a quiet floor is a noise draw, not a key-up, and taking it as
    # the onset refers the entire measurement to a point before RF exists.
    #
    # Tested on the median-smoothed envelope, the same smoother the settle
    # search uses and for the mirror-image reason: demanding a run of raw
    # hops makes one hop that dips back below the threshold break it, which
    # on a keying sitting only ONSET_RISE_DB over the floor is a coin flip.
    # Measured on `logs/head/cold_ab.jsonl` index 6: one hop 0.08 dB under
    # the threshold, in the middle of an otherwise clean step, moved the
    # onset 60 ms late. A median is edge-preserving, so it erases that dip
    # without dragging the key-up edge earlier.
    onset, rf_end = _sustained_span(
        _median_smooth(env, smoothing_hops) > floor + rise_db, ONSET_RUN_HOPS)
    if onset is None:
        out["settle_unmeasured_reason"] = "no-rf-onset"
        return out
    out["onset_seconds"] = onset * hop_seconds
    out["keyed_seconds"] = (rf_end - onset) * hop_seconds

    raw_keyed = keyed = env[onset:rf_end]
    # Reported, never gated on: the median spans the ramp as well as the
    # steady part, so it is a summary of the keying and not a statement
    # about whether the ramp in it is readable. `steady_rise_db` below is
    # the quantity the refusal is made on.
    out["keyed_snr_db"] = float(np.median(keyed)) - floor
    if reference_db is not None:
        reference = np.asarray(reference_db, dtype=np.float64)
        n = min(len(keyed), len(reference))
        keyed = keyed[:n] - reference[:n]
    keyed = _median_smooth(keyed, smoothing_hops)
    # The hop straddling the un-key is part silence, so it sits tens of dB
    # below steady state while still clearing the onset threshold, and the
    # smoothing window drags that edge inwards. Left in, it alone would make
    # every keying report a settle time equal to its whole length. Guarding
    # the trailing hops is the honest fix: the ramp being measured is at the
    # *start* of the keying and nothing is lost by not searching the end.
    guard = max(1, int(smoothing_hops))
    searchable = keyed[:max(0, len(keyed) - guard)]
    if len(searchable) < 4:
        out["settle_unmeasured_reason"] = "keying-too-short"
        return out
    # Steady state is the last third of the searchable span, by median
    # rather than mean so one clipped hop cannot move it. A third rather
    # than a half because the short OFDM keyings are only a few ramp time
    # constants long, and a steady estimate taken from a span that is still
    # ramping biases the settle time upwards -- see the caveat above.
    tail = max(4, int(len(searchable) * STEADY_TAIL_FRACTION))
    steady = float(np.median(searchable[-tail:]))
    out["steady_db"] = steady
    # How far the steady state stands over the pre-key floor, measured on
    # the raw envelope over the same hops the steady estimate came from --
    # raw because the floor is raw, and over the steady window rather than
    # the whole keying because the ramp at the front is not part of the
    # question. This is what the refusal below is made on.
    raw_tail = raw_keyed[:len(searchable)][-tail:]
    out["steady_rise_db"] = float(np.median(raw_tail)) - floor
    for tol in sorted({float(tolerance_db), *map(float, tolerance_ladder)}):
        first = _first_settled_hop(searchable - steady, tol, SETTLED_RUN_HOPS)
        out["settle_by_tolerance_db"][f"{tol:g}"] = (
            None if first is None else first * hop_seconds)
    settle = out["settle_by_tolerance_db"][f"{float(tolerance_db):g}"]

    # Refuse to return a number the evidence does not support. These are
    # ordered physics first: with no readable ramp the excursions being
    # timed are the noise, so nothing downstream of that is worth reporting.
    if out["steady_rise_db"] < MIN_SETTLE_RISE_DB:
        # RF was detected but the level the keying ends up at is back down
        # near the floor, so the onset fired on a transient and there is no
        # steady state above the noise for a settle point to refer to.
        out["settle_unmeasured_reason"] = "rise-too-small"
    elif _first_settled_hop(searchable[-tail:] - steady, float(tolerance_db),
                            SETTLED_RUN_HOPS) is None:
        # There is no steady state. The window the steady level was
        # estimated from cannot itself hold SETTLED_RUN_HOPS inside
        # tolerance of that level, so "within tolerance of steady" is not a
        # condition this capture can meet for a reason that has anything to
        # do with the AGC, and a settle time found against it would be
        # timing the wander rather than the ramp.
        out["settle_unmeasured_reason"] = "steady-state-unstable"
    elif settle is None:
        out["settle_unmeasured_reason"] = "no-settled-run"
    elif settle > MAX_SETTLE_FRACTION_OF_KEYING * out["keyed_seconds"]:
        out["settle_unmeasured_reason"] = "settle-exceeds-keyed-span"
    else:
        out["settle_seconds"] = settle
    return out


def _first_settled_hop(deviation, tolerance, run_hops=SETTLED_RUN_HOPS):
    """Index of the first hop from which the level stays inside `tolerance`.

    "Settled" means the start of a run of `run_hops` consecutive hops inside
    tolerance, not merely one hop past the last excursion. On a monotone
    ramp the two are the same answer -- which is why this costs no
    sensitivity on a real AGC curve -- but only this one ignores an isolated
    late excursion instead of letting one noise-driven hop rescore the whole
    keying. Returns None when no such run exists: that is a keying with no
    steady state in it, and it must be reported as unmeasured rather than as
    a settle time equal to the whole span.
    """
    settled = np.abs(np.asarray(deviation, dtype=np.float64)) <= tolerance
    if not len(settled):
        return None
    run = min(int(run_hops), len(settled))
    # A window of `run` hops is all-settled exactly when it sums to `run`.
    windows = np.convolve(settled.astype(int), np.ones(run, int), mode="valid")
    starts = np.flatnonzero(windows == run)
    return None if not len(starts) else int(starts[0])


def _sustained_span(flags, run_hops):
    """(start, end) of the first and last runs of `run_hops` true hops.

    `start` is where the first such run begins and `end` one past where the
    last one ends, so the span is bounded by edges the signal held rather
    than by the first and last isolated hop -- the same "and stayed there"
    rule `_first_settled_hop` applies, read from both ends. The two ends
    need it for the same reason: the pre-key floor and the post-un-key
    silence are noise, and one noise draw over the threshold at either end
    moves the boundary by however far away from RF it happened to land.

    Returns (None, None) when no run that long exists -- a capture with no
    sustained RF in it, which is `no-rf-onset` and not a short keying: the
    keyings this bench makes are seconds long and 100 ms of RF that does not
    persist is not one of them.
    """
    flags = np.asarray(flags, dtype=bool)
    run = min(int(run_hops), len(flags))
    if run < 1:
        return None, None
    windows = np.convolve(flags.astype(int), np.ones(run, int), mode="valid")
    starts = np.flatnonzero(windows == run)
    if not len(starts):
        return None, None
    return int(starts[0]), int(starts[-1]) + run


def _median_smooth(values, width):
    """Running median. Odd widths only; width <= 1 is a pass-through."""
    values = np.asarray(values, dtype=np.float64)
    width = int(width)
    if width <= 1 or len(values) < width:
        return values
    if width % 2 == 0:
        width += 1
    pad = width // 2
    padded = np.pad(values, pad, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, width)
    return np.median(windows, axis=1)


# -- decoding and classification ------------------------------------------

#: `raw_ber`'s provenance, recorded per frame so the two are never mixed
#: silently in an aggregate. `shipped-decode` means the bit errors were
#: counted against bits the shipped receive path itself published
#: (`pre_fec_bits` for OFDM, `ber` for a whole-capture FSK debug decode).
#: `debug-rerun-on-acquired-span` means the shipped FSK result carried no
#: BER -- `whale/phy/hc0.py`'s `demodulate` has no reference payload to
#: compare against, only `demodulate_debug` does -- so the phy's own debug
#: entry point was re-run over exactly the span the shipped receiver
#: acquired and decoded. The bits are the same bits; the acquisition behind
#: them was run a second time, which is the one thing about it that is not
#: the shipped receiver's own arithmetic.
BER_FROM_SHIPPED_DECODE = "shipped-decode"
BER_FROM_DEBUG_RERUN = "debug-rerun-on-acquired-span"


def _fsk_phy(name: str):
    """The FSK phy module for `name`, or None if it is not an FSK mode."""
    from whale.phy import hc0, hc1w, hr0
    return {"hr0": hr0, "hc0": hc0, "hc1w": hc1w}.get(name)


def normalise_result(name: str, result: dict, reference: bytes) -> dict:
    """One decode result in the bench's shape, whatever produced it.

    The families return different key names for the same fact (`start_index`
    vs `start_sample`, `cfo_hz` vs `freq_offset_hz`) and reach raw BER by
    different routes. Normalising here means everything downstream -- the
    record, the analyser, the summary table -- reads one shape, and that the
    shipped receive path and the offline whole-capture decode below are read
    through exactly the same mapping rather than two that can drift.
    """
    out = {"confidence": float(result.get("confidence", 0.0) or 0.0),
           "start_index": None, "payload_ok": False,
           "synced": bool(result.get("synced")), "cfo_hz": None,
           "raw_ber": None, "raw_ber_source": None, "carrier_snr_db": None,
           "tone_snr_db": None, "failure": None, "decoded_bytes": None}
    if _fsk_phy(name) is not None:
        out.update(
            start_index=_as_int(result.get("start_index")),
            cfo_hz=_as_float(result.get("cfo_hz")),
            # Present only when the result came from `demodulate_debug`,
            # which is the only FSK entry point that is given the reference.
            raw_ber=_as_float(result.get("ber")),
            tone_snr_db=_as_float(result.get("tone_snr_db")),
            carrier_snr_db=_as_list(result.get("carrier_snr_db")),
            failure=result.get("failure"))
    else:
        # `pre_fec_bits` and `per_bin_snr_db` are published by every OFDM
        # decode, diagnostics or not, so the shipped receive path's own
        # result carries both without being asked for anything extra.
        out.update(
            start_index=_as_int(result.get("start_sample")),
            cfo_hz=_as_float(result.get("freq_offset_hz")),
            carrier_snr_db=_as_list(result.get("per_bin_snr_db")),
            raw_ber=_ofdm_raw_ber(_ofdm_phy(name), result, reference),
            failure=None if result.get("crc_ok") else "crc")
    if out["raw_ber"] is not None:
        out["raw_ber_source"] = BER_FROM_SHIPPED_DECODE
    payload = result.get("payload")
    out["decoded_bytes"] = None if payload is None else len(payload)
    out["payload_ok"] = payload == reference
    return out


def debug_decode(name: str, audio_rx, reference: bytes) -> dict:
    """Whole-capture decode of one frame, taking the best-scoring position.

    NOT what the shipped receiver does, and deliberately kept: this is the
    offline analysis primitive `scripts/head_analyse.py` builds --trim and
    --capture out of, where the question is "what is in this slice of
    audio", asked once of a slice the analyser chose itself. The run log's
    per-keying decode goes through `receive_keying` instead, which is the
    receiver the operator actually runs -- see its docstring.
    """
    audio_rx = np.asarray(audio_rx, dtype=np.float64).reshape(-1)
    if not len(audio_rx):
        out = normalise_result(name, {}, reference)
        out["failure"] = "empty capture"
        return out
    phy = _fsk_phy(name)
    if phy is not None:
        debug = getattr(phy, "demodulate_debug", None)
        result = (debug(audio_rx, reference) if debug is not None
                  else phy.demodulate(audio_rx))
    else:
        result = _ofdm_phy(name).demodulate(audio_rx, diagnostics=True)
    return normalise_result(name, result, reference)


def _ofdm_raw_ber(phy, result, reference: bytes):
    """Pre-FEC bit error rate against the bits we know we transmitted.

    `pre_fec_bits` is the deinterleaved coded stream, which is exactly what
    `pack_and_encode_bits` produces before interleaving -- so the comparison
    is like for like without re-deriving the interleaver here.
    """
    bits = result.get("pre_fec_bits")
    if bits is None:
        return None
    try:
        _, coded = phy.pack_and_encode_bits(reference)
    except Exception:
        return None
    n = min(len(bits), len(coded))
    if not n:
        return None
    return float(np.mean(np.asarray(bits[:n]) != np.asarray(coded[:n])))


def _as_int(value):
    return None if value is None else int(value)


def _as_float(value):
    if value is None:
        return None
    value = float(value)
    return value if np.isfinite(value) else None


def _as_list(value):
    if value is None:
        return None
    arr = np.asarray(value, dtype=float).reshape(-1)
    if not len(arr) or not np.all(np.isfinite(arr)):
        return None
    return [round(float(v), 2) for v in arr]


def outcome_of(result: dict, threshold: float) -> str:
    """decoded / synced / never-seen -- the taxonomy `whale/sweep.py` uses.

    "synced" is the interesting one: the preamble was found and the frame
    still did not come out, which means the head is not the problem.
    """
    if result["payload_ok"]:
        return "decoded"
    if result["synced"] or result["confidence"] >= threshold:
        return "synced"
    return "never-seen"


def landing_of(result: dict, true_preamble_index: int | None,
               threshold: float,
               tolerance=LANDING_TOLERANCE_SAMPLES,
               onset_index: int | None = None) -> dict:
    """Where acquisition landed relative to the preamble we know we sent.

    `in-head` is the failure worth naming: the head out-scored the real
    preamble, so the decoder starts on noise-shaped modulation that carries
    nothing. It is the failure mode `whale/streaming.py`'s 0.12 candidate
    gate is closest to, given the 0.10-0.13 in-head scores the OFDM head
    measures, and it is indistinguishable from "never acquired" in any
    pass/fail report.

    `before-onset` is the other early failure, and a different bug: the
    decoder locked onto something before RF even started -- silence, noise,
    or a previous keying's tail. There is no head there to blame, so calling
    it `in-head` (as this used to, unconditionally, for anything before the
    preamble) is a false accusation of the settling head; it is worst for a
    keying with `head_seconds` 0, where there is no head at all to land in.
    Only reported when `onset_index` is given -- callers that omit it keep
    the old, coarser in-head-or-late split.

    The truth reference is the detected RF onset plus the known head length,
    so its accuracy is the onset detector's -- hence `tolerance`.
    """
    out = {"landing": "never-acquired", "landing_error_samples": None}
    start = result["start_index"]
    if start is None or result["confidence"] < threshold:
        return out
    if true_preamble_index is None:
        out["landing"] = "unknown"
        return out
    error = int(start) - int(true_preamble_index)
    out["landing_error_samples"] = error
    if error < -tolerance:
        if onset_index is not None and int(start) < int(onset_index) - tolerance:
            out["landing"] = "before-onset"
        else:
            out["landing"] = "in-head"
    elif error > tolerance:
        out["landing"] = "late"
    else:
        out["landing"] = "on-preamble"
    return out


def in_head_confidence(name: str, audio_rx, onset_index: int,
                       true_preamble_index: int) -> float | None:
    """The best acquisition score found anywhere *inside* the head.

    Measured by running the mode's own acquisition on the head alone: if
    that score clears the candidate gate, a real receiver can lock onto the
    head of its own transmission's twin and never see the preamble behind
    it. Worth recording on passing keyings too -- it is the margin, and a
    margin that has quietly gone to zero is what a sweep is for.
    """
    lo = max(0, int(onset_index))
    hi = int(onset_index) + int(true_preamble_index)
    if hi - lo < RX_RATE // 20:  # under 50 ms of head: nothing to score
        return None
    head = np.asarray(audio_rx, dtype=np.float64).reshape(-1)[lo:hi]
    try:
        phy = _fsk_phy(name)
        if phy is not None:
            return float(phy.demodulate(head).get("confidence", 0.0) or 0.0)
        confidence, _, _ = _ofdm_phy(name).acquire(head)
        return float(confidence)
    except Exception:
        # Acquisition on a deliberately truncated slice is allowed to fail;
        # a missing margin is not a reason to lose the whole keying.
        return None


# -- the shipped receive path, driven over a capture ----------------------

#: Which receiver produced a record. Stamped into every JSONL record so a
#: log written before this bench moved onto the shipped receive machinery
#: cannot be silently aggregated together with one written after: the two
#: answer different questions on a burst, and the difference is the whole
#: reason the move happened.
RECEIVE_PATH = "streaming"

#: How much audio the replay transport hands over per poll. The shipped
#: decode loop polls at `link_receiver.DECODE_POLL_INTERVAL` and takes
#: whatever the sound card has produced since, so this is that interval's
#: worth. It is not a free parameter: it has to stay under
#: `whale/streaming.py`'s 0.25 s `OfdmSearch` block, or a burst's frames
#: would reach the receiver in lumps bigger than the window it searches and
#: the search would no longer be the shipped one.
RECEIVE_CHUNK_SECONDS = link_receiver.DECODE_POLL_INTERVAL


class _ReplayTransport:
    """One already-captured array, served as the transport surface the
    shipped receiver reads.

    Exactly the receive half of `whale/transport.py`'s surface that
    `whale.link_receiver._ReceiverMixin` touches -- `read_rx`,
    `consume_rx_through`, `rx_generation`, `is_transmitting` and the
    start/stop pair -- and nothing else. No sound card, no PTT, nothing
    that can key a radio: this replays a capture that already happened.

    Samples are handed over a chunk at a time rather than all at once, and
    that is the point of the class. The receiver must see the keying arrive
    the way it arrives on the air, because "the first adequate frame wins"
    is a property of a receiver that has only heard the front of a burst
    when it decides. Handed the whole capture in one go, even the shipped
    search would be choosing among every frame at once.
    """

    def __init__(self, audio, chunk_samples=None):
        self._audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        self._chunk = max(1, int(chunk_samples if chunk_samples is not None
                                 else RECEIVE_CHUNK_SECONDS * RX_RATE))
        #: The capture is one uninterrupted recording, so there is exactly
        #: one generation. A real dropout is counted by `ContinuousCapture`
        #: while the audio is being recorded, not re-invented here.
        self.rx_generation = 0
        self._served = 0
        self.consumed = 0

    @property
    def exhausted(self) -> bool:
        return self._served >= len(self._audio)

    def is_transmitting(self) -> bool:
        return False

    def start_receiving(self) -> None:
        pass

    def stop_receiving(self) -> None:
        pass

    def read_rx(self, cursor=None):
        start = self._served
        if cursor is not None and cursor[0] == self.rx_generation:
            start = int(cursor[1])
        end = min(len(self._audio), start + self._chunk)
        self._served = max(self._served, end)
        return self.rx_generation, start, self._audio[start:end]

    def consume_rx_through(self, generation, end) -> None:
        if generation == self.rx_generation:
            self.consumed = max(self.consumed, int(end))


class BenchReceiver(link_receiver._ReceiverMixin):
    """The shipped receive path, listening to one capture this bench keyed.

    Follows `whale/sweep.py`'s precedent: mix in
    `whale.link_receiver._ReceiverMixin` and override only the two methods
    that are about ARQ -- which candidate profiles to search, and what a
    decoded payload means. Everything that decides *where a frame is* stays
    the shipped code: `whale/streaming.py`'s `ReceiveStream`, its
    `OfdmSearch` walking fixed 0.25 s windows and taking the first candidate
    over the 0.12 gate, the mixin's wait-for-more-audio rule while a frame
    is still arriving, its near-miss rule that tells "synced but did not
    check out" from "never seen", and its `_prune_stale` retention window.

    Why this matters more here than anywhere else: the bench used to decode
    a capture by handing the whole thing to the mode's own whole-capture
    demodulator and taking the globally best-scoring position. On a
    multi-frame burst that is never frame 0 -- frame 0 is the one the AGC
    ramp attenuates -- so the bench reported every burst as nothing decoded
    with every frame one stride late, on the one scenario that answers
    whether the head is needed once per keying or once per frame.

    Two departures from sweep.py, both forced by this being an offline
    instrument rather than a station:

      - `_ReceiverMixin._decode_loop` is a thread polling a live sound card
        and sleeping between polls. `run()` below is that loop's body with
        the threading, the sleeps and the is-transmitting check taken out
        and nothing else changed, so one capture measures deterministically
        instead of racing a wall clock.
      - the bench has no session and no ARQ, so `mycall`, `state` and the
        peer-mode set are fixed, the way sweep.py fixes them.
    """

    def __init__(self, transport, profile, policy=None):
        self.transport = transport
        self.mycall = "BENCH"
        # Read by the mixin. One profile is searched: the bench knows
        # exactly which mode it keyed, and searching others would only add
        # ways to mis-measure it.
        self.profile = profile
        self.rx_profile = profile
        self.policy = whale_policy.HF if policy is None else policy
        self.state = "LISTENING"
        self.peer_supported_modes = set()
        self.on_event = lambda name, **kw: None
        self._rx_packets = queue.Queue()
        #: Enough retained audio that the longest frame this profile could
        #: be part-way through is never cut in half -- sized exactly as
        #: `whale/link.py` sizes it.
        self._rx_keep_seconds = profile.airtime(
            AIR_HEADER_BYTES + profile.chunk_size) + 1.0
        #: Every acquisition the shipped path made, in the order it made
        #: them. This is the bench's whole output from the receiver.
        self.acquisitions: list[dict] = []
        self._init_receiver()

    # -- the two ARQ methods, replaced ------------------------------------

    def _candidate_decode_profiles(self, snap=None):
        return (self.profile,)

    def _decode_one(self, snap) -> bool:
        """Take one frame or one near-miss out of `snap`, and record it.

        The same shape as `_ReceiverMixin._decode_one` and sweep.py's: a
        payload is consumed through `end_index`; a candidate over the gate
        with no `end_index` is a frame still arriving, so the audio is kept
        and nothing is consumed; a result carrying `end_index` without a
        payload is a near miss, consumed through its sync end. What is
        replaced is only what a decoded payload *means*: the bench parses no
        air header, it hands the acquisition to the ground truth it holds.
        """
        profile = self.profile
        result = self._decode_attempt(profile, snap)
        if result.get("payload") is not None:
            end = result.get("end_index", len(snap))
            self._observe(result, snap, end)
            # The shipped receiver learns the peer's frequency offset from
            # every frame it takes and hints the next decode with it. Kept
            # because it changes what the next frame's acquisition sees --
            # dropping it would quietly measure a receiver with a colder
            # start on every frame of a burst than the one that ships.
            offset = result.get("freq_offset_hz", result.get("cfo_hz"))
            if (self.policy.track_frequency_offset and offset is not None
                    and np.isfinite(offset)):
                self._rx_frequency_hint_hz = float(offset)
            self._consume_rx(end)
            return True
        pending = (result.get("confidence", 0) >= profile.confidence_threshold
                   and "end_index" not in result)
        if not pending and "end_index" in result:
            skip = result.get("sync_end_index", result["end_index"])
            self._observe(result, snap, skip)
            self._consume_rx(skip)
            return True
        self._prune_stale(len(snap))
        return False

    # -- what the bench keeps ---------------------------------------------

    def _observe(self, result, snap, end) -> None:
        """Remember one acquisition, in the capture's own coordinates.

        Indices inside `result` are offsets into the retained snapshot,
        which slides forward as audio is consumed. Every index the bench
        records is an index into the whole capture instead, because that is
        the only coordinate system the preamble positions it computed when
        it built the TX buffer live in.
        """
        base = self._receive_stream.audio.start
        start = result.get("start_index")
        if start is None:
            start = result.get("start_sample")
        span = None
        if start is not None and end is not None and int(end) > int(start):
            span = np.asarray(snap[int(start):int(end)], dtype=np.float64)
        self.acquisitions.append({
            "result": result,
            "start_index": None if start is None else base + int(start),
            "span": span,
        })

    # -- the decode loop, offline -----------------------------------------

    def run(self):
        """`_ReceiverMixin._decode_loop`, without the thread or the clock.

        The shipped loop's body: poll the transport, append to the
        `ReceiveStream`, decode the retained snapshot, and go round again --
        until a poll that brought nothing new also decoded nothing and the
        capture is spent. What is gone is the `time.sleep` between idle
        polls, the `is_transmitting` guard (a replayed capture never
        transmits) and the stop event.
        """
        from whale.transport import RX_BUFFER_SECONDS, RX_SAMPLE_RATE
        self._receive_stream = ReceiveStream(
            int(RX_BUFFER_SECONDS * RX_SAMPLE_RATE))
        stream = self._receive_stream
        retry = False
        while True:
            generation, start, samples = self.transport.read_rx(stream.cursor)
            changed = (generation != stream.generation
                       or start != stream.audio.end)
            stream.append(generation, start, samples)
            if not len(samples) and not changed and not retry:
                if self.transport.exhausted:
                    return self.acquisitions
                continue
            snap = stream.audio.read()
            retained = stream.audio.start
            retry = bool(len(snap)) and self._decode_one(snap)
            # The shipped loop is stopped by an event on another thread and
            # can afford to retry a snapshot forever; this one is driven by
            # a finite capture and must not. A decode that reports progress
            # without consuming anything would spin on the same samples, so
            # it gets the next poll's audio instead of another look at the
            # same audio -- a receiver that is stuck is a finding, not a
            # reason for an offline instrument to hang.
            retry = retry and stream.audio.start > retained


def receive_keying(name: str, capture_rx, profile=None, chunk_samples=None):
    """Every acquisition the shipped receiver makes on one keying's capture.

    This is the bench's decode path, and it is the shipped one: the capture
    is replayed into `whale.link_receiver._ReceiverMixin` through
    `whale/streaming.py`'s `ReceiveStream`, so frames are found in arrival
    order, the first adequate one wins, against the same 0.12 candidate gate
    the operator's modem runs. No acquisition logic is re-implemented here.

    The profile searched is the mode as shipped, not a head-resized variant:
    a settling head changes what is keyed and never how it is acquired, so
    the receiver under test is the one with no knowledge of the head at all.
    """
    capture_rx = np.asarray(capture_rx, dtype=np.float32).reshape(-1)
    if not len(capture_rx):
        return []
    profile = mode_by_name(name) if profile is None else profile
    receiver = BenchReceiver(_ReplayTransport(capture_rx, chunk_samples),
                             profile)
    return receiver.run()


def frame_result(name: str, acquisition, reference: bytes) -> dict:
    """One acquisition, normalised and scored against what we sent it.

    Raw BER is taken from the shipped decode's own published bits wherever
    it publishes them, which is every OFDM decode, via `pre_fec_bits`. The
    FSK phys publish `raw_payload_bits` but turn them into a BER only inside
    `demodulate_debug`, which the shipped receiver never calls because it
    has no reference payload to call it with. Rather than re-derive each
    family's bit packing here -- hc0 and hc1w do not agree on it -- the
    phy's own debug entry point is re-run over exactly the span the shipped
    receiver acquired and decoded, and the frame says so in `raw_ber_source`.
    The bits compared are the same bits; the acquisition behind them ran
    twice, and that is the one part of this figure that is not the shipped
    receiver's own arithmetic.
    """
    result = acquisition["result"]
    out = normalise_result(name, result, reference)
    out["start_index"] = acquisition["start_index"]
    if out["raw_ber"] is None and acquisition.get("span") is not None:
        phy = _fsk_phy(name)
        debug = None if phy is None else getattr(phy, "demodulate_debug", None)
        if debug is not None:
            try:
                ber = _as_float(debug(acquisition["span"], reference).get("ber"))
            except Exception:
                # A BER is a diagnostic; losing it must not lose the frame.
                ber = None
            if ber is not None:
                out["raw_ber"] = ber
                out["raw_ber_source"] = BER_FROM_DEBUG_RERUN
    return out


def assign_frames(acquisitions, frames: int, true_preamble, stride: int):
    """Which reference frame each acquisition belongs to.

    The bench built the TX buffer, so frame `seq`'s preamble sits exactly at
    `true_preamble + seq * stride` and nothing is inferred from the receiver
    being measured: an acquisition is filed under the frame whose *known*
    preamble position it is nearest. That is what keeps a burst's landings
    honest. A frame acquired a stride late is reported as `late` against its
    own frame rather than quietly renumbered into the next one, and a frame
    the receiver skipped stays `never-seen` instead of shifting every frame
    behind it by one and misreading them all as failures.

    Acquisitions arrive in order, so a frame already filled is never
    refilled and a later acquisition never claims an earlier frame: the
    first adequate acquisition for a frame is the one that counts, which is
    the receiver's own rule. Returns one entry per frame, each an
    acquisition or None, and the acquisitions that matched no frame.
    """
    assigned = [None] * max(1, int(frames))
    unmatched = []
    lowest = 0
    for acquisition in acquisitions:
        start = acquisition["start_index"]
        if true_preamble is None or stride <= 0 or start is None:
            # No ground truth to file against (RF onset was never detected),
            # or a single-frame keying with no stride: fall back to arrival
            # order, which is then all there is to go on.
            seq = next((i for i in range(lowest, len(assigned))
                        if assigned[i] is None), None)
        else:
            seq = int(round((start - true_preamble) / stride))
            seq = min(max(seq, lowest), len(assigned) - 1)
        if seq is None or assigned[seq] is not None:
            unmatched.append(acquisition)
            continue
        assigned[seq] = acquisition
        lowest = seq + 1
    return assigned, unmatched


# -- the per-keying measurement (pure: capture in, record out) ------------

def frame_stride_rx(tx_audio, frames: int) -> int:
    """Samples, at the receive rate, from one frame's start to the next's.

    One keying is one mode, one head and one payload size repeated, so
    `keying_audio` concatenates `frames` encodings of identical length and
    the stride is exactly the TX buffer divided by the frame count. Derived
    rather than estimated on purpose: the per-frame preamble position is the
    reference every landing in a burst is measured against, and estimating
    it from where acquisition happened to land would measure the decoder
    against itself.
    """
    if tx_audio is None or int(frames) <= 1:
        return 0
    # The same decimation `measure_keying` takes the TX envelope at, so the
    # stride lands in the capture's own sample rate.
    n_rx = len(np.asarray(tx_audio).reshape(-1)[::DECIMATION])
    return n_rx // int(frames)


def measure_keying(cell: Cell, name: str, capture_rx, reference_payloads,
                   threshold: float, head_seconds: float,
                   tx_audio=None, extra: dict | None = None) -> dict:
    """Everything one keying is worth, from its capture and what we sent.

    Deliberately takes audio rather than a radio: this is the half of the
    bench that is testable without hardware, and `tests/test_head_bench.py`
    drives it with synthesised captures whose settle time and preamble
    position are known by construction.

    `tx_audio` is the 48 kHz buffer that was keyed. It is what turns the
    level envelope into a path-gain measurement (see `settle_measurement`);
    without it the settle time still comes out, less precisely.
    """
    capture_rx = np.asarray(capture_rx, dtype=np.float32).reshape(-1)
    envelope = level_envelope(capture_rx)
    tx_envelope = (None if tx_audio is None else
                   level_envelope(np.asarray(tx_audio, np.float64)[::DECIMATION]))
    settle = settle_measurement(envelope, tx_envelope)

    preamble_offset = preamble_offset_rx(name, head_seconds)
    onset_index = (None if settle["onset_seconds"] is None
                   else int(round(settle["onset_seconds"] * RX_RATE)))
    true_preamble = (None if onset_index is None
                     else onset_index + preamble_offset)

    stride = frame_stride_rx(tx_audio, len(reference_payloads))

    # The shipped receiver, over this capture. It walks the keying forward
    # and takes the first frame that clears the gate rather than the best
    # one in the whole capture, which on a burst is the difference between
    # finding frame 0 and never finding it -- see `receive_keying`.
    acquisitions = receive_keying(name, capture_rx)
    assigned, unmatched = assign_frames(acquisitions, len(reference_payloads),
                                        true_preamble, stride)

    frames = []
    for seq, reference in enumerate(reference_payloads):
        acquisition = assigned[seq] if seq < len(assigned) else None
        located = (frame_result(name, acquisition, reference)
                   if acquisition is not None else
                   # The receiver never acquired anything at this frame's
                   # position. That is a fact about the search, not a decode
                   # that came out wrong, so it carries no decode diagnostics
                   # and its failure names what actually happened.
                   dict(normalise_result(name, {}, reference),
                        failure="no acquisition at this frame's position"))
        # Frame `seq`'s preamble sits `seq` whole frames after frame 0's.
        # Re-based per frame because one keying spreads its frames over its
        # whole span: measured against frame 0's position, every later
        # frame's landing is a fixed multiple of the stride out and says
        # nothing at all.
        expected = (None if true_preamble is None
                    else true_preamble + seq * stride)
        frames.append({
            "seq": seq,
            "outcome": outcome_of(located, threshold),
            "expected_preamble_index": expected,
            **{k: located[k] for k in (
                "confidence", "start_index", "cfo_hz", "raw_ber",
                "raw_ber_source", "carrier_snr_db", "tone_snr_db", "failure",
                "decoded_bytes")},
            **landing_of(located, expected, threshold,
                         onset_index=onset_index),
        })

    record = {
        "cell_key": cell.key,
        **asdict(cell),
        "head_seconds": float(head_seconds),
        "preamble_offset_rx": int(preamble_offset),
        "true_preamble_index": true_preamble,
        # Frame 0's preamble is at `true_preamble_index`; frame n's is that
        # plus n strides. Recorded so a landing can be re-checked from the
        # log alone.
        "frame_stride_rx": int(stride),
        "onset_index": onset_index,
        "confidence_threshold": float(threshold),
        # Which receiver produced the frames below. A record without this
        # field came from the whole-capture best-argmax decode this bench
        # used before it moved onto the shipped machinery, and the two must
        # never be aggregated together -- they disagree on every burst.
        "receive_path": RECEIVE_PATH,
        "receive_chunk_seconds": RECEIVE_CHUNK_SECONDS,
        # Acquisitions the shipped receiver made that belong to no frame the
        # bench sent. Non-zero means it locked onto something that is not
        # one of our preambles, which is a finding, not noise to hide.
        "acquisition_count": len(acquisitions),
        "unmatched_acquisitions": [
            a["start_index"] for a in unmatched],
        # QUANTITY (A). Says nothing about acquisition; see the module
        # docstring, and see head_analyse.py --trim for quantity (B).
        "onset_seconds": settle["onset_seconds"],
        "settle_seconds": settle["settle_seconds"],
        # None when settle_seconds is a number; otherwise why it is not one.
        # A cell whose settle times are missing is diagnosable; one whose
        # settle times are wrong is not.
        "settle_unmeasured_reason": settle["settle_unmeasured_reason"],
        # Reported for diagnosis; the guard is `steady_rise_db`. A log
        # written before those two parted company has no `steady_rise_db`.
        "keyed_snr_db": settle["keyed_snr_db"],
        "steady_rise_db": settle["steady_rise_db"],
        "settle_tolerance_db": settle["tolerance_db"],
        "settle_by_tolerance_db": settle["settle_by_tolerance_db"],
        "settle_referenced_to_tx": settle["referenced"],
        "keyed_span_seconds": settle["keyed_seconds"],
        "steady_db": settle["steady_db"],
        "floor_db": settle["floor_db"],
        "peak_db": settle["peak_db"],
        "in_head_confidence": None,
        "frames": frames,
        "outcome": frames[0]["outcome"] if frames else "never-seen",
        "decoded_count": sum(f["outcome"] == "decoded" for f in frames),
        "frame_count": len(frames),
        "envelope_db": [round(float(v), 2) for v in envelope],
        "envelope_hop_seconds": ENVELOPE_HOP_SECONDS,
    }
    if onset_index is not None and preamble_offset:
        record["in_head_confidence"] = in_head_confidence(
            name, capture_rx, onset_index, preamble_offset)
    if extra:
        record.update(extra)
    return record


# -- continuous capture ---------------------------------------------------

class ContinuousCapture:
    """Gapless capture of one receive-only transport for a whole run.

    `RadioTransport.send()` clears the receive buffer in its `finally` --
    right for the modem, fatal here, because it would delete the very
    key-up transient this bench measures. The receiving transport is opened
    `receive_only=True`, so `send()` raises rather than keying and the only
    remaining clear is the input-overflow path in `_in_callback`. That one
    bumps `_rx_generation`, and this thread records every generation change
    it sees: a keying spanning one is not a measurement, it is a dropout.

    The transport's own ring holds RX_BUFFER_SECONDS (10 s), so this polls
    far inside that and consumes what it took, copying it into a run-long
    array of its own.
    """

    def __init__(self, transport, poll=CAPTURE_POLL_SECONDS):
        self.transport = transport
        self.poll = poll
        self._chunks = []
        self._base = None            # absolute index of _chunks[0][0]
        self._length = 0
        self._cursor = None
        self._generation = None
        self.generation_changes = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="head-bench-capture")

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5 * self.poll)
        return False

    def _loop(self):
        while not self._stop.is_set():
            self.drain()
            self._stop.wait(self.poll)
        self.drain()

    def drain(self):
        """Pull everything new out of the transport and append it."""
        generation, start, samples = self.transport.read_rx(self._cursor)
        with self._lock:
            if self._generation is None:
                self._generation, self._base = generation, start
            elif generation != self._generation:
                # A gap, not a seam. Keep counting -- the record says the
                # keying is suspect rather than the run being lost.
                self.generation_changes += 1
                self._generation = generation
            if len(samples):
                self._chunks.append(np.asarray(samples, np.float32))
                self._length += len(samples)
            self._cursor = (generation, start + len(samples))
        if len(samples):
            self.transport.consume_rx_through(generation, start + len(samples))

    @property
    def position(self) -> int:
        """Samples captured so far -- the mark a keying is sliced from."""
        with self._lock:
            return self._length

    def slice(self, start: int, stop: int):
        with self._lock:
            if not self._chunks:
                return np.zeros(0, np.float32)
            flat = np.concatenate(self._chunks)
            self._chunks = [flat]
        lo = max(0, int(start))
        hi = min(len(flat), int(stop))
        return flat[lo:hi].copy() if hi > lo else np.zeros(0, np.float32)


# -- the simulated loop (no radios) ---------------------------------------

class SimulatedPair:
    """A loopback stand-in for a `RadioTransport`, for `--simulate`.

    Not a channel model and not a substitute for air time: it exists so the
    driver, the JSONL, the resume logic and the whole measurement chain can
    be exercised end to end -- and so the instrument can be shown to recover
    a settle time that was put in on purpose, before any air time is spent
    on it. The "AGC" is an exponential approach with a known time constant;
    `tests/test_head_bench.py` checks the analyser gets it back.

    It implements exactly the transport surface this bench uses: send(),
    read_rx(), consume_rx_through(), the three counters, and close().
    """

    class _Radio:
        id = "sim"

    def __init__(self, agc_seconds=0.25, noise=0.002, quiet_seconds=1.0,
                 seed=0):
        self.radio = self._Radio()
        self.agc_seconds = agc_seconds
        self.noise = noise
        #: Silence appended either side of a transmission, standing in for
        #: the real bench's idle air. It has to exceed PRE_ROLL_SECONDS or
        #: there is no noise floor for the onset trigger to sit above.
        self.quiet_seconds = quiet_seconds
        self.rng = np.random.default_rng(seed)
        self.receive_only = False
        self.tx_underflows = 0
        self.rx_overflows = 0
        self.rx_generation = 0
        self.peer = None
        self._buf = []
        self._end = 0
        self._start = 0
        self._lock = threading.Lock()

    # -- transmit side
    def send(self, tx_audio, **kwargs):
        rx = np.asarray(tx_audio, dtype=np.float64)[::DECIMATION]
        ramp = 1.0 - np.exp(-np.arange(len(rx)) / (self.agc_seconds * RX_RATE))
        shaped = rx * ramp + self.noise * self.rng.standard_normal(len(rx))
        peer = self.peer if self.peer is not None else self
        peer._quiet(self.quiet_seconds)
        peer._append(shaped)
        peer._quiet(self.quiet_seconds)
        return len(rx) / RX_RATE

    # -- receive side
    def _quiet(self, seconds):
        n = int(seconds * RX_RATE)
        if n > 0:
            self._append(self.noise * self.rng.standard_normal(n))

    def _append(self, samples):
        with self._lock:
            self._buf.append(np.asarray(samples, np.float32))
            self._end += len(samples)

    def read_rx(self, cursor=None):
        with self._lock:
            start = self._start
            if cursor is not None and cursor[0] == self.rx_generation:
                start = min(self._end, max(start, cursor[1]))
            remaining, pieces = start - self._start, []
            for chunk in self._buf:
                if remaining >= len(chunk):
                    remaining -= len(chunk)
                else:
                    pieces.append(chunk[remaining:])
                    remaining = 0
            samples = (np.concatenate(pieces) if pieces
                       else np.zeros(0, np.float32))
            return self.rx_generation, start, samples

    def consume_rx_through(self, generation, end):
        with self._lock:
            if generation != self.rx_generation:
                return
            count = max(0, end - self._start)
            while self._buf and count >= len(self._buf[0]):
                count -= len(self._buf[0])
                self._start += len(self._buf.pop(0))
            if count and self._buf:
                self._buf[0] = self._buf[0][count:]
                self._start += count

    def start_receiving(self):
        pass

    def close(self):
        pass


def simulated_pair(seed=0, **kwargs):
    """Two `SimulatedPair`s wired to each other."""
    a = SimulatedPair(seed=seed, **kwargs)
    b = SimulatedPair(seed=seed + 1, **kwargs)
    a.peer, b.peer = b, a
    return a, b


# -- the driver -----------------------------------------------------------

def keying_audio(name: str, head_seconds: float, payloads):
    """The exact TX buffer for one keying, plus the head it really got.

    The realised head is returned rather than the requested one because each
    family rounds `head_seconds` up its own way -- FSK to whole 4-symbol
    blocks, OFDM to whole symbols -- and the record must state what went on
    the air, not what was asked for.
    """
    mode = headed_mode(name, head_seconds)
    pieces = [np.asarray(mode.encode(p), np.float32) for p in payloads]
    realised = preamble_offset_rx(name, head_seconds) * DECIMATION / TX_RATE
    return np.concatenate(pieces), realised, mode


def run_cell(cell: Cell, sender, listener, capture: "ContinuousCapture",
             run_id: int, log: RunLog, keep_pass_fraction: float, rng,
             gap_scale: float = 1.0) -> dict:
    """One keying: idle the gap, build the scenario's history, key, measure.

    The gap is honoured *before* the keying rather than after, so a resumed
    run's first cell gets the same AGC history it would have had in an
    uninterrupted run -- the whole point of the gap axis is what the
    receiver was doing beforehand.
    """
    name = cell.mode
    mode = mode_by_name(name)
    payloads = [payload_for(run_id, mode, seq) for seq in range(cell.frames)]
    audio, realised_head, headed = keying_audio(name, cell.head_seconds, payloads)

    time.sleep(cell.gap_seconds * gap_scale)

    # `alternate`: put the other mode on the air first, so the receiver has
    # to change spectra and re-level before the keying being measured.
    if cell.alt_mode:
        alt = mode_by_name(cell.alt_mode)
        alt_audio, _, _ = keying_audio(
            cell.alt_mode, cell.head_seconds, [payload_for(run_id, alt, 0)])
        sender.send(alt_audio)
        time.sleep(cell.gap_seconds * gap_scale)

    # `turnaround`: the far end speaks first and we answer within T. Only
    # reachable under --allow-two-way, because it needs the listener keyable.
    if cell.turnaround_seconds is not None:
        peer_audio, _, _ = keying_audio(
            name, cell.head_seconds, [payload_for(run_id, mode, 0)])
        listener.send(peer_audio)
        time.sleep(cell.turnaround_seconds)

    before = {"tx_underflows": sender.tx_underflows,
              "rx_overflows": listener.rx_overflows,
              "rx_generation": listener.rx_generation,
              "capture_generation_changes": capture.generation_changes}
    capture.drain()
    mark = capture.position
    start_mark = max(0, mark - int(PRE_ROLL_SECONDS * RX_RATE))

    keyed = sender.send(audio)
    time.sleep(POST_ROLL_SECONDS * gap_scale)
    capture.drain()
    slice_rx = capture.slice(start_mark, capture.position)

    record = measure_keying(
        cell, name, slice_rx, payloads,
        float(getattr(headed, "confidence_threshold", 0.12)),
        realised_head, tx_audio=audio,
        extra={
            "run_id": run_id,
            "requested_head_seconds": cell.head_seconds,
            "keyed_seconds": None if keyed is None else float(keyed),
            "airtime_seconds": float(headed.airtime(len(payloads[0]))),
            "capture_samples": int(len(slice_rx)),
            "pre_roll_samples": int(mark - start_mark),
            "timestamp": time.time(),
            # Xruns, per keying. A keying where any of these moved is not a
            # head measurement at all; see the module docstring.
            "tx_underflows": int(sender.tx_underflows - before["tx_underflows"]),
            "rx_overflows": int(listener.rx_overflows - before["rx_overflows"]),
            "rx_generation_changed":
                listener.rx_generation != before["rx_generation"],
            "capture_generation_changes":
                capture.generation_changes - before["capture_generation_changes"],
        })

    failed = record["decoded_count"] < record["frame_count"]
    # Every failing capture is kept; a sampled fraction of the passes is kept
    # as controls, because a failure with nothing to compare it against is
    # barely more diagnosable than a bare "failed".
    if failed or rng.random() < keep_pass_fraction:
        record["capture_path"] = log.save_capture(
            cell.key.replace("|", "_"), slice_rx)
    log.append(record)
    return record


def format_progress(index: int, total: int, record: dict) -> str:
    settle = record.get("settle_seconds")
    first = record["frames"][0] if record["frames"] else {}
    # An unmeasured settle prints its reason, not "n/a": the reason is the
    # whole point of refusing the number.
    settle_text = (f"{settle * 1000:.0f}ms" if settle is not None else
                   record.get("settle_unmeasured_reason") or "n/a")
    return (f"[{index}/{total}] {record['scenario']:<10} {record['direction']} "
            f"{record['mode']:<5} head={record['head_seconds']:.3f}s "
            f"gap={record['gap_seconds']:g}s -> "
            f"{record['outcome']:<10} "
            f"{record['decoded_count']}/{record['frame_count']} "
            f"conf={first.get('confidence', 0.0):.3f} "
            f"land={first.get('landing', '?')} settle={settle_text}"
            + (" XRUN" if record["rx_overflows"] or record["tx_underflows"]
               else ""))


def _drive(pending, sender, listener, log, run_id, rng, keep_pass_fraction,
           gap_scale) -> bool:
    """Run the pending cells under one continuous capture. True if interrupted.

    One capture thread for the whole run: a run is one direction (main()
    enforces it), so the listener never changes under us and the capture
    stays gapless across every keying rather than being restarted per cell.
    """
    with ContinuousCapture(listener) as capture:
        for index, cell in enumerate(pending, 1):
            try:
                record = run_cell(cell, sender, listener, capture, run_id, log,
                                  keep_pass_fraction, rng, gap_scale)
            except KeyboardInterrupt:
                print("\ninterrupted -- stopping with the radio down")
                return True
            print(format_progress(index, len(pending), record))
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True,
                    help="JSONL run log; captures go in a directory beside it")
    ap.add_argument("--run-label", default=None,
                    help="names the run and seeds its payloads; defaults to "
                         "the log's filename so a resume derives the same ones")
    ap.add_argument("--scenario", nargs="+", default=None, choices=sorted(PLAN),
                    help="scenarios to run (default: all of them)")
    ap.add_argument("--mode", nargs="+", default=list(DEFAULT_MODES),
                    help="HF modes to sweep")
    ap.add_argument("--head", nargs="+", type=float, default=list(DEFAULT_HEADS),
                    help="settling head lengths in seconds")
    ap.add_argument("--reps", type=int, default=DEFAULT_REPS,
                    help="repetitions per cell")
    ap.add_argument("--direction", default="ab", choices=list(DIRECTIONS),
                    help="ab is a->b, ba is b->a. The 2.7x spread in the two "
                         "measured ramps says these are different "
                         "measurements, so run both -- one invocation each, "
                         "since only one station can be receive-only at once")
    ap.add_argument("--a", default="ic705", help="station A radio id")
    ap.add_argument("--b", default="ic7300", help="station B radio id")
    ap.add_argument("--keep-pass-fraction", type=float,
                    default=KEEP_PASS_FRACTION,
                    help="fraction of passing keyings whose capture is kept "
                         "as a control (failing ones are always kept)")
    ap.add_argument("--allow-two-way", action="store_true",
                    help="permit scenarios where BOTH radios key (turnaround). "
                         "Without it the listening radio is opened "
                         "receive-only and nothing in this process can key it.")
    ap.add_argument("--simulate", action="store_true",
                    help="drive the whole bench against a loopback fake "
                         "transport: no radios, no PTT, no air time. For "
                         "proving the instrument, never for measuring a radio.")
    ap.add_argument("--gap-scale", type=float, default=None,
                    help="multiply every idle gap by this (default 1.0 on "
                         "radios, 0.0 under --simulate, where waiting out a "
                         "30 s AGC gap measures nothing)")
    ap.add_argument("--dry-run", action="store_true",
                    help="expand the matrix, print what would run, key nothing")
    ap.add_argument("--seed", type=int, default=0,
                    help="seeds only the keep-a-capture sampling")
    args = ap.parse_args(argv)

    cells = expand(scenarios=args.scenario, modes=args.mode,
                   directions=(args.direction,), heads=args.head,
                   reps=args.reps)
    if any(c.scenario == "turnaround" for c in cells) and not (
            args.allow_two_way or args.simulate):
        ap.error("the turnaround scenario keys both radios; pass "
                 "--allow-two-way to permit it, or leave it out of --scenario")

    log = RunLog(args.out)
    done = log.completed_keys()
    pending = [c for c in cells if c.key not in done]
    label = args.run_label or Path(args.out).stem
    run_id = run_id_from(label)
    gap_scale = ((0.0 if args.simulate else 1.0)
                 if args.gap_scale is None else args.gap_scale)

    print(f"{len(cells)} cells, {len(done)} already recorded, "
          f"{len(pending)} to run (run id {run_id})")
    if args.dry_run:
        for cell in pending:
            print("  " + cell.key)
        return 0
    if not pending:
        print("nothing to do")
        return 0

    rng = np.random.default_rng(args.seed)
    interrupted = False
    try:
        if args.simulate:
            ta, tb = simulated_pair(seed=args.seed)
            sender, listener = (ta, tb) if args.direction == "ab" else (tb, ta)
            interrupted = _drive(pending, sender, listener, log, run_id, rng,
                                 args.keep_pass_fraction, gap_scale)
        else:
            # The listening station is opened receive-only unless the run
            # explicitly needs it to answer. Nothing in this process can then
            # key it -- a property of the transport rather than of this
            # script remembering which one is which.
            two_way = args.allow_two_way
            with bench.radio_pair(
                    args.a, args.b, warmup=3.0,
                    a_receive_only=not two_way and args.direction == "ba",
                    b_receive_only=not two_way and args.direction == "ab",
            ) as (ta, tb):
                sender, listener = (ta, tb) if args.direction == "ab" else (tb, ta)
                interrupted = _drive(pending, sender, listener, log, run_id,
                                     rng, args.keep_pass_fraction, gap_scale)
    except KeyboardInterrupt:
        # radio_pair()'s finally has already closed both transports, and
        # closing un-keys. Everything recorded so far is on disk: the log is
        # flushed per record precisely so this path loses nothing.
        interrupted = True
        print("\ninterrupted -- radios closed, log written so far")
    finally:
        log.close()

    print(f"wrote {log.path}")
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
