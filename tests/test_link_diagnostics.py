import numpy as np

from whale import link


def test_decode_snr_summary_uses_tone_estimate():
    assert link._decode_snr_summary({"tone_snr_db": 14.46}) == (
        "SNR 14.5 dB (tone)")


def test_decode_snr_summary_uses_median_finite_carriers():
    result = {"carrier_snr_db": np.array([10.0, np.inf, 14.0, 12.0])}
    assert link._decode_snr_summary(result) == (
        "SNR 12.0 dB (median carrier)")


def test_decode_snr_summary_marks_missing_estimate_unavailable():
    assert link._decode_snr_summary({"confidence": 0.99}) == "SNR unavailable"


def test_decode_snr_summary_uses_effective_sync_estimate():
    assert link._decode_snr_summary({"snr_db": 8.24}) == (
        "SNR 8.2 dB (effective sync)")
