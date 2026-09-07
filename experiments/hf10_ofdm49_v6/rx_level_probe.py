"""Is the hf10 receive path clipping, or merely scaled above 1.0?

evm_probe showed captures with RMS ~1.0 and peaks ~4.4 on a float32
input stream that nominally spans +/-1. Two readings have opposite
consequences:

  a) the audio device applies a gain > 1 and nothing is clipped -- the
     ">= 0.999" clip counter is then meaningless and the EVM floor lies
     elsewhere;
  b) the codec ADC is genuinely overdriven and hard-clipping the OFDM
     waveform's peaks -- which would be signal-proportional distortion,
     exactly matching an SNR that does not move over 46 dB of transmit
     drive, and the fix is a receive level setting, not a waveform.

An OFDM passband signal is Gaussian (kurtosis 3). Hard clipping truncates
the tails: kurtosis drops well below 3, the top of the amplitude
histogram piles up at a rail, and the same peak value repeats many times.
This probe transmits the ordinary OFDM frame at several transmit drive
levels, saves each raw capture, and reports those statistics over the
frame's active span only (silence excluded, so RMS and crest are real).
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
from experiments.hf10_ofdm49_v6 import ofdm49_v6 as ofdm49
from experiments.hf10_ofdm49_v6.evm_probe import analyse


def active_span(cap: np.ndarray, frac: float = 0.25) -> np.ndarray:
    """The part of the capture actually carrying the frame: samples whose
    short-term envelope is above `frac` of the capture's peak envelope."""
    if not cap.size:
        return cap
    win = 64
    env = np.convolve(np.abs(cap), np.ones(win) / win, mode="same")
    hot = np.where(env > frac * env.max())[0]
    if len(hot) < win:
        return cap
    return cap[hot[0]:hot[-1] + 1]


def level_stats(cap: np.ndarray) -> dict:
    span = active_span(cap)
    peak = float(np.max(np.abs(span)))
    rms = float(np.sqrt(np.mean(span ** 2)))
    # how many samples sit within 1% of the observed peak: a hard rail
    # collects far more than a Gaussian tail ever would
    near_rail = int(np.sum(np.abs(span) >= 0.99 * peak))
    kurt = float(np.mean((span / (rms + 1e-18)) ** 4))
    return {
        "span_samples": int(span.size),
        "peak": peak,
        "rms": rms,
        "crest_db": float(20 * np.log10(peak / (rms + 1e-18))),
        "kurtosis": kurt,          # 3.0 == Gaussian, << 3 == clipped
        "near_rail_samples": near_rail,
        "near_rail_fraction": near_rail / max(span.size, 1),
        "over_unity_fraction": float(np.mean(np.abs(span) > 1.0)),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--fft-size", type=int, default=240)
    ap.add_argument("--cp-len", type=int, default=60)
    ap.add_argument("--bps", type=int, default=2)
    ap.add_argument("--packet-bytes", type=int, default=600)
    ap.add_argument("--pilot-interval", type=int, default=20)
    ap.add_argument("--drive-scales", default="1.0,0.5,0.25,0.1,0.03")
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--seed", type=int, default=20260907)
    ap.add_argument("--capture-tail", type=float, default=1.0)
    ap.add_argument("--save-captures", action="store_true")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args(argv)

    scales = [float(s) for s in args.drive_scales.split(",")]
    bins = ofdm49.bins_in_band(args.fft_size)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out = args.output or (REPOSITORY_ROOT / "logs" / "mode_qualification" / "hf-ssb"
                          / "hf10" / f"{stamp}-rx-level-probe.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    cap_dir = out.parent / f"{stamp}-captures"
    if args.save_captures:
        cap_dir.mkdir(parents=True, exist_ok=True)

    records = []
    with bench.radio_pair(args.a, args.b, b_receive_only=True) as (tx, rx):
        for scale in scales:
            mode = ofdm49.OFDM49Mode(fft_size=args.fft_size, cp_len=args.cp_len,
                                     active_bins=tuple(bins), bits_per_symbol=args.bps,
                                     packet_bytes=args.packet_bytes,
                                     pilot_interval=args.pilot_interval,
                                     drive_scale=scale)
            for trial in range(1, args.trials + 1):
                stale = rx.snapshot_rx()
                rx.consume_rx(len(stale))
                rng = np.random.default_rng(np.random.SeedSequence([args.seed, trial]))
                payload = rng.integers(0, 256, mode.max_payload_bytes, dtype=np.uint8).tobytes()
                audio = mode.modulate(payload)
                tx.send(audio)
                time.sleep(args.capture_tail)
                cap = np.asarray(rx.snapshot_rx(), dtype=np.float64)
                rec = {"drive_scale": scale, "trial": trial}
                rec.update(level_stats(cap))
                res = mode.demodulate(cap, diagnostics=True)
                rec["synced"] = bool(res.get("synced"))
                if res.get("synced"):
                    rep = analyse(mode, res, payload)
                    rec["evm_snr_db"] = rep["overall_snr_db"]
                    rec["per_bin_snr_db"] = rep["per_bin_snr_db"]
                    rec["decoded"] = bool(res.get("payload") == payload)
                    rec["channel_snr_db"] = res.get("channel_snr_db")
                if args.save_captures:
                    f = cap_dir / f"scale{scale}_trial{trial}.npy"
                    np.save(f, cap.astype(np.float32))
                    rec["capture_file"] = f.name
                records.append(rec)
                print(f"  drive={scale:<5} t{trial}: peak={rec['peak']:.3f} "
                      f"rms={rec['rms']:.4f} crest={rec['crest_db']:.1f}dB "
                      f"kurt={rec['kurtosis']:.2f} rail={rec['near_rail_fraction']*100:.2f}% "
                      f"over1={rec['over_unity_fraction']*100:.1f}% "
                      f"evm_snr={rec.get('evm_snr_db', float('nan')):.1f}dB "
                      f"decoded={rec.get('decoded')}")
                time.sleep(0.4)

    out.write_text(json.dumps({"direction": f"{args.a}->{args.b}",
                               "config": {"fft_size": args.fft_size, "cp_len": args.cp_len,
                                          "bps": args.bps, "packet_bytes": args.packet_bytes,
                                          "pilot_interval": args.pilot_interval},
                               "records": records}, indent=1))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
