"""Reproducible, expanded channel scenarios built from individual stages.

Scenario preset names describe project test recipes.  They deliberately do
not replace the standards-derived Watterson presets or the measured FM-radio
presets used by their component channels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .channel import (AwgnChannel, ChannelChain, ClippingChannel,
                      FilterChannel, FrequencyOffsetChannel,
                      NarrowbandInterference,
                      NarrowbandInterferenceChannel, SnrKind, SnrSpec,
                      WattersonChannel)
from .fm_channel import ComplexFmChannel, FM_RADIO_PRESETS, FmRfPath


@dataclass(frozen=True)
class HfSsbScenarioPreset:
    """One complete HF SSB simulation recipe."""

    name: str
    watterson_preset: str
    tx_band_hz: tuple[float, float]
    rx_band_hz: tuple[float, float]
    clip_limit: float
    frequency_offset_hz: float
    drift_hz_per_second: float
    interference: tuple[NarrowbandInterference, ...]


HF_SSB_SCENARIO_PRESETS = {
    "quiet": HfSsbScenarioPreset(
        "quiet", "mid_latitude_quiet", (250.0, 3_100.0), (250.0, 3_100.0),
        0.98, 0.5, 0.01,
        (NarrowbandInterference(1_800.0, -40.0, power_reference="relative"),)),
    "moderate": HfSsbScenarioPreset(
        "moderate", "mid_latitude_moderate", (300.0, 2_900.0),
        (300.0, 2_900.0), 0.85, 3.0, 0.10,
        (NarrowbandInterference(1_800.0, -24.0, power_reference="relative",
                                drift_hz_per_second=0.2, duty_cycle=0.25),)),
    "disturbed": HfSsbScenarioPreset(
        "disturbed", "mid_latitude_disturbed", (350.0, 2_700.0),
        (350.0, 2_700.0), 0.70, 10.0, 0.50,
        (NarrowbandInterference(1_650.0, -15.0, power_reference="relative",
                                drift_hz_per_second=1.0, duty_cycle=0.5),
         NarrowbandInterference(2_150.0, -18.0, kind="noise", width_hz=180.0,
                                power_reference="relative"))),
}


class _ScenarioChannel(ChannelChain):
    """A chain whose serialized configuration retains scenario provenance."""

    def __init__(self, stages, description: Mapping[str, object]):
        super().__init__(stages)
        self._scenario_description = dict(description)

    def describe(self) -> Mapping[str, object]:
        return {**self._scenario_description,
                "sample_rate": self.sample_rate,
                "stages": [dict(stage.describe()) for stage in self.stages]}


class HfSsbScenario:
    """A complete HF SSB path assembled in physical stage order."""

    def __init__(self, preset: HfSsbScenarioPreset, *, sample_rate: int,
                 snr: SnrSpec, seed: int):
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if snr.kind is not SnrKind.PASSBAND_3KHZ:
            raise ValueError("HF scenario AWGN requires 3 kHz passband-referenced SNR")
        self.preset = preset
        self.sample_rate = int(sample_rate)
        self.snr = snr
        self.seed = int(seed)

    @classmethod
    def from_preset(cls, preset: str, *, sample_rate: int, snr: SnrSpec,
                    seed: int) -> "HfSsbScenario":
        try:
            definition = HF_SSB_SCENARIO_PRESETS[preset]
        except KeyError:
            raise ValueError(
                f"unknown HF SSB scenario preset {preset!r}; "
                f"have {sorted(HF_SSB_SCENARIO_PRESETS)}") from None
        return cls(definition, sample_rate=sample_rate, snr=snr, seed=seed)

    def build(self) -> ChannelChain:
        p, rate = self.preset, self.sample_rate
        stages = (
            FilterChannel(rate, low_hz=p.tx_band_hz[0], high_hz=p.tx_band_hz[1]),
            ClippingChannel(rate, p.clip_limit),
            FrequencyOffsetChannel(rate, p.frequency_offset_hz,
                                   p.drift_hz_per_second),
            WattersonChannel.from_preset(rate, p.watterson_preset,
                                         seed=self.seed),
            NarrowbandInterferenceChannel(rate, p.interference,
                                          seed=self.seed ^ 0x495446),
            AwgnChannel(rate, self.snr, seed=self.seed ^ 0x4157474E),
            FilterChannel(rate, low_hz=p.rx_band_hz[0], high_hz=p.rx_band_hz[1]),
        )
        return _ScenarioChannel(stages, {
            "type": "hf_ssb_scenario", "preset": p.name, "seed": self.seed})

    def describe(self) -> Mapping[str, object]:
        return self.build().describe()


@dataclass(frozen=True)
class FmScenarioPreset:
    """Project-defined FM stress recipe based on the measured bench model."""

    name: str
    carrier_to_noise_db: float
    radio_preset: str
    tx_band_hz: tuple[float, float]
    clip_limit: float
    rf_frequency_error_hz: float
    rf_paths: tuple[FmRfPath, ...]


FM_SCENARIO_PRESETS = {
    "quiet": FmScenarioPreset("quiet", 30.0, "vhf_bench_conservative",
                               (300.0, 2_900.0), 0.98, 0.0, (FmRfPath(),)),
    "moderate": FmScenarioPreset("moderate", 15.0, "vhf_bench_conservative",
                                  (350.0, 2_700.0), 0.85, 500.0,
                                  (FmRfPath(),)),
    "disturbed": FmScenarioPreset(
        "disturbed", 8.0, "vhf_bench_conservative", (400.0, 2_500.0), 0.70,
        1_500.0, (FmRfPath(), FmRfPath(0.0008, 0.45, 1.2))),
}


class FmScenario:
    """A project simulation profile around the complex-IQ FM model.

    These profiles are exploratory test conditions, not propagation standards
    and not measurements of radios beyond the named underlying bench preset.
    """

    def __init__(self, preset: FmScenarioPreset, *, sample_rate: int, seed: int,
                 carrier_to_noise_db: float | None = None,
                 radio_preset: str | None = None):
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        selected_radio = preset.radio_preset if radio_preset is None else radio_preset
        if selected_radio not in FM_RADIO_PRESETS:
            raise ValueError(f"unknown FM radio preset {selected_radio!r}; "
                             f"have {sorted(FM_RADIO_PRESETS)}")
        self.preset, self.sample_rate, self.seed = preset, int(sample_rate), int(seed)
        self.carrier_to_noise_db = (preset.carrier_to_noise_db
                                    if carrier_to_noise_db is None
                                    else float(carrier_to_noise_db))
        self.radio_preset = selected_radio

    @classmethod
    def from_preset(cls, preset: str, *, sample_rate: int, seed: int,
                    carrier_to_noise_db: float | None = None,
                    radio_preset: str | None = None) -> "FmScenario":
        try:
            definition = FM_SCENARIO_PRESETS[preset]
        except KeyError:
            raise ValueError(f"unknown FM scenario preset {preset!r}; "
                             f"have {sorted(FM_SCENARIO_PRESETS)}") from None
        return cls(definition, sample_rate=sample_rate, seed=seed,
                   carrier_to_noise_db=carrier_to_noise_db,
                   radio_preset=radio_preset)

    def build(self) -> ChannelChain:
        p, rate = self.preset, self.sample_rate
        stages = (
            FilterChannel(rate, low_hz=p.tx_band_hz[0], high_hz=p.tx_band_hz[1]),
            ClippingChannel(rate, p.clip_limit),
            ComplexFmChannel.from_preset(
                rate, self.radio_preset, self.carrier_to_noise_db, self.seed,
                rf_frequency_error_hz=p.rf_frequency_error_hz,
                rf_paths=p.rf_paths),
        )
        return _ScenarioChannel(stages, {
            "type": "fm_scenario", "preset": p.name, "seed": self.seed,
            "profile_authority": "whalemodem_project_simulation",
            "is_propagation_standard": False})

    def describe(self) -> Mapping[str, object]:
        return self.build().describe()


# An explicit VHF spelling is convenient at call sites that also use HF SSB.
VhfFmScenario = FmScenario
