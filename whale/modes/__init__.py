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
    from .. import afsk
    from .vf3_mode import registry_with_vf3

    base = None if budget is None else afsk.default_registry(budget)
    return registry_with_vf3(base)


def hf_registry(budget=None):
    """The station's mode ladder for an HF SSB channel: HC0, then HC1.

    HC0 (`whale/modes/hc0.py`) is the control mode and the bottom rung -- a
    non-coherent 16-FSK frame that decodes 19.5 dB further into the noise
    than the OFDM rung above it.  HC1 is that rung, five times faster on a
    path that can carry it.

    Separate from `default_registry` rather than an extension of it: the
    CPFSK profiles have no carrier-frequency estimate anywhere in them, so
    on SSB they are not a robust rung to fall back to.

    `budget` is accepted and ignored, so this is interchangeable with
    `default_registry` where a `ChannelPolicy` selects one -- both HF rungs
    have a payload fixed by their frame structure rather than by a
    keying-time budget, in exactly the way VF3's is.
    """
    del budget
    from .hc0_mode import hf_registry as _hf_registry

    return _hf_registry()
