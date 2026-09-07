"""Fast deterministic tests for the HC2 AWGN SNR/EVM sweep harness.

These check the measurement harness, not the waveform's SNR threshold: the
full sweep is a long-running command, not a pytest.
"""

import numpy as np
import pytest

from whale.channel import AwgnChannel, SnrKind, SnrSpec
from whale.qualification import trial_seed
from whale.trials import TrialOutcome, TrialResult

from experiments.hc2_32qam import hc2_32qam as hc2
from experiments.hc2_32qam import benchmark_hc2_snr as bench


PAD = 4_096
FAST = dict(max_frequency_offset_hz=2.0, acquisition_step_hz=2.0)


def _capture(payload, snr_db=None, seed=7):
    frame = hc2.modulate(payload)
    capture = np.concatenate((np.zeros(PAD, np.float32), frame,
                              np.zeros(PAD, np.float32)))
    if snr_db is None:
        return np.asarray(capture, dtype=float)
    spec = SnrSpec(snr_db, SnrKind.WAVEFORM,
                   reference_start=PAD, reference_stop=PAD + len(frame))
    channel = AwgnChannel(hc2.SAMPLE_RATE, spec, seed)
    return np.asarray(channel.process(capture).audio, dtype=float)


def _metrics(payload, snr_db=None, seed=7):
    rx = _capture(payload, snr_db, seed)
    decoded, diagnostics = hc2.demodulate(rx, return_diagnostics=True, **FAST)
    return decoded, bench.frame_metrics(rx, diagnostics, payload)


def _short_payload(seed=5):
    return np.random.default_rng(seed).integers(
        0, 256, 64, dtype=np.uint8).tobytes()


def test_clean_capture_has_negligible_evm_and_no_raw_bit_errors():
    payload = _short_payload()
    decoded, metrics = _metrics(payload)
    assert decoded == payload
    assert metrics["equalizer_rejected"] is False
    assert metrics["raw_ber"] == 0.0
    assert metrics["symbol_error_rate"] == 0.0
    # ~2% is the receiver's noise-free implementation floor: the analytic
    # (``scipy.signal.hilbert``) front end leaks across the per-symbol FFT,
    # unlike the real-FFT oracle path, whose EVM is ~1e-6%.  It rose from
    # ~1.74% to ~2.12% (full-capacity frames) when the two training symbols
    # stopped being identical: the channel estimate now averages two
    # different leakage patterns instead of two matching ones, so less of the
    # front end's own error cancels.  Still four times below the 10% trigger.
    assert metrics["evm_percent"] == pytest.approx(1.99, abs=0.15)
    assert metrics["evm_percent"] == pytest.approx(
        metrics["true_evm_percent"], abs=1e-9)


def test_evm_grows_with_added_noise():
    payload = _short_payload(11)
    _, clean = _metrics(payload)
    _, noisy = _metrics(payload, snr_db=14.0, seed=101)
    assert clean["evm_percent"] < noisy["evm_percent"]
    assert noisy["evm_db"] == pytest.approx(
        20 * np.log10(noisy["evm_percent"] / 100.0))


def test_replica_reproduces_the_receiver_decision_exactly():
    """frame_metrics must slice the same constellation the decoder used."""
    payload = _short_payload(23)
    rx = _capture(payload, snr_db=11.0, seed=404)
    decoded, diagnostics = hc2.demodulate(rx, return_diagnostics=True, **FAST)
    tracked, _ = bench._phase_tracked_grid(
        rx, int(diagnostics["start_sample"]),
        float(diagnostics["frequency_offset_hz"]))
    replica = hc2._decode_packet(hc2.bits_from_qam32(tracked))
    assert replica == decoded


def test_trial_seeding_is_deterministic_and_reproducible():
    seed = trial_seed(20260830, bench.SEED_NAMESPACE, 3, 7)
    assert seed == trial_seed(20260830, bench.SEED_NAMESPACE, 3, 7)
    assert seed != trial_seed(20260830, bench.SEED_NAMESPACE, 4, 7)
    assert seed != trial_seed(20260831, bench.SEED_NAMESPACE, 3, 7)
    kwargs = dict(snr_db=30.0, seed=seed, trial=7, label="unit",
                  payload_bytes=32, lead_samples=PAD, tail_samples=PAD,
                  **FAST)
    first, second = bench.frame_trial(**kwargs), bench.frame_trial(**kwargs)
    assert first.outcome is TrialOutcome.DECODED
    assert first.decoder_metrics == second.decoder_metrics
    assert first.channel_measurements == second.channel_measurements


def test_wilson_interval_brackets_the_point_estimate():
    low, high = bench.wilson(90, 100)
    assert 0.0 < low < 0.9 < high < 1.0
    assert bench.wilson(0, 10)[0] == 0.0
    assert bench.wilson(10, 10)[1] == pytest.approx(1.0)
    assert bench.wilson(0, 0) == [0.0, 1.0]


def _fake(evm, decoded):
    return TrialResult(
        trial=1, direction="unit", mode_id=bench.SEED_NAMESPACE,
        mode_name=bench.MODE_NAME, payload_bytes=1,
        outcome=TrialOutcome.DECODED if decoded else TrialOutcome.PAYLOAD_FAILED,
        tx_samples=1, tx_sample_rate=48_000, rx_samples=1,
        rx_sample_rate=48_000, keyed_seconds=1.0,
        decoder_metrics={"evm_percent": evm})


def test_evm_separation_reports_threshold_and_overlap():
    clean = [_fake(v, True) for v in (5.0, 6.0, 7.0)]
    dirty = [_fake(v, False) for v in (12.0, 13.0, 14.0)]
    separated = bench.evm_separation(clean + dirty)
    assert separated["accuracy"] == 1.0
    assert separated["overlap_evm_percent"] is None
    assert 7.0 <= separated["threshold_evm_percent"] < 12.0

    mixed = bench.evm_separation(clean + [_fake(9.0, True)] +
                                 [_fake(8.0, False)] + dirty)
    assert mixed["overlap_evm_percent"] == [8.0, 9.0]
    assert mixed["overlap_trials"] == 2
    assert mixed["accuracy"] < 1.0
    assert mixed["false_accept"] + mixed["false_reject"] == 1


def test_tiny_smoke_sweep_produces_a_complete_artifact():
    args = bench.build_parser().parse_args([
        "--trials", "1", "--points", "30", "0",
        "--payload-bytes", "32", "--lead-samples", str(PAD),
        "--tail-samples", str(PAD), "--max-frequency-offset-hz", "2",
        "--acquisition-step-hz", "2", "--quiet"])
    args.out = None
    artifact = bench.run(args)

    assert artifact["schema"] == "whalemodem.hc2-awgn-snr-evm.v1"
    assert artifact["qualification_evidence"] is False
    assert artifact["capture"]["frame_samples"] == hc2.FRAME_SAMPLES
    high, low = artifact["summaries"]
    assert high["snr_db"] == 30.0 and high["delivered"] == 1
    assert high["realized_payload_bps"] == pytest.approx(
        hc2.SUSTAINED_USER_BIT_RATE)
    assert low["snr_db"] == 0.0 and low["delivered"] == 0
    assert low["realized_payload_bps"] == 0.0
    assert high["errors"] == low["errors"] == 0
    assert low["evm_percent_failed"]["median"] > high["evm_percent_decoded"]["median"]
    assert len(artifact["trials"]) == 2
    # The SNR reference must be the frame span, never the padding.
    reference = artifact["trials"][0]["channel_measurements"]["reference_samples"]
    assert reference == [PAD, PAD + hc2.FRAME_SAMPLES]
