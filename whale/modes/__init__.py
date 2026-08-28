"""Physical-layer modes beyond the baseline CPFSK profiles in whale/afsk.py.

Each module here supplies a `WaveformMode` (see whale/waveform.py) that the
link layer can negotiate exactly like an `afsk.Profile`: the link deals in
packets, `encode`/`decode`/`airtime`, `chunk_size` and `mode_id`, and does
not know which modulation is underneath.
"""


def default_registry():
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
    """
    from .vf3_mode import registry_with_vf3

    return registry_with_vf3()
