"""The mode contract, run against every mode the qualification manifest
declares, on every channel policy, at every qualification level.

This is the single place the link-layer/physical-layer contract
(`whale.waveform.WaveformMode`) is written down and enforced, rather than a
prose description a mode implementation can silently drift away from. The
mode list is derived from `whale.mode_qualification.MANIFEST` via
`registry(policy, EXPERIMENTAL)` -- the cumulative, everything-declared
level -- so a newly declared mode is automatically covered and this file
never needs its own list of names maintained by hand.

MODE_QUALIFICATION.md section 1.4 requires each mode to reject silence,
bounded white noise, a bare carrier, and non-finite or wrong-shaped audio
without raising or doing unbounded work. Truncated audio, corrupt
header/length, corrupt payload/CRC, and impossible declared length are
already exercised per mode (most completely for VF14; HC0/HC1W are
fixed-length) by test_vf14_mode.py, test_hc0_mode.py and test_hc1w_mode.py,
so they are not repeated here.

Software only -- no radios, no sound cards.
"""

from __future__ import annotations

import numpy as np
import pytest

from whale import framing, rx_audio, waveform
from whale.mode_qualification import QualificationLevel, registry

RNG = np.random.default_rng(20260830)
CAPTURE_SECONDS = 3
CAPTURE_SAMPLES = CAPTURE_SECONDS * rx_audio.CAPTURE_SAMPLE_RATE


def _all_modes():
    """Every mode declared on every policy, deduplicated by mode_id.

    `registry(policy, EXPERIMENTAL)` is cumulative -- default, optional and
    experimental modes all come back -- so this is every mode the manifest
    knows about. Mode IDs are unique across policies (validate_manifest()
    enforces it), so a dict keyed by mode_id cannot silently drop one.
    """
    by_id = {}
    for policy in ("fm", "hf"):
        for mode in registry(policy, QualificationLevel.EXPERIMENTAL).modes:
            by_id[mode.mode_id] = mode
    return tuple(sorted(by_id.values(), key=lambda m: m.name))


MODES = _all_modes()

#: Sanity floor: catches a `registry()`/MANIFEST wiring mistake that would
#: silently shrink this file's coverage back down toward the old hardcoded
#: list of 12.
assert len(MODES) >= 20, f"expected every declared mode, found {len(MODES)}"


def _downsampled(audio):
    return rx_audio.downsample(np.asarray(audio, dtype=np.float64))


def _silence():
    return _downsampled(np.zeros(CAPTURE_SAMPLES))


def _white_noise():
    return _downsampled(RNG.normal(0.0, 0.2, CAPTURE_SAMPLES))


def _bare_carrier():
    t = np.arange(CAPTURE_SAMPLES) / rx_audio.CAPTURE_SAMPLE_RATE
    return _downsampled(0.3 * np.sin(2 * np.pi * 1_500.0 * t))


