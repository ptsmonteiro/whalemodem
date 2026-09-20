"""describe() is uniform across modes and agrees with docs/MODES.md.

The table's numeric columns are derived from the same airtime()/chunk_size
the encoder uses, so a waveform change that is not documented fails here.
"""

import re
from pathlib import Path

import pytest

from whale import waveform
from whale.framing import AIR_HEADER_BYTES
from whale.mode_qualification import registry, QualificationLevel

MODES_MD = Path(__file__).resolve().parent.parent / "docs" / "MODES.md"


def all_modes():
    modes = {}
    for policy in ("fm", "hf"):
        for mode in registry(policy, QualificationLevel.EXPERIMENTAL).modes:
            modes[mode.name] = mode
    return modes


def documented_rows():
    """Every mode row in docs/MODES.md, keyed by mode name."""
    rows = {}
    for line in MODES_MD.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] in ("Mode", ""):
            continue
        rows[cells[0]] = cells
    return rows


@pytest.mark.parametrize("name", sorted(all_modes()))
def test_describe_is_the_shared_format(name):
    mode = all_modes()[name]
    assert mode.describe() == waveform.format_mode(mode)
    assert mode.describe().startswith(f"{name} ({mode.mode_id}) ")


@pytest.mark.parametrize("name", sorted(all_modes()))
def test_band_is_lowest_to_highest_carrier_centre(name):
    lo, hi = all_modes()[name].band_hz
    assert 0 < lo <= hi < 6000


@pytest.mark.parametrize("name", sorted(documented_rows()))
def test_documented_figures_match_the_waveform(name):
    """The span, payload, duration and rate columns are computed, not typed."""
    modes = all_modes()
    assert name in modes, f"docs/MODES.md documents unknown mode {name!r}"
    mode = modes[name]
    span, payload, duration, net = (documented_rows()[name][i]
                                    for i in (1, 6, 7, 8))

    lo, hi = mode.band_hz
    doc_lo, doc_hi = (float(v.replace(",", ""))
                      for v in re.match(r"([\d,.]+)-([\d,.]+) Hz", span).groups())
    assert (round(lo, 3), round(hi, 3)) == (doc_lo, doc_hi)

    frame_seconds = mode.airtime(AIR_HEADER_BYTES + mode.chunk_size)
    assert int(payload.rstrip(" B").replace(",", "")) == mode.chunk_size
    assert float(duration.rstrip(" s")) == pytest.approx(frame_seconds, abs=5e-4)
    documented_net = float(net.rstrip(" bit/s").replace(",", ""))
    assert documented_net == pytest.approx(
        8 * mode.chunk_size / frame_seconds, rel=2e-4, abs=0.5)
