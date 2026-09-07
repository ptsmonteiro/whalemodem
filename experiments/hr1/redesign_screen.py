#!/usr/bin/env python3
"""Reproducible oracle/capacity screen for HR1-A and HR1-B.

The information estimates use the exact phase-marginal likelihood of an
orthogonal noncoherent MFSK observation.  They are screening proxies, not a
qualification curve.  The held-out smoke uses the repository's complete-
keying waveform SNR and real 48 kHz -> 12 kHz receive boundary.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
from scipy.special import i0e, logsumexp

from experiments.hr1 import benchmark, hr1, hr1b
from whale import rx_audio
from whale.channel import AwgnChannel, SnrKind, SnrSpec, WattersonChannel


SCHEMA = "whalemodem.hr1.redesign-screen.v1"
HELD_OUT_MASTER_SEED = 0x48523042


def noncoherent_information(m: int, esn0: float, *, samples: int,
                            seed: int) -> dict:
    """CM capacity, exact BICM GMI, max-log GMI, and dispersion proxy."""

    rng = np.random.default_rng(seed)
    transmitted = rng.integers(m, size=samples)
    phase = rng.uniform(0, 2 * np.pi, size=samples)
    received = ((rng.normal(size=(samples, m))
                 + 1j * rng.normal(size=(samples, m))) / np.sqrt(2))
    received[np.arange(samples), transmitted] += (
        np.sqrt(esn0) * np.exp(1j * phase))
    argument = 2 * np.sqrt(esn0) * np.abs(received)
    likelihood = np.log(i0e(argument)) + np.abs(argument)
    information_density = (math.log(m)
                           + likelihood[np.arange(samples), transmitted]
                           - logsumexp(likelihood, axis=1)) / math.log(2)
    bits = int(round(math.log2(m)))
    labels = np.arange(m)
    exact_bicm = 0.0
    maxlog = 0.0
    energy = np.abs(received) ** 2
    for bit_at in range(bits):
        mask = ((labels >> (bits - 1 - bit_at)) & 1).astype(bool)
        log0 = logsumexp(likelihood[:, ~mask], axis=1)
        log1 = logsumexp(likelihood[:, mask], axis=1)
        bit = ((transmitted >> (bits - 1 - bit_at)) & 1)
        selected = np.where(bit == 0, log0, log1)
        exact_bicm += 1 - float(np.mean(
            np.logaddexp(log0, log1) - selected)) / math.log(2)
        metric = (np.max(energy[:, ~mask], axis=1)
                  - np.max(energy[:, mask], axis=1))
        sign = 1 - 2 * bit
        candidates = [
            1 - float(np.mean(np.logaddexp(0, -scale * sign * metric)))
            / math.log(2)
            for scale in np.logspace(-2, 2, 81)
        ]
        maxlog += max(candidates)
    return {
        "m": m, "esn0_linear": esn0,
        "esn0_db": 10 * math.log10(esn0),
        "coded_modulation_bits_per_symbol": float(np.mean(information_density)),
        "information_density_variance_bits2": float(np.var(information_density)),
        "exact_bicm_bits_per_symbol": exact_bicm,
        "maxlog_bicm_gmi_bits_per_symbol": maxlog,
    }


def _capture(channel_audio: np.ndarray) -> np.ndarray:
    return rx_audio.downsample(np.concatenate((
        np.asarray(channel_audio, dtype=np.float32),
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32))))


def held_out_awgn(snr_db: float, trials: int) -> dict:
    outcomes = []
    for trial in range(trials):
        seed = benchmark.derive_seed(
            HELD_OUT_MASTER_SEED, "hr1b-held-out-awgn",
            f"awgn|-|{snr_db}", trial + 1)
        payload = np.random.default_rng(seed).bytes(64)
        transmitted = hr1b.HR1B.encode(payload)
        noise_seed = benchmark.derive_seed(seed, "noise", "hr1b", trial + 1)
        spec = SnrSpec(snr_db, SnrKind.WAVEFORM, reference_start=0,
                       reference_stop=len(transmitted))
        noisy = AwgnChannel(48_000, spec, noise_seed).process(transmitted).audio
        decoded = hr1b.decode_aligned(
            _capture(noisy),
            preamble_start=(hr1b.LEAD_RX_SAMPLES
                            + rx_audio.FILTER_DELAY_DECODE_SAMPLES),
            class_id=hr1b.FULL_CLASS)
        outcomes.append({
            "trial": trial + 1, "derived_seed": seed,
            "decoded": decoded.get("payload") == payload,
            "failure": decoded.get("failure"),
            "rs_corrected_bytes": decoded.get("rs_corrected_bytes"),
        })
    delivered = sum(row["decoded"] for row in outcomes)
    return {"waveform_snr_db": snr_db, "trials": trials,
            "delivered": delivered, "outcomes": outcomes}


def held_out_watterson_wiring(preset: str, trials: int = 3) -> dict:
    outcomes = []
    for trial in range(trials):
        seed = benchmark.derive_seed(
            HELD_OUT_MASTER_SEED, "hr1b-held-out-watterson",
            f"{preset}|20", trial + 1)
        payload = np.random.default_rng(seed).bytes(64)
        transmitted = hr1b.HR1B.encode(payload)
        faded = WattersonChannel.from_preset(
            48_000, preset, seed ^ 0x57415454).process(transmitted).audio
        spec = SnrSpec(20.0, SnrKind.WAVEFORM, reference_start=0,
                       reference_stop=len(transmitted))
        noisy = AwgnChannel(48_000, spec, seed ^ 0x4157474E).process(faded).audio
        decoded = hr1b.HR1B.decode(_capture(noisy))
        outcomes.append({"trial": trial + 1, "derived_seed": seed,
                         "decoded": decoded.get("payload") == payload,
                         "failure": decoded.get("failure"),
                         "candidate_rank": decoded.get("candidate_rank")})
    return {"preset": preset, "waveform_snr_db": 20.0, "trials": trials,
            "delivered": sum(row["decoded"] for row in outcomes),
            "outcomes": outcomes}


def report(*, information_samples: int, awgn_trials: int) -> dict:
    snr_linear = 10 ** (-24 / 10)
    # HR1-A is essentially continuously keyed.  Its last-two receiver gets
    # 16 ms; the all-three clean oracle gets 24 ms.
    a_last2_esn0 = snr_linear * 24_000 * 0.016
    a_all3_esn0 = snr_linear * 24_000 * 0.024
    b_waveform = hr1b.HR1B.encode(bytes(range(64))).astype(np.float64)
    b_symbol = hr1b._modulate_symbols(np.asarray([0])).astype(np.float64)
    b_esn0 = ((np.sum(b_symbol ** 2) / 48_000) * snr_linear * 24_000
              / np.mean(b_waveform ** 2))
    information = {
        "hr1a_last_two": noncoherent_information(
            16, a_last2_esn0, samples=information_samples, seed=1001),
        "hr1a_all_three_clean_oracle": noncoherent_information(
            16, a_all3_esn0, samples=information_samples, seed=1002),
        "hr1b_coherent_trusted_pair": noncoherent_information(
            16, b_esn0, samples=information_samples, seed=1003),
        "tone_count_comparison_last_two": [
            noncoherent_information(m, a_last2_esn0,
                                    samples=information_samples, seed=1100 + m)
            for m in (8, 16, 32)
        ],
        "hr1a_all_three_bicm_threshold_grid": [
            {"waveform_snr_db": point,
             **noncoherent_information(
                 16, a_all3_esn0 * 10 ** ((point + 24) / 10),
                 samples=information_samples, seed=1200 + abs(point))}
            for point in (-24, -23, -22, -21, -20)
        ],
    }
    a_required = hr1.FEC_INPUT_BITS / hr1.DATA_SYMBOLS
    overhead = {
        "guard_loss_db": 10 * math.log10(3 / 2),
        "preamble_pilot_lead_tail_energy_fraction": (
            1 - hr1.DATA_SYMBOLS * hr1.SYMBOL_SECONDS / hr1.FRAME_SECONDS),
        "preamble_pilot_lead_tail_loss_db": 10 * math.log10(
            hr1.FRAME_SECONDS / (hr1.DATA_SYMBOLS * hr1.SYMBOL_SECONDS)),
        "checked_framing_loss_db_useful432_to_fec568": 10 * math.log10(
            hr1.FEC_INPUT_BITS / (54 * 8)),
        "termination_pad_finite_frame_loss_db": 10 * math.log10(
            hr1.FEC_INPUT_BITS / (hr1.FEC_INPUT_BITS - 8)),
        "hr1a_required_checked_bits_per_data_symbol": a_required,
        "hr1a_session_rate_projection_bps": 23.894,
        "hr1b_full_frame_seconds": hr1b.FULL_FRAME_SECONDS,
        "hr1b_tiny_frame_seconds": hr1b.TINY_FRAME_SECONDS,
        "hr1b_clean_session_rate_projection_bps": hr1b.CLEAN_SESSION_RATE,
    }
    occupied = benchmark._occupied_bandwidth_99(
        b_waveform, 48_000)
    return {
        "schema": SCHEMA,
        "claim_scope": "bounded_redesign_screen_not_qualification",
        "held_out_master_seed": HELD_OUT_MASTER_SEED,
        "candidate_source": benchmark.source_metadata(hr1b.HR1B),
        "git": benchmark._git_metadata(),
        "environment": benchmark._environment(),
        "snr_convention": {
            "kind": "waveform",
            "signal_reference": "complete_half_open_keying_interval",
            "noise_band_hz": [0.0, 24_000.0],
        },
        "whole_keying_waveform_snr_db": -24.0,
        "information_samples_per_point": information_samples,
        "information": information,
        "loss_and_rate_accounting": overhead,
        "bounded_candidate_set": {
            "geometry": [
                {"m": 8, "observation_ms": 8, "dwell_ms": 24,
                 "spacing_hz": 125, "nominal_bank_hz": 1000,
                 "spread_to_spacing_30hz": 0.24,
                 "decision": "reject_lower_cm_information_than_16_at_same_energy"},
                {"m": 16, "observation_ms": 8, "dwell_ms": 24,
                 "spacing_hz": 125, "nominal_bank_hz": 2000,
                 "spread_to_spacing_30hz": 0.24,
                 "decision": "select"},
                {"m": 32, "observation_ms": 8, "dwell_ms": 24,
                 "spacing_hz": 125, "nominal_bank_hz": 4000,
                 "spread_to_spacing_30hz": 0.24,
                 "decision": "reject_bandwidth"},
                {"m": 32, "observation_ms": 16, "dwell_ms": 32,
                 "spacing_hz": 62.5, "nominal_bank_hz": 2000,
                 "spread_to_spacing_30hz": 0.48,
                 "decision": "defer_doppler_and_16ms_guard_cost"},
            ],
            "coding": [
                {"name": "HR1-A binary rate-1/3 convolutional",
                 "decision": "reject_information_interface_and_code_loss"},
                {"name": "exact binary likelihood plus stronger binary code",
                 "decision": "reject_same_airtime_bicm_rate_deficit"},
                {"name": "GF16 rate-1/4 convolutional plus shortened RS",
                 "decision": "select"},
                {"name": "short LDPC or polar over independent bit metrics",
                 "decision": "defer_requires_multilevel_or_joint_demapper"},
                {"name": "full-frame repetition or rate-1/6",
                 "decision": "reject_clean_session_rate_below_18bps"},
                {"name": "incremental parity on ARQ failure",
                 "decision": "retain_future_option_not_in_clean_wire"},
            ],
        },
        "hr1b": {
            "wire_revision": "B",
            "full_inner": "GF16 memory-2 rate 1/4",
            "full_outer": "shortened RS(96,70), corrects 13 bytes",
            "tiny_inner": "GF16 memory-2 rate 1/3",
            "guard": "8 ms silence then two phase-continuous 8 ms observations",
            "occupied_bandwidth_99": occupied,
            "awgn_held_out": [held_out_awgn(point, awgn_trials)
                              for point in (-25.0, -24.0, -23.0)],
            "watterson_high_snr_real_receiver_wiring": [
                held_out_watterson_wiring(preset)
                for preset in ("mid_latitude_disturbed",
                               "mid_latitude_disturbed_nvis",
                               "high_latitude_disturbed")
            ],
        },
        "decision": "go_hr1b_to_small_real_receiver_boundary_screen",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--information-samples", type=int, default=200_000)
    parser.add_argument("--awgn-trials", type=int, default=20)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    document = report(information_samples=args.information_samples,
                      awgn_trials=args.awgn_trials)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(benchmark._jsonable(document), indent=2) + "\n")
    print(args.out)


if __name__ == "__main__":
    main()
