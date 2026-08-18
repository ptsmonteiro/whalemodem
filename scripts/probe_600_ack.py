"""Focused bench probe for one specific failure: whale/link.py's short
PT_DATA_ACK frame (1 body byte -> 2-byte payload, ~0.28s airtime at
PROFILE_600) not getting decoded on the STA2(HT) -> STA1(IC-705) leg, the
weaker of the two paths per scripts/measure_snr.py (~12.6 dB vs ~14.8 dB).

Unlike scripts/hw_smoke_link.py (full connect/ARQ/disconnect), this only
transmits the one frame shape in question, repeatedly, from HT to IC-705,
and for each capture prints the demodulator's internal numbers (correlation
peak, noise floor, confidence, symbols available vs needed) instead of just
pass/fail -- so a threshold or framing problem is visible directly, and each
capture is also saved to disk so parameter sweeps (confidence_threshold,
etc.) can be re-run offline against already-captured audio instead of
re-transmitting.

Run: python scripts/probe_600_ack.py
     python scripts/probe_600_ack.py --trials 10
     python scripts/probe_600_ack.py --replay capture_dir  # re-analyze, no TX
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
from scipy.signal import correlate

import bench
from whale import afsk, framing
from whale.link import PT_DATA_ACK
from whale.transport import SAMPLE_RATE

SETTLE_AFTER_PTT = 0.3
CAPTURE_TAIL = 1.0  # extra seconds captured after TX ends, in case the frame arrives late


def _demod_debug(audio, profile):
    """Reimplements whale.afsk.demodulate's search but returns every
    intermediate number instead of just the verdict, so a near-miss can be
    diagnosed instead of just observed."""
    sps = round(SAMPLE_RATE / profile.baud)
    audio = np.asarray(audio, dtype=np.float64)
    diff = afsk._tone_energy_diff(audio, SAMPLE_RATE, sps, profile.freq0, profile.freq1)
    template = afsk._sync_template(sps, SAMPLE_RATE, profile.freq0, profile.freq1)
    if len(diff) < len(template):
        return {"ok": False, "reason": "audio shorter than sync template"}

    # Same normalised measure demodulate() uses -- a raw correlation peak
    # scored against the buffer's median is not comparable between one
    # capture and the next, which is most of what made this probe's
    # original numbers hard to read.
    ncc = afsk._normalised_correlation(diff, template)
    corr = correlate(diff, template, mode="valid", method="fft")
    i_star = int(np.argmax(ncc))
    confidence = float(ncc[i_star])

    n_sync = len(framing.sync_bits(profile.baud))
    first_index = i_star + (sps - 1)
    max_symbols = (len(diff) - 1 - first_index) // sps + 1

    info = {
        "ok": True, "confidence": confidence, "peak": float(corr[i_star]),
        "raw_peak_over_median": float(np.max(corr)) / float(np.median(np.abs(corr)) or 1.0),
        "sync_locked": confidence >= profile.confidence_threshold,
        "max_symbols": max_symbols,
        "symbols_needed_for_length_field": n_sync + framing.LENGTH_FIELD_BITS,
    }
    if max_symbols < n_sync + framing.LENGTH_FIELD_BITS:
        info["verdict"] = "not enough symbols captured to even read the length field"
        return info

    credible_bits = afsk.max_credible_frame_bits(profile.baud)
    num_symbols = min(max_symbols, credible_bits)
    sample_indices = first_index + sps * np.arange(num_symbols)
    decoded_bits = (diff[sample_indices] > 0).astype(int).tolist()
    payload = framing.parse_frame_bits(decoded_bits[n_sync:])
    length = framing.declared_length(decoded_bits[n_sync:])
    total_bits_needed = n_sync + framing.frame_bits_for_length(length)
    info["declared_length"] = length
    info["total_bits_needed"] = total_bits_needed
    info["payload"] = payload
    info["verdict"] = "DECODED" if payload is not None else (
        "implausible declared length -- dead sync, not a frame"
        if total_bits_needed > credible_bits
        else "frame complete but CRC/parse failed" if max_symbols >= total_bits_needed
        else "still arriving (not enough symbols for declared length yet)")
    return info


def _run_trial(tx, rx, payload, profile, trial_dir, index):
    # snapshot_rx() does NOT consume the buffer -- it just returns
    # everything captured since the last read (up to RX_BUFFER_SECONDS).
    # Actually flush stale audio from prior trials by consuming what we
    # just read, or every trial after the first accumulates the whole
    # session's audio instead of just this trial's frame.
    stale = rx.snapshot_rx()
    rx.consume_rx(len(stale))
    tx_audio = afsk.modulate(payload, profile=profile)
    frame_seconds = len(tx_audio) / SAMPLE_RATE
    t0 = time.time()
    tx.send(tx_audio)
    tx_wall = time.time() - t0
    time.sleep(CAPTURE_TAIL)
    captured = rx.snapshot_rx()

    if trial_dir is not None:
        np.save(trial_dir / f"trial_{index:02d}.npy", captured)

    print(f"\n--- trial {index}: TX keyed {tx_wall:.2f}s (frame airtime {frame_seconds:.2f}s), "
          f"captured {len(captured)} samples ({len(captured) / SAMPLE_RATE:.2f}s) ---")
    _analyze(captured, profile)


def _analyze(captured, profile):
    info = _demod_debug(captured, profile)
    if not info["ok"]:
        print(f"  {info['reason']}")
        return
    print(f"  confidence={info['confidence']:.1f} (threshold={profile.confidence_threshold}) "
          f"peak={info['peak']:.3g} raw_peak_over_median={info['raw_peak_over_median']:.3g} "
          f"sync_locked={info['sync_locked']}")
    print(f"  max_symbols={info['max_symbols']} "
          f"(need {info['symbols_needed_for_length_field']} for length field)")
    if "declared_length" in info:
        print(f"  declared_length={info['declared_length']} "
              f"total_bits_needed={info['total_bits_needed']}")
    print(f"  verdict: {info['verdict']}")

    # Sweep confidence_threshold offline against this same capture -- cheap,
    # no retransmission -- to see how far off a threshold change would need
    # to go to save this specific capture.
    if info["ok"] and not info["sync_locked"]:
        for thr in (3.0, 2.5, 2.0, 1.5):
            if info["confidence"] >= thr:
                print(f"  (would sync-lock if confidence_threshold were <= {thr})")
                break
        else:
            print("  (confidence too low to sync-lock even at threshold 1.5 -- not a threshold problem)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--save-dir", default=None, help="directory to save raw captures for offline replay")
    ap.add_argument("--replay", default=None, help="re-analyze .npy captures in this directory instead of transmitting")
    ap.add_argument("--profile", default=afsk.PROFILE_600.name, choices=sorted(p.name for p in afsk.PROFILES))
    args = ap.parse_args()
    profile = next(p for p in afsk.PROFILES if p.name == args.profile)

    # The exact frame shape link.py's DATA_ACK actually sends: [PT_DATA_ACK, seq].
    payload = bytes([PT_DATA_ACK, 0x00])

    if args.replay is not None:
        replay_dir = Path(args.replay)
        for f in sorted(replay_dir.glob("trial_*.npy")):
            captured = np.load(f)
            print(f"\n--- replay {f.name}: {len(captured)} samples ({len(captured) / SAMPLE_RATE:.2f}s) ---")
            _analyze(captured, profile)
        return 0

    trial_dir = None
    if args.save_dir is not None:
        trial_dir = Path(args.save_dir)
        trial_dir.mkdir(parents=True, exist_ok=True)

    print(f"profile={profile.name} tones={profile.freq0:.0f}/{profile.freq1:.0f} Hz "
          f"confidence_threshold={profile.confidence_threshold}")
    print(f"payload under test: PT_DATA_ACK frame, {len(payload)} bytes "
          f"({afsk.frame_seconds(len(payload), profile):.2f}s nominal airtime)")
    with bench.radio_pair("ht", "ic705") as (t_ht, t_ic705):
        for i in range(1, args.trials + 1):
            _run_trial(t_ht, t_ic705, payload, profile, trial_dir, i)
            time.sleep(1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
