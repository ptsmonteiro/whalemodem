"""Declarative qualification state and policy registry filtering.

Qualification is a property of a mode *on a channel policy*.  The same
on-air mode may therefore have a different disposition on another policy.
Registry levels are cumulative: an optional registry contains default and
optional modes, while an experimental registry contains every declared mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .waveform import ModeRegistry


class QualificationLevel(IntEnum):
    DEFAULT = 0
    OPTIONAL = 1
    EXPERIMENTAL = 2

    @classmethod
    def parse(cls, value: "QualificationLevel | str") -> "QualificationLevel":
        if isinstance(value, cls):
            return value
        try:
            return cls[str(value).upper()]
        except KeyError:
            raise ValueError(
                f"unknown qualification level {value!r}; "
                f"have {[level.name.lower() for level in cls]}") from None


@dataclass(frozen=True)
class QualificationEntry:
    policy: str
    mode_id: int
    level: QualificationLevel


# These dispositions preserve the historically shipped ladders.  They are
# explicitly provisional in MODE_QUALIFICATION.md; the manifest describes
# product availability, not a claim that every evidence gate has passed.
MANIFEST = (
    QualificationEntry("vhf-fm", 0, QualificationLevel.DEFAULT),
    QualificationEntry("vhf-fm", 1, QualificationLevel.DEFAULT),
    QualificationEntry("vhf-fm", 2, QualificationLevel.DEFAULT),
    QualificationEntry("vhf-fm", 3, QualificationLevel.DEFAULT),
    QualificationEntry("vhf-fm", 8, QualificationLevel.EXPERIMENTAL),
    QualificationEntry("vhf-fm", 6, QualificationLevel.EXPERIMENTAL),
    QualificationEntry("hf-ssb", 5, QualificationLevel.DEFAULT),
    QualificationEntry("hf-ssb", 4, QualificationLevel.DEFAULT),
    QualificationEntry("hf-ssb", 10, QualificationLevel.DEFAULT),
    QualificationEntry("hf-ssb", 7, QualificationLevel.EXPERIMENTAL),
    QualificationEntry("hf-ssb", 9, QualificationLevel.EXPERIMENTAL),
)


def qualification_level(policy: str, mode_id: int) -> QualificationLevel:
    matches = [entry.level for entry in MANIFEST
               if entry.policy == policy and entry.mode_id == mode_id]
    if len(matches) != 1:
        raise ValueError(
            f"expected one qualification entry for {(policy, mode_id)!r}, "
            f"found {len(matches)}")
    return matches[0]


def registry(policy: str, level: QualificationLevel | str =
             QualificationLevel.DEFAULT, budget=None) -> ModeRegistry:
    """Return the cumulative registry available at ``level`` for ``policy``."""
    requested = QualificationLevel.parse(level)
    if policy == "vhf-fm":
        from . import afsk
        from .modes.vf3_mode import VF3
        from .modes.vf4_mode import VF4
        from .modes.vf6_mode import VF6
        base = afsk.default_registry() if budget is None else afsk.default_registry(budget)
        candidates, control = tuple(base.modes) + (VF3, VF4, VF6), base.control
    elif policy == "hf-ssb":
        from .modes.hr0_mode import HR0
        from .modes.hc0_mode import HC0
        from .modes.hc1_mode import HC1
        candidates, control = (HR0, HC0, HC1), HR0
        # Experiment-backed adapters are optional development dependencies.
        # A normal/default station must not import them merely to construct
        # the production HF ladder.
        if requested >= QualificationLevel.EXPERIMENTAL:
            from .modes.hf2_mode import HF2
            from .modes.hf3_mode import HF3
            candidates += (HF2, HF3)
    else:
        raise ValueError(f"unknown channel policy {policy!r}")

    selected = tuple(mode for mode in candidates
                     if qualification_level(policy, mode.mode_id) <= requested)
    if control.mode_id not in {mode.mode_id for mode in selected}:
        raise ValueError(
            f"{policy} control mode {control.mode_id} is unavailable at "
            f"{requested.name.lower()} level")
    return ModeRegistry(selected, control)


def validate_manifest() -> None:
    keys = [(entry.policy, entry.mode_id) for entry in MANIFEST]
    if len(keys) != len(set(keys)):
        raise ValueError("qualification manifest contains duplicate policy/mode entries")
    mode_policies: dict[int, set[str]] = {}
    for entry in MANIFEST:
        mode_policies.setdefault(entry.mode_id, set()).add(entry.policy)
    # Mode IDs are global protocol identifiers. Repeating one across policies
    # is allowed only when it denotes the same mode; no current mode does so.
    if any(len(policies) > 1 for policies in mode_policies.values()):
        raise ValueError("qualification manifest reuses a mode ID across policies")


validate_manifest()
