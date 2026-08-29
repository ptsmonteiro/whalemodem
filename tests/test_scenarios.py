import json

import numpy as np
import pytest

from whale.channel import SnrSpec
from whale.fm_channel import FM_RADIO_PRESETS
from whale.scenario import (FM_SCENARIO_PRESETS, HF_SSB_SCENARIO_PRESETS,
                            FmScenario, HfSsbScenario, VhfFmScenario)


def test_hf_presets_build_all_stages_in_physical_order_and_expand_description():
    scenario = HfSsbScenario.from_preset(
        "moderate", sample_rate=48_000, snr=SnrSpec(8.0), seed=1234)
    description = scenario.describe()
    stages = description["stages"]
    assert [stage["type"] for stage in stages] == [
        "filter", "clipping", "frequency_offset", "watterson",
        "narrowband_interference", "awgn", "filter"]
    assert stages[3]["preset"] == "mid_latitude_moderate"
    assert stages[5]["snr"]["db"] == 8.0
    assert stages[1]["limit"] == 0.85
    json.dumps(description, allow_nan=False)


@pytest.mark.parametrize("name", ["quiet", "moderate", "disturbed"])
def test_hf_presets_are_seeded_replayable(name):
    scenario = HfSsbScenario.from_preset(
        name, sample_rate=8_000, snr=SnrSpec(20), seed=17)
    audio = np.sin(2 * np.pi * 1_000 * np.arange(2_000) / 8_000)
    assert np.array_equal(scenario.build().process(audio).audio,
                          scenario.build().process(audio).audio)


def test_fm_scenarios_expand_project_provenance_and_keep_bench_presets_separate():
    before = set(FM_RADIO_PRESETS)
    scenario = FmScenario.from_preset("disturbed", sample_rate=48_000, seed=9)
    description = scenario.describe()
    assert description["is_propagation_standard"] is False
    assert description["profile_authority"] == "whalemodem_project_simulation"
    fm = description["stages"][2]
    assert fm["preset"] == "vhf_bench_conservative"
    assert fm["carrier_to_noise_db"] == 8.0
    assert len(fm["rf_paths"]) == 2
    assert set(FM_RADIO_PRESETS) == before
    assert VhfFmScenario is FmScenario


def test_all_scenario_names_and_overrides_are_available():
    assert set(HF_SSB_SCENARIO_PRESETS) == {"quiet", "moderate", "disturbed"}
    assert set(FM_SCENARIO_PRESETS) == {"quiet", "moderate", "disturbed"}
    fm = FmScenario.from_preset(
        "quiet", sample_rate=48_000, seed=1, carrier_to_noise_db=22,
        radio_preset="ic705_to_kg_uv9d")
    stage = fm.describe()["stages"][2]
    assert stage["carrier_to_noise_db"] == 22
    assert stage["preset"] == "ic705_to_kg_uv9d"


def test_unknown_scenario_names_are_actionable():
    with pytest.raises(ValueError, match="unknown HF SSB scenario preset"):
        HfSsbScenario.from_preset(
            "good", sample_rate=48_000, snr=SnrSpec(10), seed=1)
    with pytest.raises(ValueError, match="unknown FM scenario preset"):
        FmScenario.from_preset("urban", sample_rate=48_000, seed=1)
