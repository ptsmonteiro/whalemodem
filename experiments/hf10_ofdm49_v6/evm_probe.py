"""Decompose the hf10 OFDM49 error floor on the real bench path.

The v6 record (4332 bps) is limited by a ~15 dB effective SNR, and the
hf4 TX-volume sweep of 2026-09-07 showed received SNR is flat to within
~1 dB over a 46 dB range of transmit drive. A link whose SNR does not
respond to transmit level is not thermal-noise limited: the error is
proportional to the signal (an EVM floor). Turning anything up cannot
fix it, and 32-QAM (~19 dB) / 64-QAM (~25 dB) cannot work until it is
lifted. This probe measures WHERE that floor comes from.

It transmits one long, deliberately robust QPSK frame (so the truth
symbols are recoverable even when the error is large), then compares the
equalized symbols against the known transmitted symbols and splits the
error into impairments that have different fixes:

  1. per-subcarrier SNR    -> SSB filter skirts; fix = bit loading / drop bins
  2. EVM vs symbol index   -> sample-clock offset drift; fix = resampling
  3. common phase error    -> oscillator phase noise; fix = per-symbol CPE
  4. residual after 1-3    -> ICI / nonlinearity; the true floor

Usage:
    python experiments/hf10_ofdm49_v6/evm_probe.py --packet-bytes 1200
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import numpy as np

import bench
from whale.dsp import bits as _bits
from whale.phy import ofdm49 as ofdm49


def truth_symbols(mode, payload: bytes) -> np.ndarray:
    """Regenerate exactly the data symbols modulate() put on the data bins."""
    _, coded = mode.pack_and_encode_bits(payload)
    whitener = _bits.pn_bits(len(coded), ofdm49.WHITENER_SEED)
    data_bits = coded ^ whitener
    needed = mode.n_data_ofdm_symbols * mode.bits_per_ofdm_symbol
    if len(data_bits) < needed:
        data_bits = np.concatenate([data_bits,
                                    np.zeros(needed - len(data_bits), dtype=np.uint8)])
    syms = ofdm49.bits_to_symbols(data_bits, mode.bits_per_symbol)
    return syms.reshape(mode.n_data_ofdm_symbols, mode.n_data_bins)


def analyse(mode, result, payload) -> dict:
    rx = np.asarray(result["equalized_symbols"])       # (n_sym, n_data_bins)
    tx = truth_symbols(mode, payload)
    n = min(len(rx), len(tx))
    rx, tx = rx[:n], tx[:n]
    bins = np.asarray(mode.active_bins)[mode._data_idx].astype(float)

    err = rx - tx
    ref = float(np.mean(np.abs(tx) ** 2))

    def snr_db(e):
        return float(10 * np.log10(ref / (np.mean(np.abs(e) ** 2) + 1e-18)))

    # 1. per-subcarrier
    per_bin = 10 * np.log10(ref / (np.mean(np.abs(err) ** 2, axis=0) + 1e-18))
    # 2. per-symbol (drift signature)
    per_sym = 10 * np.log10(ref / (np.mean(np.abs(err) ** 2, axis=1) + 1e-18))

    # 3. common phase error per symbol, and what removing it recovers
    cpe = np.angle(np.sum(rx * np.conj(tx), axis=1))
    err_cpe = rx * np.exp(-1j * cpe)[:, None] - tx

    # 4. per-symbol phase slope across frequency == symbol timing error.
    #    A sample-clock offset makes this slope grow linearly with symbol
    #    index; fit it and convert to ppm.
    slopes = np.array([np.polyfit(bins, np.unwrap(np.angle(rx[i] * np.conj(tx[i]))), 1)[0]
                       for i in range(n)])
    slope_growth = np.polyfit(np.arange(n, dtype=float), slopes, 1)[0]
    # phase = 2*pi*bin*tau/fft_size  ->  tau (samples) = slope*fft_size/(2*pi)
    tau_per_symbol = slope_growth * mode.fft_size / (2 * np.pi)
    ppm = tau_per_symbol / mode.symbol_len * 1e6

    # residual once CPE and per-symbol timing slope are both removed
    corr = rx * np.exp(-1j * (cpe[:, None] + slopes[:, None] * bins[None, :]))
    err_res = corr - tx

    return {
        "n_symbols": int(n),
        "frame_seconds": mode.frame_seconds(),
        "overall_snr_db": snr_db(err),
        "after_cpe_removal_db": snr_db(err_cpe),
        "after_cpe_and_timing_db": snr_db(err_res),
        "per_bin_snr_db": [float(v) for v in per_bin],
        "per_bin_hz": [float(b) * ofdm49.DESIGN_RATE / mode.fft_size for b in bins],
        "per_symbol_snr_db": [float(v) for v in per_sym],
        "first_quarter_snr_db": float(np.mean(per_sym[: max(n // 4, 1)])),
        "last_quarter_snr_db": float(np.mean(per_sym[-max(n // 4, 1):])),
        "cpe_rms_deg": float(np.degrees(np.std(cpe))),
        "timing_drift_samples_per_symbol": float(tau_per_symbol),
        "clock_offset_ppm": float(ppm),
        "preamble_per_bin_snr_db": result.get("per_bin_snr_db"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--fft-size", type=int, default=240)
    ap.add_argument("--cp-len", type=int, default=60)
    ap.add_argument("--bps", type=int, default=2)
    ap.add_argument("--packet-bytes", type=int, default=1200)
    ap.add_argument("--pilot-interval", type=int, default=20)
    ap.add_argument("--drive-scale", type=float, default=1.0)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260907)
    ap.add_argument("--capture-tail", type=float, default=1.0)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args(argv)

    bins = ofdm49.bins_in_band(args.fft_size)
    mode = ofdm49.OFDM49Mode(fft_size=args.fft_size, cp_len=args.cp_len,
                             active_bins=tuple(bins), bits_per_symbol=args.bps,
                             packet_bytes=args.packet_bytes,
                             pilot_interval=args.pilot_interval,
                             drive_scale=args.drive_scale)
    print(f"probe: fft={mode.fft_size} cp={mode.cp_len} bins={mode.n_active} "
          f"bps={mode.bits_per_symbol} payload={mode.max_payload_bytes}B "
          f"symbols={mode.n_data_ofdm_symbols} frame={mode.frame_seconds():.3f}s "
          f"crest={mode.crest_factor_db():.1f}dB")

    reports = []
    with bench.radio_pair(args.a, args.b, b_receive_only=True) as (tx, rx):
        for trial in range(1, args.trials + 1):
            stale = rx.snapshot_rx()
            rx.consume_rx(len(stale))
            rng = np.random.default_rng(np.random.SeedSequence([args.seed, trial]))
            payload = rng.integers(0, 256, mode.max_payload_bytes, dtype=np.uint8).tobytes()
            audio = mode.modulate(payload)
            keyed = tx.send(audio)
            time.sleep(args.capture_tail)
            cap = np.asarray(rx.snapshot_rx(), dtype=np.float64)
            res = mode.demodulate(cap, diagnostics=True)
            if not res.get("synced"):
                print(f"  {trial}: NO SYNC (conf={res['confidence']:.3f})")
                continue
            rep = analyse(mode, res, payload)
            rep.update(trial=trial, keyed_seconds=keyed,
                       capture_peak=float(np.max(np.abs(cap))),
                       capture_rms=float(np.sqrt(np.mean(cap ** 2))),
                       clipped=int(np.sum(np.abs(cap) >= 0.999)),
                       decoded=bool(res.get("payload") == payload),
                       channel_snr_db=res.get("channel_snr_db"))
            reports.append(rep)
            pb = np.array(rep["per_bin_snr_db"])
            print(f"  {trial}: decoded={rep['decoded']} peak={rep['capture_peak']:.3f} "
                  f"clip={rep['clipped']} evm_snr={rep['overall_snr_db']:.1f}dB "
                  f"(+cpe {rep['after_cpe_removal_db']:.1f} "
                  f"+timing {rep['after_cpe_and_timing_db']:.1f}) "
                  f"bin min/med/max={pb.min():.1f}/{np.median(pb):.1f}/{pb.max():.1f} "
                  f"drift={rep['clock_offset_ppm']:.1f}ppm "
                  f"cpe_rms={rep['cpe_rms_deg']:.1f}deg "
                  f"sym q1/q4={rep['first_quarter_snr_db']:.1f}/"
                  f"{rep['last_quarter_snr_db']:.1f}dB")
            time.sleep(0.5)

    out = args.output or (REPOSITORY_ROOT / "logs" / "mode_qualification" / "hf-ssb" / "hf10"
                          / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-evm-probe.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"config": {"fft_size": args.fft_size, "cp_len": args.cp_len,
                                          "bps": args.bps, "packet_bytes": args.packet_bytes,
                                          "pilot_interval": args.pilot_interval,
                                          "drive_scale": args.drive_scale},
                               "direction": f"{args.a}->{args.b}",
                               "trials": reports}, indent=1))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
