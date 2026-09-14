"""Fit `channel_model.FmHtIc705Channel.base_snr_db` against the three
hardware SER anchors (G, K8, H) recorded in that module's docstring.

Offline only: builds each anchor mode, runs `trials` simulated frames
through the tilt+AWGN channel at each candidate `base_snr_db`, measures
pre-FEC symbol error rate exactly as `scripts/sweep_mfsk_fm.py` does
(`hard_tones` vs. the transmitted `tone_grid`), and reports the fit.

    python experiments/fm_mfsk/calibrate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np

from experiments.fm_mfsk.channel_model import FmHtIc705Channel
from experiments.fm_mfsk.mfsk_fm_mode import mode_for
from whale import rx_audio

ANCHORS = {
    # name: (mode kwargs, target overall SER range, per-subband target or None)
    "G": (dict(tone_count=4, subbands=4, symbol_samples=320, frame_seconds=5.0,
               band_lo_hz=600.0, band_hi_hz=3000.0),
          (0.00005, 0.0002), None),
    "K8": (dict(tone_count=2, subbands=8, symbol_samples=320, frame_seconds=5.0,
                band_lo_hz=600.0, band_hi_hz=3000.0),
           (0.0003, 0.0036), None),
    "H": (dict(tone_count=2, subbands=4, symbol_samples=160, frame_seconds=5.0,
               band_lo_hz=600.0, band_hi_hz=3000.0),
          (0.056, 0.106), (0.001, 0.003, None, (0.09, 0.13))),
}


def _payload(mode, seed):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, mode.max_payload_bytes, dtype=np.uint8).tobytes()


def run_ser(mode, channel, trials, seed0):
    """Average pre-FEC SER and per-subband SER over `trials` frames."""
    per_subband_sum = np.zeros(mode.subbands)
    total_symbols = 0
    total_errors = 0
    for t in range(trials):
        payload = _payload(mode, seed0 + t)
        tx = np.asarray(mode.modulate(payload), dtype=np.float32)
        rxed = channel.process(tx, mode.tx_sample_rate, subbands=mode.subbands,
                               seed=seed0 * 1000 + t, baud=mode.symbol_rate)
        padded = np.concatenate([np.zeros(4800, np.float32), rxed,
                                 np.zeros(9600, np.float32)])
        audio12k = rx_audio.downsample(padded.astype(np.float32))
        result = mode.demodulate(audio12k)
        if not result["synced"] or result.get("hard_tones") is None:
            per_subband_sum += 1.0  # total desync counts as all-wrong
            total_symbols += mode.payload_symbols * mode.subbands
            total_errors += mode.payload_symbols * mode.subbands
            continue
        truth = mode.tone_grid(payload)
        hard = np.asarray(result["hard_tones"])
        wrong = truth != hard
        per_subband_sum += wrong.mean(axis=0)
        total_symbols += wrong.size
        total_errors += int(wrong.sum())
    return total_errors / max(total_symbols, 1), per_subband_sum / trials


def _log_error(ser, target_range):
    target_mid = np.sqrt(max(target_range[0], 1e-6) * max(target_range[1], 1e-6))
    return (np.log10(max(ser, 1e-6)) - np.log10(max(target_mid, 1e-6))) ** 2


def score(names, base_snr_db, trials, tilt_db, k_slope_db, isi_slope_db=0.0,
          seed0=1000):
    channel = FmHtIc705Channel(tilt_db=tilt_db, base_snr_db=base_snr_db,
                               self_noise_db_per_k_double=k_slope_db,
                               isi_db_per_baud_double=isi_slope_db)
    report = {}
    error = 0.0
    for name in names:
        kwargs, target_range, subband_targets = ANCHORS[name]
        mode = mode_for(**kwargs)
        ser, per_subband = run_ser(mode, channel, trials, seed0)
        error += _log_error(ser, target_range)
        report[name] = {"ser": ser, "target_range": target_range,
                        "per_subband": per_subband.tolist()}
    return error, report


def main():
    trials = 12
    tilt_db = -13.0

    # Stage 1: fit (base_snr_db, self_noise_db_per_k_double) against the two
    # anchors that share a baud rate (G, K8 both 150 Bd) -- this isolates
    # the K-dependent self-noise slope from any baud-rate effect.
    print("Stage 1: base_snr_db x self_noise_db_per_k_double, fit to G+K8")
    best1 = None
    for k_slope in (4.0, 6.0, 8.0):
        for base_snr_db in np.arange(-2.0, 10.01, 1.0):
            error, report = score(("G", "K8"), base_snr_db, trials, tilt_db, k_slope)
            if best1 is None or error < best1[0]:
                best1 = (error, base_snr_db, k_slope, report)
    error1, base_snr_db, k_slope, report1 = best1
    print(f"  best: base_snr_db={base_snr_db:.1f} self_noise_db_per_k_double="
          f"{k_slope:.1f} fit_error={error1:.3f}")
    for name in ("G", "K8"):
        r = report1[name]
        print(f"    {name}: SER={r['ser']*100:.4f}%  target="
              f"{r['target_range'][0]*100:.4f}-{r['target_range'][1]*100:.4f}%")

    # Stage 2: with that pinned, fit isi_db_per_baud_double to H alone (H
    # shares K=4 with G but runs at 300 Bd instead of 150).
    print("\nStage 2: isi_db_per_baud_double, fit to H (base_snr_db/k_slope pinned)")
    best2 = None
    for isi_slope in np.arange(0.0, 40.01, 2.0):
        error, report = score(("H",), base_snr_db, trials, tilt_db, k_slope, isi_slope)
        if best2 is None or error < best2[0]:
            best2 = (error, isi_slope, report)
    error2, isi_slope, report2 = best2
    r = report2["H"]
    print(f"  best: isi_db_per_baud_double={isi_slope:.1f} fit_error={error2:.3f}")
    print(f"    H: SER={r['ser']*100:.4f}%  target={r['target_range'][0]*100:.4f}"
          f"-{r['target_range'][1]*100:.4f}%  per_subband="
          + ", ".join(f"{v*100:.3f}%" for v in r["per_subband"]))

    print("\nFinal calibrated channel:")
    print(f"  tilt_db={tilt_db}  base_snr_db={base_snr_db:.1f}  "
          f"self_noise_db_per_k_double={k_slope:.1f}  "
          f"isi_db_per_baud_double={isi_slope:.1f}")
    final_error, final_report = score(("G", "K8", "H"), base_snr_db, trials * 2,
                                      tilt_db, k_slope, isi_slope)
    print(f"  combined fit_error (24 trials/anchor)={final_error:.3f}")
    for name in ("G", "K8", "H"):
        r = final_report[name]
        print(f"    {name}: SER={r['ser']*100:.4f}%  target="
              f"{r['target_range'][0]*100:.4f}-{r['target_range'][1]*100:.4f}%  "
              "per_subband=" + ", ".join(f"{v*100:.3f}%" for v in r["per_subband"]))
    return base_snr_db, k_slope, isi_slope, final_report


if __name__ == "__main__":
    main()
