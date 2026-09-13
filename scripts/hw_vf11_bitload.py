"""Over-the-air test of per-carrier adaptive bit loading (VF11) on the FM bench.

Runs the loop a real adaptive link would run, in miniature:

  1. SOUND. Send one uniform-QPSK frame and let the receiver report the SNR it
     measured on each of the 49 carriers.
  2. PLAN. Turn that into a bits-per-carrier map.
  3. LOAD. Send VF11 frames using that map and see what fraction decode.

Each direction is sounded and planned separately, because the two legs of this
bench have genuinely different frequency responses -- ic705->ht and ht->ic705
do not have the same notches, and a map that suits one wastes the other. Being
able to run a different map each way is the point of bit loading, not a
complication of it.

The margin sweep is the measurement that matters. Textbook required-SNR
figures for these constellations proved about 4 dB optimistic on this path --
they predicted uniform 8PSK would work where it scored 0/10 -- so rather than
trusting a threshold table, this walks the whole map up and down in dB and
finds where throughput times reliability actually peaks. A map that doubles
the bit rate and halves the frame success rate has bought nothing.

VF9 runs first as a baseline in the same session, so the comparison is against
the channel as it is today rather than against a number from an earlier one.

Run: python scripts/hw_vf11_bitload.py
     python scripts/hw_vf11_bitload.py --margins 0,-2,-4 --trials 3
"""
import argparse
import sys
import time

import numpy as np

import bench
from whale.modes import vf11
from whale.modes.vf9 import VF9

CAPTURE_TAIL = 1.0
INTER_TRIAL = 0.8


def send_and_decode(mode, tx, rx, payload):
    stale = rx.snapshot_rx()
    rx.consume_rx(len(stale))
    tx.send(mode.encode(payload))
    time.sleep(CAPTURE_TAIL)
    return mode.decode(rx.snapshot_rx())


def sound(tx, rx, tx_name, rx_name, attempts=3):
    """Per-carrier SNR for this direction, from uniform-QPSK frames.

    Uses vf11.sound(), which measures against all ~200 payload symbols rather
    than the 4 header symbols the ordinary decode path reports. Measured, the
    header figure carries 2.7 dB of spread AND reads 3.1 dB optimistic -- it
    is fitted to the same symbols its residual is measured on -- so planning a
    map from it hands carriers constellations they cannot carry. Sounding this
    way cuts the spread to 0.8 dB and removes the bias.

    Averaged in the linear domain over several frames, since one keying's AGC
    excursion should not end up baked into the map.
    """
    payload = bytes(np.random.default_rng(7).integers(
        0, 256, VF9.chunk_size, dtype=np.uint8))
    seen = []
    for _ in range(attempts):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        tx.send(VF9.encode(payload))
        time.sleep(CAPTURE_TAIL)
        snr_db = vf11.sound(rx.snapshot_rx(), payload)
        if snr_db is not None:
            seen.append(10 ** (np.asarray(snr_db) / 10))
        time.sleep(INTER_TRIAL)
    if not seen:
        return None
    snr_db = 10 * np.log10(np.mean(seen, axis=0))
    print(f"  sounded {tx_name}->{rx_name} over {len(seen)} frames: "
          f"per-carrier SNR min/median/max = {snr_db.min():.1f}/"
          f"{np.median(snr_db):.1f}/{snr_db.max():.1f} dB")
    return snr_db


def trial_run(mode, tx, rx, tx_name, rx_name, trials, label):
    payload = bytes(np.random.default_rng(0x11).integers(
        0, 256, mode.chunk_size, dtype=np.uint8))
    good = 0
    for i in range(1, trials + 1):
        result = send_and_decode(mode, tx, rx, payload)
        ok = result.get("payload") == payload
        good += int(ok)
        print(f"    [{tx_name}->{rx_name} {label}] {i}/{trials}: "
              f"codewords={result.get('codewords_ok', 0)}/{mode.n_codewords} "
              f"crc={result.get('crc_ok')} match={ok} "
              f"snr={result.get('snr_db', float('nan')):.1f}dB")
        time.sleep(INTER_TRIAL)
    rate = good / trials
    goodput = mode.bits_per_second * rate
    print(f"    [{tx_name}->{rx_name} {label}] => {good}/{trials} "
          f"({rate * 100:.0f}%), {mode.bits_per_second:.0f} bps raw, "
          f"{goodput:.0f} bps goodput")
    return rate, goodput


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--margins", default="2,0,-2",
                    help="dB to shift every threshold; negative is more "
                         "aggressive (default: %(default)s)")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--sound-frames", type=int, default=2)
    ap.add_argument("--skip-baseline", action="store_true")
    args = ap.parse_args()

    margins = [float(x) for x in args.margins.split(",")]
    summary = {}

    with bench.radio_pair() as (t_ic705, t_ht):
        legs = ((t_ic705, t_ht, "ic705", "ht"), (t_ht, t_ic705, "ht", "ic705"))
        for tx, rx, tx_name, rx_name in legs:
            leg = f"{tx_name}->{rx_name}"
            print(f"\n=== {leg} ===")

            if not args.skip_baseline:
                rate, goodput = trial_run(VF9, tx, rx, tx_name, rx_name,
                                          args.trials, "vf9 QPSK baseline")
                summary[(leg, "vf9")] = (VF9.bits_per_second, rate, goodput)

            snr_db = sound(tx, rx, tx_name, rx_name, args.sound_frames)
            if snr_db is None:
                print(f"  sounding failed on {leg} -- skipping")
                continue

            for margin in margins:
                mode = vf11.from_snr(snr_db, margin)
                if mode.n_codewords < 1:
                    print(f"  margin {margin:+.0f}: map carries nothing, skipped")
                    continue
                print(f"  margin {margin:+.0f}: {mode.describe()}")
                rate, goodput = trial_run(mode, tx, rx, tx_name, rx_name,
                                          args.trials, f"margin{margin:+.0f}")
                summary[(leg, f"margin{margin:+.0f}")] = (
                    mode.bits_per_second, rate, goodput)

    print("\n== SUMMARY (goodput = raw bps x frame success) ==")
    for (leg, label), (raw, rate, goodput) in summary.items():
        print(f"  {leg:14s} {label:22s} {raw:6.0f} bps x {rate * 100:3.0f}% "
              f"= {goodput:6.0f} bps")

    legs_seen = {leg for leg, _ in summary}
    for leg in sorted(legs_seen):
        rows = {label: v for (l, label), v in summary.items() if l == leg}
        if rows:
            best = max(rows.items(), key=lambda kv: kv[1][2])
            print(f"  best on {leg}: {best[0]} at {best[1][2]:.0f} bps goodput")
    return 0


if __name__ == "__main__":
    sys.exit(main())
