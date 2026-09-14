"""Evaluate FM MFSK candidates on the calibrated `FmHtIc705Channel`.

Offline only -- never touches a radio.  Each candidate mode gets `trials`
independent frames through the channel; a trial passes only on an exact
decoded-payload match (post-FEC), same criterion as
`scripts/sweep_mfsk_fm.py`'s hardware trials.  Prints pass rate and net
bit/s per candidate, ordered fastest-first among passing candidates.

    python experiments/fm_mfsk/evaluate_candidates.py --trials 60
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np

from experiments.fm_mfsk.channel_model import (DEFAULT_BASE_SNR_DB,
                                                DEFAULT_ISI_DB_PER_BAUD_DOUBLE,
                                                DEFAULT_SELF_NOISE_DB_PER_K_DOUBLE,
                                                DEFAULT_TILT_DB,
                                                FmHtIc705Channel)
from experiments.fm_mfsk.mfsk_fm_mode import mode_for
from whale import rx_audio

CANDIDATES = {
    "G_r1/2":  dict(tone_count=4, subbands=4, symbol_samples=320,
                    frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                    fec_rate="1/2"),
    "G_r2/3":  dict(tone_count=4, subbands=4, symbol_samples=320,
                    frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                    fec_rate="2/3"),
    "G_r3/4":  dict(tone_count=4, subbands=4, symbol_samples=320,
                    frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                    fec_rate="3/4"),
    "G_r5/6":  dict(tone_count=4, subbands=4, symbol_samples=320,
                    frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                    fec_rate="5/6"),
    "G_r7/8":  dict(tone_count=4, subbands=4, symbol_samples=320,
                    frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                    fec_rate="7/8"),
    "H_r1/2":  dict(tone_count=2, subbands=4, symbol_samples=160,
                    frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                    fec_rate="1/2"),
    "H_r2/3":  dict(tone_count=2, subbands=4, symbol_samples=160,
                    frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                    fec_rate="2/3"),
    "H_r3/4":  dict(tone_count=2, subbands=4, symbol_samples=160,
                    frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                    fec_rate="3/4"),
    "H_r5/6":  dict(tone_count=2, subbands=4, symbol_samples=160,
                    frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                    fec_rate="5/6"),
    "H300_M4K2_r1/2": dict(tone_count=4, subbands=2, symbol_samples=160,
                           frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                           fec_rate="1/2"),
    "H300_M4K2_r3/4": dict(tone_count=4, subbands=2, symbol_samples=160,
                           frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                           fec_rate="3/4"),
    "M2K4_200Bd_pre0":  dict(tone_count=2, subbands=4, symbol_samples=240,
                             frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                             fec_rate="1/2", preemph_db=0.0),
    "M2K4_200Bd_pre6":  dict(tone_count=2, subbands=4, symbol_samples=240,
                             frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                             fec_rate="1/2", preemph_db=6.0),
    "M2K4_200Bd_pre10": dict(tone_count=2, subbands=4, symbol_samples=240,
                             frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
                             fec_rate="1/2", preemph_db=10.0),
    "M4K4_200Bd_pre0":  dict(tone_count=4, subbands=4, symbol_samples=240,
                             frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=2960.0,
                             fec_rate="1/2", preemph_db=0.0),
    "M4K4_200Bd_pre6":  dict(tone_count=4, subbands=4, symbol_samples=240,
                             frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=2960.0,
                             fec_rate="1/2", preemph_db=6.0),
}

#: Combinatorial-index-modulation candidates: exactly `active_tones` of a
#: full N=`tone_count` tone grid on per symbol (subbands=1, mapping=
#: "combinatorial"), instead of G's one-tone-per-subband constraint.  N=16
#: at 150 Hz spacing is the same physical grid as G/K8; N=8 at 300 Hz
#: spacing is the same grid as H, included only as a baud-rate comparison.
#: k<=4 candidates are the ones this evaluation recommends -- see the
#: module docstring/report: the channel model's K-dependent self-noise
#: term is calibrated from K<=8 *subband* anchors, not from k>4
#: simultaneous-tone combinatorial geometries, so k=5/6 rows below are
#: optimistic and are kept only for comparison.
for _k in (3, 4, 5, 6):
    for _rate in ("3/4", "5/6", "7/8"):
        for _frame in (5.0, 8.0):
            CANDIDATES[f"Comb_N16_k{_k}_r{_rate.replace('/', '')}_{int(_frame)}s"] = dict(
                tone_count=16, subbands=1, symbol_samples=320,
                frame_seconds=_frame, band_lo_hz=600.0, band_hi_hz=3000.0,
                fec_rate=_rate, mapping="combinatorial", active_tones=_k)

#: Same combinatorial mapping on the 300 Bd / 8-tone grid, for comparison.
for _k in (3, 4):
    for _rate in ("3/4", "5/6", "7/8"):
        CANDIDATES[f"Comb_N8_300Bd_k{_k}_r{_rate.replace('/', '')}"] = dict(
            tone_count=8, subbands=1, symbol_samples=160,
            frame_seconds=5.0, band_lo_hz=600.0, band_hi_hz=3000.0,
            fec_rate=_rate, mapping="combinatorial", active_tones=_k)

#: G (M4K4, subband mapping) at every puncture rate and both frame
#: durations, for a direct apples-to-apples comparison against the
#: combinatorial candidates above on the same 16-tone/150-Bd grid.
for _rate in ("3/4", "5/6", "7/8"):
    for _frame in (5.0, 8.0):
        CANDIDATES[f"G_r{_rate.replace('/', '')}_{int(_frame)}s"] = dict(
            tone_count=4, subbands=4, symbol_samples=320,
            frame_seconds=_frame, band_lo_hz=600.0, band_hi_hz=3000.0,
            fec_rate=_rate)


def _payload(mode, seed):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, mode.max_payload_bytes, dtype=np.uint8).tobytes()


def evaluate(name, kwargs, channel, trials, seed0):
    try:
        mode = mode_for(**kwargs)
    except Exception as exc:
        return {"name": name, "error": f"{type(exc).__name__}: {exc}"}
    passed = 0
    snr_worst = []
    for t in range(trials):
        payload = _payload(mode, seed0 + t)
        tx = np.asarray(mode.modulate(payload), dtype=np.float32)
        rxed = channel.process(tx, mode.tx_sample_rate,
                               subbands=mode.active_tone_count,
                               seed=seed0 * 1000 + t, baud=mode.symbol_rate)
        padded = np.concatenate([np.zeros(4800, np.float32), rxed,
                                 np.zeros(9600, np.float32)])
        audio12k = rx_audio.downsample(padded.astype(np.float32))
        result = mode.decode(audio12k)
        ok = result.get("payload") == payload
        passed += int(bool(ok))
        if result.get("tone_snr_db_worst") is not None:
            snr_worst.append(result["tone_snr_db_worst"])
    return {
        "name": name, "passed": passed, "trials": trials,
        "pass_rate": passed / trials,
        "net_bit_rate": mode.net_bit_rate(),
        "max_payload_bytes": mode.max_payload_bytes,
        "frame_seconds": mode.frame_seconds(),
        "tone_snr_db_worst_mean": float(np.mean(snr_worst)) if snr_worst else None,
        "geometry": mode.describe(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trials", type=int, default=50)
    parser.add_argument("--seed", type=int, default=424242)
    parser.add_argument("--tilt-db", type=float, default=DEFAULT_TILT_DB)
    parser.add_argument("--base-snr-db", type=float, default=DEFAULT_BASE_SNR_DB)
    parser.add_argument("--self-noise-db-per-k-double", type=float,
                        default=DEFAULT_SELF_NOISE_DB_PER_K_DOUBLE)
    parser.add_argument("--isi-db-per-baud-double", type=float,
                        default=DEFAULT_ISI_DB_PER_BAUD_DOUBLE)
    parser.add_argument("--only", nargs="*", default=None,
                        help="subset of CANDIDATES names to run")
    args = parser.parse_args(argv)

    channel = FmHtIc705Channel(tilt_db=args.tilt_db, base_snr_db=args.base_snr_db,
                               self_noise_db_per_k_double=args.self_noise_db_per_k_double,
                               isi_db_per_baud_double=args.isi_db_per_baud_double)
    print(f"Channel: {channel.describe()}")
    print(f"{'candidate':20s} {'pass':>10s} {'net bit/s':>10s} "
          f"{'bytes':>6s} {'frame s':>8s} {'worst SNR':>10s}")
    rows = []
    names = args.only or list(CANDIDATES)
    for name in names:
        row = evaluate(name, CANDIDATES[name], channel, args.trials, args.seed)
        rows.append(row)
        if "error" in row:
            print(f"{name:20s}  ERROR: {row['error']}")
            continue
        print(f"{name:20s} {row['passed']:3d}/{row['trials']:<3d} ({row['pass_rate']*100:5.1f}%) "
              f"{row['net_bit_rate']:10.1f} {row['max_payload_bytes']:6d} "
              f"{row['frame_seconds']:8.2f} "
              f"{row['tone_snr_db_worst_mean'] if row['tone_snr_db_worst_mean'] is not None else float('nan'):10.2f}")
    print()
    passing = [r for r in rows if "error" not in r and r["pass_rate"] == 1.0]
    passing.sort(key=lambda r: -r["net_bit_rate"])
    print("Fastest configs at 100% pass rate:")
    for r in passing:
        print(f"  {r['name']:20s} {r['net_bit_rate']:8.1f} bit/s")
    return rows


if __name__ == "__main__":
    main()
