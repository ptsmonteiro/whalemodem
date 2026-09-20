"""Stable contracts between the link protocol and physical-layer modes."""

from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence, runtime_checkable

import numpy as np


@runtime_checkable
class WaveformMode(Protocol):
    """One negotiable physical-layer capability.

    Implementations own modulation, synchronization, framing/coding, timing,
    and decode diagnostics.  The link layer only deals in packets and these
    operations; it does not need to know which waveform carries them.
    """

    name: str
    mode_id: int
    chunk_size: int
    confidence_threshold: float
    # Lowest to highest carrier/tone centre frequency, in Hz.  Not the
    # occupied bandwidth: that is a measurement and lives in docs/MODES.md.
    band_hz: tuple[float, float]
    # The two facts that cannot be derived from code, stated once here in the
    # wording docs/MODES.md uses: "49-carrier 32-QAM OFDM", "QC-LDPC 3/4".
    modulation: str
    fec: str
    # Encoded arrays and receive-buffer indices deliberately use different
    # clocks: radio I/O/TX stays at 48 kHz while the shared RX front end
    # supplies every decoder at 12 kHz.
    tx_sample_rate: int
    rx_sample_rate: int

    def encode(self, payload: bytes) -> np.ndarray: ...

    def decode(self, audio: np.ndarray) -> dict: ...

    def airtime(self, payload_len: int) -> float: ...

    def describe(self) -> str: ...


@dataclass(frozen=True)
class ModeRegistry:
    """Ordered negotiable modes plus the robust control-plane mode."""

    modes: Sequence[WaveformMode]
    control: WaveformMode

    def __post_init__(self):
        modes = tuple(self.modes)
        if not modes:
            raise ValueError("at least one waveform mode is required")
        by_id = {mode.mode_id: mode for mode in modes}
        if len(by_id) != len(modes):
            raise ValueError("waveform mode IDs must be unique")
        if self.control.mode_id not in by_id:
            raise ValueError("control mode must be present in modes")
        object.__setattr__(self, "modes", modes)
        object.__setattr__(self, "by_id", by_id)

    by_id: Mapping[int, WaveformMode] = field(init=False, repr=False)

    @property
    def supported_ids(self):
        return tuple(mode.mode_id for mode in self.modes)

    def resolve(self, mode_id: int) -> WaveformMode:
        return self.by_id.get(mode_id, self.control)

    def step(self, current: WaveformMode, direction: int):
        try:
            index = next(i for i, mode in enumerate(self.modes)
                         if mode.mode_id == current.mode_id)
        except StopIteration:
            return None
        new_index = index + direction
        if not 0 <= new_index < len(self.modes):
            return None
        return self.modes[new_index]


def _policy_for(mode_id: int) -> str:
    """The channel policy a mode is declared on, or "?" if undeclared.

    Imported lazily: whale.mode_qualification imports ModeRegistry from here.
    """
    from .mode_qualification import MANIFEST
    for entry in MANIFEST:
        if entry.mode_id == mode_id:
            return entry.policy
    return "?"


def format_mode(mode: WaveformMode) -> str:
    """The one-line description every mode renders through.

    Built here rather than per mode so the 13 modes cannot drift into 13
    formats, which is what happened to the ad-hoc describe() functions this
    replaces.
    """
    from .framing import AIR_HEADER_BYTES
    # Derived here, from the same airtime() and chunk_size the encoder uses,
    # so the figures cannot disagree with the waveform that produced them.
    frame_seconds = mode.airtime(AIR_HEADER_BYTES + mode.chunk_size)
    net_bps = 8 * mode.chunk_size / frame_seconds
    lo, hi = mode.band_hz
    band = f"{lo:.0f} Hz" if lo == hi else f"{lo:.0f}-{hi:.0f} Hz"
    return (f"{mode.name} ({mode.mode_id}) {_policy_for(mode.mode_id)}  "
            f"{band}  {mode.modulation}  {mode.fec}  "
            f"{mode.chunk_size} B/{frame_seconds:.3f} s = {net_bps:.0f} bit/s")


class ModeDescription:
    """Mixin giving every mode the one shared `describe()`.

    Deliberately holds nothing else: vf13 and vf14 already define their own
    `frame_seconds`, so anything named here risks shadowing a mode's own.
    """

    def describe(self) -> str:
        return format_mode(self)
