"""Physical-layer modes beyond the baseline CPFSK profiles in whale/afsk.py.

Each module here supplies a `WaveformMode` (see whale/waveform.py) that the
link layer can negotiate exactly like an `afsk.Profile`: the link deals in
packets, `encode`/`decode`/`airtime`, `chunk_size` and `mode_id`, and does
not know which modulation is underneath.
"""


def default_registry(budget=None):
    """The station's negotiable mode ladder: the CPFSK profiles, then VF3.

    This -- not `afsk.default_registry()` -- is what a Link uses when it is
    not handed a registry.  `afsk.default_registry()` remains the CPFSK-only
    ladder, which is what tests that care about the two-tone profiles in
    isolation want; composing the two here rather than in `whale/afsk.py`
    keeps the CPFSK module unaware of the waveforms stacked on top of it.

    VF3 sits at the top because it is the fastest and least robust rung: the
    ladder is ordered by rate, and `_maybe_adapt` climbs it only after a
    clean streak and steps down after silence, so a link that cannot hold
    VF3 falls back to 1200 baud on its own.  Negotiation is per-station --
    a peer that does not advertise mode 3 simply never has it selected.

    `budget` is the useful-frame budget in seconds (see
    whale/policy.py's `max_useful_frame_seconds`), which sizes the CPFSK
    rungs' chunks; None means afsk's own default. VF3 is unaffected -- its
    payload is fixed by its OFDM frame structure, not by a time budget.
    """
    from ..mode_qualification import registry
    return registry("vhf-fm", "default", budget)


def optional_registry(budget=None):
    """VHF modes available after explicit operator opt-in."""
    from ..mode_qualification import registry
    return registry("vhf-fm", "optional", budget)


def experimental_registry(budget=None):
    """All declared VHF modes, for explicit development and qualification."""
    from ..mode_qualification import registry
    return registry("vhf-fm", "experimental", budget)


def hf_registry(budget=None):
    """The HF SSB ladder: HR0 control, then HC0 and HC1.

    HR0 is the maximum-margin 128-FSK Level-0 control mode. HC0 and HC1 are
    retained above it for progressively shorter keyings on supporting paths.

    Separate from `default_registry` rather than an extension of it: the
    CPFSK profiles have no carrier-frequency estimate anywhere in them, so
    on SSB they are not a robust rung to fall back to.

    `budget` is accepted and ignored, so this is interchangeable with
    `default_registry` where a `ChannelPolicy` selects one -- both HF rungs
    have a payload fixed by their frame structure rather than by a
    keying-time budget, in exactly the way VF3's is.
    """
    from ..mode_qualification import registry
    return registry("hf-ssb", "default", budget)


def hf_optional_registry(budget=None):
    from ..mode_qualification import registry
    return registry("hf-ssb", "optional", budget)


def hf_experimental_registry(budget=None):
    from ..mode_qualification import registry
    return registry("hf-ssb", "experimental", budget)
