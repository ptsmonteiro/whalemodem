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
    # VF13 is the 1,438.8 bit/s combinatorial MFSK rung below VF16 and VF12.
    QualificationEntry("fm", 19, QualificationLevel.DEFAULT),
    # VF14-16 (mode 20) was the original control mode. Superseded as control
    # and dropped from the default ladder by VF14-4 (mode 23, below);
    # demoted to experimental rather than removed so its waveform stays
    # available. Old peers that only know mode 20 no longer interoperate --
    # see mode_qualification.py's registry() control assignment.
    QualificationEntry("fm", 20, QualificationLevel.EXPERIMENTAL),
    # VF14-4 is the FM control mode and the lowest (most robust) rung of the
    # default ladder: the 600-baud 4-FSK DATA rung, mode_id 23.
    QualificationEntry("fm", 23, QualificationLevel.DEFAULT),
    QualificationEntry("fm", 21, QualificationLevel.EXPERIMENTAL),
    # VF16 is VF12's 8-PSK, rate-2/3 LDPC sibling. The conservative FM C/N
    # simulator delivered 50/50 at +5 dB where VF12 delivered 0/50; the
    # same waveform passed 10/10 in each radio direction at drive 1.
    QualificationEntry("fm", 22, QualificationLevel.DEFAULT),
    # VF12 is the HF7-geometry FM data rung: 51-carrier 50 Hz OFDM, 16-QAM,
    # rate-3/4 LDPC with comb-pilot channel tracking, 4,690 bit/s. Installed
    # as DEFAULT on 2026-09-13 by owner decision, on two-direction hardware
    # runs against the IC-705 <-> HT FM path (logs/vf12_edgefix): 20/20
    # exact-payload frames, 10 each direction, at ~15 dB measured effective
    # SNR. The rungs above it were measured and rejected on the same path --
    # 32-QAM delivers 0/16 there -- so this is the top FM rate that holds.
    # Default is availability, not qualification.
    QualificationEntry("fm", 18, QualificationLevel.DEFAULT),
    QualificationEntry("hf", 5, QualificationLevel.DEFAULT),
    # Owner approved the 32-FSK HR0 replacement on 2026-09-06 despite its
    # unproven 3 dB measured margin; Default is availability, not qualification.
    QualificationEntry("hf", 10, QualificationLevel.DEFAULT),
    QualificationEntry("hf", 7, QualificationLevel.EXPERIMENTAL),
    QualificationEntry("hf", 12, QualificationLevel.EXPERIMENTAL),
    QualificationEntry("hf", 13, QualificationLevel.EXPERIMENTAL),
    # HF7 is the maximum-speed HF data rung, installed as DEFAULT on
    # 2026-09-07 by owner decision on the evidence in
    # experiments/hf18_ofdm49_vara/RESULTS.md (100 trials per arm,
    # interleaved against HF6's geometry: 94/100 delivered, Fisher p = 0.75
    # on the delivery difference). Unlike HF2, it is installed *inside* the
    # 2,300 Hz occupied-bandwidth ceiling -- 2,253 Hz measured -- but its
    # Level-4 operating-envelope evidence has not been run. Default is
    # availability, not qualification.
    QualificationEntry("hf", 14, QualificationLevel.DEFAULT),
    # HF8 is HF7's carrier plan and guard at 8PSK with rate-2/3 LDPC, double
    # the pilot density and a 5.940 s frame: 3,292 bit/s against HF7's 6,504,
    # bought for an 8 dB lower simulated AWGN floor (12 dB vs 20) and the only
    # measured fading envelope on this PHY family -- 90% delivery on quiet
    # Watterson from 16 dB, where HF7 manages 7/40 at 24 dB.
    #
    # Installed as DEFAULT on 2026-09-07 by owner decision, on two-direction
    # hardware drive sweeps (logs/mode_qualification/hf/hf19/): HF8's
    # breakpoint is 12 dB below HF7's on IC-7300 -> IC-705, and on the weaker
    # non-clipping IC-705 -> IC-7300 path HF7 delivers 0/10 at every drive
    # level while HF8 delivers 10/10 with 3 dB to spare. Those runs establish
    # the margin claim on radios; the FADING envelope that motivates the mode
    # is still simulation only. Default is availability, not qualification.
    QualificationEntry("hf", 15, QualificationLevel.DEFAULT),
    QualificationEntry("hf", 16, QualificationLevel.DEFAULT),
    QualificationEntry("hf", 17, QualificationLevel.EXPERIMENTAL),
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
    if policy == "fm":
        from .modes.vf12 import VF12
        from .modes.vf13 import VF13
        from .modes.vf14 import VF14_16, VF14_4, VF14_8
        from .modes.vf16 import VF16
        # VF14-4 is both the control mode and the lowest (most robust) rung
        # of the default ladder. `budget` is accepted for interface
        # symmetry with hf_registry but is unused: every FM waveform here is
        # fixed-geometry, none derives its payload from a keying budget.
        del budget
        candidates, control = (VF14_16, VF14_8, VF14_4, VF13, VF16, VF12), VF14_4
    elif policy == "hf":
        from .modes.hr0_mode import HR0
        from .modes.hc0_mode import HC0
        from .modes.hc1w_mode import HC1W
        from .modes.hf5_mode import HF5
        from .modes.hf6_mode import HF6
        from .modes.hf7_mode import HF7
        from .modes.hf8_mode import HF8
        from .modes.hf9_mode import HF9
        # Rate order, which is the order _maybe_adapt climbs.
        candidates, control = (HR0, HC0, HF9, HC1W, HF8, HF5, HF6, HF7), HR0
        # HF2 remains available only at experimental level as a historical
        # fallback.
        if requested >= QualificationLevel.EXPERIMENTAL:
            from .modes.hf2_mode import HF2
            candidates += (HF2,)
    else:
        raise ValueError(f"unknown channel policy {policy!r}")

    selected = tuple(mode for mode in candidates
                     if qualification_level(policy, mode.mode_id) <= requested)
    if policy == "fm":
        from .framing import AIR_HEADER_BYTES
        selected = tuple(sorted(selected, key=lambda mode:
            8 * mode.chunk_size / mode.airtime(AIR_HEADER_BYTES + mode.chunk_size)))
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
