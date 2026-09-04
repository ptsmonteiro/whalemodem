"""How long may a symbol be on this path? Coherent-integration analysis.

Runs offline over sounder captures; never touches a radio.

A periodic sounding block gives one complex sample per subcarrier per repeat,
X[r,k]. Summing N consecutive repeats coherently is exactly what a receiver
does when it lengthens its symbol, so "how long can a symbol be" and "how far
does coherent integration keep paying" are the same question.

For a block of N repeats,

    E|sum X|^2  =  N^2 |H|^2  +  N sigma^2          (H constant over N)

so the *effective* SNR of an N-long symbol is

    SNR(N) = (P(N)/N - sigma^2) / sigma^2 ,   P(N) = E|sum X|^2

which grows as N while the channel stays coherent and stops when it does not.
Reporting it needs sigma^2 measured independently -- on this path the signal
sits well below the noise, so P(1) is essentially sigma^2 and any ratio taken
against it reads as "no gain" no matter how coherent the channel is. The noise
reference therefore comes from the capture's own silent lead-in, at the same
receiver gain, at the same bins, with the same normalisation.

The knee in SNR(N) is a hard ceiling on symbol duration: past it a longer
symbol collects noise without collecting signal.

AGC caveat: the lead-in and the burst are only at the same gain because the
signal here is far below the noise, so the AGC has nothing to react to. On a
strong path that assumption would not hold.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np

from experiments.hf15_lowsnr_ofdm import sounder as S

LENGTHS = (1, 2, 4, 8, 16, 24, 32, 48, 64, 96, 128)


def noise_reference(cap, s0, fft_size, bins, hz, min_rep=8):
    """Per-bin noise power from the silence before the burst.

    Returns None when the lead-in is too short to average over.
    """
    avail = s0 // fft_size
    if avail < min_rep:
        return None
    n = min(avail, 24)
    start = s0 - n * fft_size
    xr = S._bin_matrix(cap, start, fft_size, n, bins, hz)
    return np.mean(np.abs(xr) ** 2, axis=0)


def analyse_capture(cap, fft_size, repeats, seed_hz=None):
    wave, bins, bin_syms = S.sounding_symbol(fft_size)
    est = S.estimate_block(cap, fft_size, repeats, bins, bin_syms, wave,
                           seed_hz=seed_hz)
    if not est.get("located"):
        return None
    n_rep = est["repeats_used"]
    s0, hz = est["start"], est["freq_offset_hz"]
    sigma2 = noise_reference(cap, s0, fft_size, bins, hz)
    if sigma2 is None:
        return {"error": "lead-in too short for a noise reference"}
    xr = S._bin_matrix(cap, s0, fft_size, n_rep, bins, hz)

    sym_s = fft_size / S.DESIGN_RATE
    curve = []
    for n in LENGTHS:
        if n > n_rep:
            continue
        n_blocks = n_rep // n
        blocks = xr[:n_blocks * n].reshape(n_blocks, n, -1).sum(axis=1)
        p_over_n = np.mean(np.abs(blocks) ** 2, axis=0) / n   # per bin
        snr = np.maximum(p_over_n - sigma2, 0.0) / sigma2
        band = float(np.sum(np.maximum(p_over_n - sigma2, 0.0))
                     / np.sum(sigma2))
        curve.append({
            "n": int(n), "seconds": n * sym_s,
            "snr_db_median": float(10 * np.log10(np.median(snr) + 1e-12)),
            "snr_db_band": float(10 * np.log10(band + 1e-12)),
            "ideal_db": float(10 * np.log10(n)),
        })
    if curve:
        base = curve[0]["snr_db_band"]
        for row in curve:
            row["gain_db"] = row["snr_db_band"] - base
            row["loss_db"] = row["ideal_db"] - row["gain_db"]
    return {"freq_offset_hz": hz, "corr_ratio": est["corr_ratio"],
            "repeats_used": n_rep, "symbol_seconds": sym_s, "curve": curve}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+", type=Path)
    ap.add_argument("--fft-size", type=int, default=240)
    ap.add_argument("--repeats", type=int, default=None,
                    help="defaults to the value recorded in sounding.json")
    args = ap.parse_args(argv)

    for d in args.dirs:
        meta = json.loads((d / "sounding.json").read_text())
        repeats = args.repeats or meta["repeats"]
        print(f"\n=== {d.name}  ({meta['tx']} -> {meta['rx']}, "
              f"repeats={repeats}, fft={args.fft_size}, "
              f"symbol={args.fft_size / S.DESIGN_RATE * 1000:.1f}ms)")
        for entry in meta["trials"]:
            name = entry.get("capture_file") or f"capture{entry['trial']:02d}.npy"
            path = d / name
            if not path.exists():
                continue
            res = analyse_capture(np.load(path).astype(np.float64),
                                  args.fft_size, repeats)
            if res is None:
                print(f"  {name}: NOT LOCATED")
                continue
            if "error" in res:
                print(f"  {name}: {res['error']}")
                continue
            print(f"  {name}: drive={entry.get('drive', meta.get('drive'))} "
                  f"foff={res['freq_offset_hz']:+.2f}Hz "
                  f"corr={res['corr_ratio']:.3f}")
            print("       N   t(s)   band SNR   per-carrier   gain   ideal   loss")
            for r in res["curve"]:
                print(f"     {r['n']:3d}  {r['seconds']:5.2f}  "
                      f"{r['snr_db_band']:8.2f}  {r['snr_db_median']:11.2f}  "
                      f"{r['gain_db']:5.2f}  {r['ideal_db']:5.2f}  {r['loss_db']:5.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