def _non_finite():
    audio = RNG.normal(0.0, 0.2, CAPTURE_SAMPLES)
    audio[CAPTURE_SAMPLES // 2] = np.nan
    audio[CAPTURE_SAMPLES // 3] = np.inf
    return _downsampled(audio)


def _wrong_shape():
    return RNG.normal(0.0, 0.2, (1_000, 2))


def _empty():
    return np.zeros(0, dtype=np.float64)


CASES = {
    "silence": _silence,
    "bounded white noise": _white_noise,
    "bare carrier": _bare_carrier,
    "non-finite audio": _non_finite,
    "wrong-shaped audio": _wrong_shape,
    "empty audio": _empty,
}


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_hostile_input_is_rejected_without_raising(mode, case):
    audio = CASES[case]()
    result = mode.decode(audio)
    assert result.get("payload") is None, (mode.name, case)


# Attributes whale/link.py dereferences on a waveform mode.
REQUIRED_PROFILE_ATTRS = (
    "name",
    "mode_id",
    "rx_sample_rate",
    "tx_sample_rate",
    "chunk_size",
    "confidence_threshold",
    "encode",
    "decode",
)


@pytest.mark.parametrize("attr", REQUIRED_PROFILE_ATTRS)
@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_mode_exposes_every_attribute_the_link_dereferences(mode, attr):
    assert hasattr(mode, attr), f"{mode.name} is missing {attr}"


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_mode_satisfies_the_waveform_protocol(mode):
    assert isinstance(mode, waveform.WaveformMode)


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_declares_the_frequency_hint_capability(mode):
    # `supports_frequency_hint` is a declared attribute (defaulting to
    # False via ModeDescription), not a getattr() probe -- a new mode that
    # forgets it still gets a real bool rather than link_receiver.py
    # silently assuming "no hint".
    assert isinstance(mode.supports_frequency_hint, bool)


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_declares_the_streaming_phy_capability(mode):
    # `streaming_phy` is None for every mode except hf6-hf9's OFDM cousins
    # hf7/hf8/hf9, which return the PHY streaming.py runs the receiver
    # search against. Never an AttributeError, and never reached through
    # a mode's private `codec` attribute.
    assert mode.streaming_phy is None or hasattr(mode.streaming_phy, "total_ofdm_symbols")


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_describe_is_a_nonempty_string(mode):
    description = mode.describe()
    assert isinstance(description, str) and description


# -- decode() must accept the option kwargs shipped callers actually pass ---
#
# whale/link_receiver.py passes freq_hint_hz (guarded per-mode by
# `getattr(profile, "supports_frequency_hint", False)`, but every mode must
# still tolerate receiving it -- the guard protects against a mode ignoring
# a hint it cannot use, not against the call itself). whale/streaming.py's
# OfdmReceiver passes acquisition unconditionally to whatever profile it
# holds. A mode with no use for either option must accept and ignore it,
# not raise -- that is the whole point of decode()'s **kwargs.
DECODE_OPTIONS = (
    {},
    {"freq_hint_hz": 137.5},
    {"acquisition": (0.9, 0, 0.0)},
    {"freq_hint_hz": 137.5, "acquisition": (0.9, 0, 0.0)},
)


@pytest.mark.parametrize("options", DECODE_OPTIONS,
                         ids=lambda o: ",".join(sorted(o)) or "none")
@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_decode_tolerates_the_option_kwargs_shipped_callers_pass(mode, options):
    # Regression coverage for the ScFdeMode family (vfs1/vfs2/vfs3), vf12
    # (and vf16, which inherits its decode) and fmht4: their decode() used
    # to take bare (self, audio) and raised TypeError on any kwarg.
    mode.decode(_silence(), **options)


# -- clean round trip: decode(encode(payload)) recovers payload ------------

LEAD_SAMPLES = 4_000
TAIL_SAMPLES = 2_000


def _capture(audio):
    return rx_audio.downsample(np.concatenate((
        np.zeros(LEAD_SAMPLES, np.float32), np.asarray(audio, np.float32),
        np.zeros(TAIL_SAMPLES, np.float32))))


@pytest.mark.parametrize("size", ("empty", "full_capacity"))
@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_clean_round_trip_recovers_the_payload(mode, size):
    n = 0 if size == "empty" else mode.chunk_size
    payload = RNG.integers(0, 256, n, dtype=np.uint8).tobytes()
    audio = mode.encode(payload)
    result = mode.decode(_capture(audio))
    assert result.get("payload") == payload, (mode.name, size, result.get("failure"))
    assert result.get("crc_ok") is True, (mode.name, size)


# -- canonical result-key types ---------------------------------------------
#
# Not every mode populates every key (a mode that never syncs has no
# end_index), so this only checks the type of keys that ARE present, on a
# result that did sync and decode cleanly.
RESULT_KEY_TYPES = {
    "payload": (bytes, type(None)),
    "crc_ok": (bool, np.bool_),
    "confidence": (float, int, np.floating, np.integer),
    "decoded_length": (int, np.integer),
    "start_index": (int, np.integer, type(None)),
    "end_index": (int, np.integer, type(None)),
    "sync_end_index": (int, np.integer, type(None)),
    "start_sample": (int, np.integer, type(None)),
    "snr_db": (float, int, np.floating, np.integer),
    "freq_offset_hz": (float, int, np.floating, np.integer, type(None)),
    "decode_cpu_seconds": (float, np.floating),
}


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_decode_result_keys_carry_the_canonical_types(mode):
    payload = RNG.integers(0, 256, mode.chunk_size, dtype=np.uint8).tobytes()
    result = mode.decode(_capture(mode.encode(payload)))
    assert result.get("payload") == payload  # sanity: this decode did work
    for key, types in RESULT_KEY_TYPES.items():
        if key in result:
            assert isinstance(result[key], types), (mode.name, key, type(result[key]))


# -- airtime() is positive, finite, and matches encode()'s output length ---
#
# Default tolerance is tight (encode() and airtime() should describe the
# exact same samples). hf5 is the one documented exception: its waveform is
# `whale.phy.sc.SingleCarrierMode`, whose `modulate()` pulse-shapes with an
# RRC filter in `np.convolve(..., mode="full")`, which appends a
# `span_symbols`-wide filter tail that `frame_seconds()` does not count.
# That is a ~0.12% discrepancy (8 ms on a 6.9 s frame) inherent to the
# shared sc.py waveform, not a bug in hf5_mode.py, so hf5 alone gets a
# looser bound rather than loosening the bound for every mode.
AIRTIME_TOLERANCE = {"hf5": 0.002}
DEFAULT_AIRTIME_TOLERANCE = 1e-6


@pytest.mark.parametrize("size", (0, "half", "full_capacity"))
@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_airtime_matches_encoded_length(mode, size):
    n = {"half": mode.chunk_size // 2, "full_capacity": mode.chunk_size}.get(size, size)
    payload = RNG.integers(0, 256, n, dtype=np.uint8).tobytes()
    audio = mode.encode(payload)
    expected = mode.airtime(n)
    assert expected > 0 and np.isfinite(expected)
    actual = len(audio) / mode.tx_sample_rate
    rel = AIRTIME_TOLERANCE.get(mode.name, DEFAULT_AIRTIME_TOLERANCE)
    assert actual == pytest.approx(expected, rel=rel), (mode.name, size)


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name)
def test_airtime_of_a_full_frame_is_positive_and_finite(mode):
    # format_mode() -- and thus every mode's describe() -- calls exactly
    # this: the AIR_HEADER-wrapped full-capacity DATA frame.
    frame_seconds = mode.airtime(framing.AIR_HEADER_BYTES + mode.chunk_size)
    assert frame_seconds > 0 and np.isfinite(frame_seconds)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
