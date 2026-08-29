import pytest

from whale import modes
import whale.mode_qualification as qualification
from whale.mode_qualification import (MANIFEST, QualificationLevel,
                                      QualificationEntry,
                                      qualification_level, registry)


@pytest.mark.parametrize("policy", ("vhf-fm", "hf-ssb"))
def test_registry_levels_are_cumulative(policy):
    default = set(registry(policy, "default").supported_ids)
    optional = set(registry(policy, "optional").supported_ids)
    experimental = set(registry(policy, "experimental").supported_ids)
    assert default <= optional <= experimental


def test_manifest_keys_and_global_mode_ids_are_unique():
    keys = [(entry.policy, entry.mode_id) for entry in MANIFEST]
    ids = [entry.mode_id for entry in MANIFEST]
    assert len(keys) == len(set(keys))
    assert len(ids) == len(set(ids))


def test_every_registry_mode_has_its_declared_level():
    for policy in ("vhf-fm", "hf-ssb"):
        for mode in registry(policy, "experimental").modes:
            assert isinstance(qualification_level(policy, mode.mode_id),
                              QualificationLevel)


def test_compatibility_builders_are_default_registries():
    assert modes.default_registry().supported_ids == registry("vhf-fm").supported_ids
    assert modes.hf_registry().supported_ids == registry("hf-ssb").supported_ids


def test_filter_excludes_opt_in_mode_from_default(monkeypatch):
    manifest = tuple(
        QualificationEntry(entry.policy, entry.mode_id,
                           QualificationLevel.OPTIONAL
                           if (entry.policy, entry.mode_id) == ("vhf-fm", 3)
                           else entry.level)
        for entry in MANIFEST)
    monkeypatch.setattr(qualification, "MANIFEST", manifest)
    assert 3 not in registry("vhf-fm", "default").supported_ids
    assert 3 in registry("vhf-fm", "optional").supported_ids


def test_unknown_level_and_policy_are_rejected():
    with pytest.raises(ValueError, match="qualification level"):
        registry("vhf-fm", "qualified-ish")
    with pytest.raises(ValueError, match="channel policy"):
        registry("moon-bounce")
