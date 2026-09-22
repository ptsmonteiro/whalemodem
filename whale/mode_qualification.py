"""Declarative qualification state and policy registry filtering.

Qualification is a property of a mode *on a channel policy*.  The same
on-air mode may therefore have a different disposition on another policy.
Registry levels are cumulative: an optional registry contains default and
optional modes, while an experimental registry contains every declared mode.
"""

from __future__ import annotations

import pkgutil
from dataclasses import dataclass
from enum import IntEnum
from importlib import import_module

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
    # see whale/modes/vf14.py's LADDER, which marks VF14_4 control instead.
    QualificationEntry("fm", 20, QualificationLevel.EXPERIMENTAL),
    # VF14-4 is the FM control mode and the lowest (most robust) rung of the
    # default ladder: the 600-baud 4-FSK DATA rung, mode_id 23.
    QualificationEntry("fm", 23, QualificationLevel.DEFAULT),
    QualificationEntry("fm", 21, QualificationLevel.EXPERIMENTAL),
    # VF16 is VF12's 8-PSK, rate-2/3 LDPC sibling. The conservative FM C/N
    # simulator delivered 50/50 at +5 dB where VF12 delivered 0/50; the
    # same waveform passed 10/10 in each radio direction.
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
    # FMHT0 is rung 0 of the FM-handheld ladder: 400 Bd 4-FSK, rate 1/3,
    # no equalizer -- the beacon/link-setup/ACK waveform. Experimental
    # pending an on-air margin measurement.
    QualificationEntry("fm", 30, QualificationLevel.EXPERIMENTAL),
    # VFS2 is the workhorse rung of the FM SC-FDE family: coherent QPSK,
    # rate-1/2 LDPC, 1,527 bit/s. Experimental pending an on-air margin.
    QualificationEntry("fm", 25, QualificationLevel.EXPERIMENTAL),
    # VFS3 is the high-rate coherent rung of the FM SC-FDE family: VFS2's
    # waveform at rate-3/4 LDPC, 2,303 bit/s -- 1.5x VFS2's payload in
    # exactly the same airtime, for a full-quieting signal. Experimental
    # pending an on-air margin measurement.
    QualificationEntry("fm", 26, QualificationLevel.EXPERIMENTAL),
    # VFS1 is the robust rung of the FM SC-FDE family: differential BPSK,
    # rate-1/2 LDPC, 748 bit/s. Differential detection costs ~2.3 dB against
    # the coherent rungs above and buys tolerance of the phase disturbance a
    # pocketed or moving handheld produces. Experimental pending an on-air
    # margin measurement.
    QualificationEntry("fm", 24, QualificationLevel.EXPERIMENTAL),
    # FMHT4 is rung 4, the top of the FM-handheld ladder and the only
    # OFDM rung in it: 64-carrier 31.25 Hz 8PSK OFDM, rate-3/4 LDPC,
    # clip-and-filter peak limiting, 3,151 bit/s. Experimental pending
    # an on-air margin measurement.
    QualificationEntry("fm", 27, QualificationLevel.EXPERIMENTAL),
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


def _discover_ladders() -> dict[str, tuple["LadderEntry", ...]]:
    """Every mode's declared `LADDER` entries, grouped by policy.

    A mode is declared by adding it to `MANIFEST` above (its qualification
    evidence) *and* a module-level `LADDER` tuple next to the mode instance
    itself (its ladder position and, for the one mode per policy that is
    it, `control=True`) -- see `whale.waveform.LadderEntry`. This function
    is the only place that walks `whale.modes` to find those declarations,
    so registering a new mode never touches this file.

    Walks module names in sorted order for determinism and imports each
    one; a broken mode module's ImportError propagates rather than being
    swallowed, so a bad mode fails loudly here, at import time, not
    silently later when some registry() call happens to need it.
    """
    from . import modes as modes_pkg

    ladders: dict[str, list["LadderEntry"]] = {}
    names = sorted(info.name for info in pkgutil.iter_modules(modes_pkg.__path__))
    for name in names:
        module = import_module(f"{modes_pkg.__name__}.{name}")
        for entry in getattr(module, "LADDER", ()):
            ladders.setdefault(entry.policy, []).append(entry)
    return {policy: tuple(entries) for policy, entries in ladders.items()}


def registry(policy: str, level: QualificationLevel | str =
             QualificationLevel.DEFAULT, budget=None) -> ModeRegistry:
    """Return the cumulative registry available at ``level`` for ``policy``.

    `budget` is accepted and ignored: every mode on either ladder is
    fixed-geometry, none derives its payload from a keying-time budget.
    """
    del budget
    requested = QualificationLevel.parse(level)
    ladders = _discover_ladders()
    if policy not in ladders:
        raise ValueError(f"unknown channel policy {policy!r}")
    entries = sorted(ladders[policy], key=lambda entry: entry.rank)
    controls = [entry for entry in entries if entry.control]
    if len(controls) != 1:
        raise ValueError(
            f"{policy!r} ladder must declare exactly one control mode, "
            f"found {len(controls)}")
    control = controls[0].mode

    selected = tuple(entry.mode for entry in entries
                     if qualification_level(policy, entry.mode.mode_id) <= requested)
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

    # A mode is declared in two places: MANIFEST above (its qualification
    # evidence) and a `LADDER` entry on its own module (its ladder
    # position). Both must agree on exactly the same set of (policy,
    # mode_id) pairs -- a mode declared in one but not the other is a
    # wiring mistake, and this is the only place that would ever notice.
    ladders = _discover_ladders()
    discovered_keys = [(entry.policy, entry.mode.mode_id)
                       for entries in ladders.values() for entry in entries]
    if len(discovered_keys) != len(set(discovered_keys)):
        raise ValueError("waveform ladder declares a mode more than once")
    manifest_keys, discovered_keys = set(keys), set(discovered_keys)
    if manifest_keys != discovered_keys:
        raise ValueError(
            "qualification manifest and waveform ladder disagree -- "
            f"declared only in MANIFEST: {sorted(manifest_keys - discovered_keys)!r}; "
            f"declared only in a LADDER: {sorted(discovered_keys - manifest_keys)!r}")

    for policy, entries in ladders.items():
        controls = [entry for entry in entries if entry.control]
        if len(controls) != 1:
            raise ValueError(
                f"{policy!r} ladder must declare exactly one control mode, "
                f"found {len(controls)}")


validate_manifest()
