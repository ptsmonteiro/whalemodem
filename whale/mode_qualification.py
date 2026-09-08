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
    # Owner approved the 32-FSK HR0 replacement on 2026-09-06 despite its
    # unproven 3 dB measured margin; Default is availability, not qualification.
    QualificationEntry("hf-ssb", 10, QualificationLevel.DEFAULT),
    QualificationEntry("hf-ssb", 7, QualificationLevel.EXPERIMENTAL),
    QualificationEntry("hf-ssb", 12, QualificationLevel.EXPERIMENTAL),
    QualificationEntry("hf-ssb", 13, QualificationLevel.EXPERIMENTAL),
    # HF7 is the maximum-speed HF data rung, installed as DEFAULT on
    # 2026-09-07 by owner decision on the evidence in
    # experiments/hf18_ofdm49_vara/RESULTS.md (100 trials per arm,
    # interleaved against HF6's geometry: 94/100 delivered, Fisher p = 0.75
    # on the delivery difference). Unlike HF2, it is installed *inside* the
    # 2,300 Hz occupied-bandwidth ceiling -- 2,253 Hz measured -- but its
    # Level-4 operating-envelope evidence has not been run. Default is
    # availability, not qualification.
    QualificationEntry("hf-ssb", 14, QualificationLevel.DEFAULT),
    # HF8 is HF7's carrier plan and guard at 8PSK with rate-2/3 LDPC, double
    # the pilot density and a 0.616 s frame: 3,299 bit/s against HF7's 7,805,
    # bought for an 8 dB lower simulated AWGN floor (12 dB vs 20) and the only
    # measured fading envelope on this PHY family -- 90% delivery on quiet
    # Watterson from 16 dB, where HF7 manages 7/40 at 24 dB.
    #
    # Installed as DEFAULT on 2026-09-07 by owner decision, on two-direction
    # hardware drive sweeps (logs/mode_qualification/hf-ssb/hf19/): HF8's
    # breakpoint is 12 dB below HF7's on IC-7300 -> IC-705, and on the weaker
    # non-clipping IC-705 -> IC-7300 path HF7 delivers 0/10 at every drive
    # level while HF8 delivers 10/10 with 3 dB to spare. Those runs establish
    # the margin claim on radios; the FADING envelope that motivates the mode
    # is still simulation only. Default is availability, not qualification.
    QualificationEntry("hf-ssb", 15, QualificationLevel.DEFAULT),
    QualificationEntry("hf-ssb", 16, QualificationLevel.DEFAULT),
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
        from .modes.hc1w_mode import HC1W
        from .modes.hf5_mode import HF5
        from .modes.hf6_mode import HF6
        from .modes.hf7_mode import HF7
        from .modes.hf8_mode import HF8
        # Rate order, which is the order _maybe_adapt climbs.
        candidates, control = (HR0, HC0, HC1W, HF8, HF5, HF6, HF7), HR0
        # HF2 remains available only at experimental level as a historical
        # fallback.
        if requested >= QualificationLevel.EXPERIMENTAL:
            from .modes.hf2_mode import HF2
            candidates += (HF2,)
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
