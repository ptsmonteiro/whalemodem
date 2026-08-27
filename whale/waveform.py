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
    sample_rate: int

    def encode(self, payload: bytes) -> np.ndarray: ...

    def decode(self, audio: np.ndarray) -> dict: ...

    def airtime(self, payload_len: int) -> float: ...


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
