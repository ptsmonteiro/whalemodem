"""All shipped analog-FM modes use one configured settling head."""

from whale import framing
from whale.mode_qualification import registry


def test_every_fm_mode_uses_the_shared_settling_head():
    for mode in registry("fm", "experimental").modes:
        configured = getattr(mode, "lead_in_seconds",
                             getattr(mode, "head_seconds", None))
        assert configured == framing.FM_SETTLING_HEAD_SECONDS, mode.name
