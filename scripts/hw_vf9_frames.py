"""Over-the-air test of the VF9 OFDM mode on the real FM bench.

Sends whole VF9 frames -- framing, LDPC, CRC and all -- in both directions and
reports whether the payload came back byte-for-byte, how many of the 30 LDPC
codewords decoded, and what per-carrier SNR the receiver measured. Unlike
scripts/measure_ofdm_fm_burst.py, which probes the channel with known symbols,
this exercises the mode exactly as whale/link.py would drive it.

Both directions always, and the worst one is the answer: the two legs of this
bench differ materially, and a mode that works one way is not a mode.

Run: python scripts/hw_vf9_frames.py
     python scripts/hw_vf9_frames.py --trials 5 --payload 512
"""
import argparse
import dataclasses
import sys
import time

import numpy as np

import bench
from whale.modes.vf9 import (CARRIER_BINS, CARRIER_SPACING_HZ, FEC_RATE,
                             N_CARRIERS, VF9, VF10)

MODES = {"vf9": VF9, "vf10": VF10}

CAPTURE_TAIL = 1.0
INTER_TRIAL = 1.0
# Consecutive failures after which a direction is abandoned -- see the same
# constant in scripts/sweep_modes.py. A mode that has missed this many in a
# row will not clear the bar, and the rest is airtime spent confirming it.
ABORT_AFTER = 3


def run_trial(mode, tx, rx, tx_name, rx_name, payload, index, trials,
              save_prefix=None):
    stale = rx.snapshot_rx()
    rx.consume_rx(len(stale))

    audio = mode.encode(payload)
    under0, over0 = tx.tx_underflows, rx.rx_overflows
    t0 = time.time()
    tx.send(audio)
    keyed = time.time() - t0
    # A transmit underrun plays a gap into the middle of a frame: the header
    # goes out clean and everything after it is corrupted, which reads as
    # "good confidence, good SNR, zero codewords" and is indistinguishable
    # from a channel problem unless you look here.
    under = tx.tx_underflows - under0
    over = rx.rx_overflows - over0
    time.sleep(CAPTURE_TAIL)
    captured = rx.snapshot_rx()

    if save_prefix:
        path = f"{save_prefix}_{mode.name}_{tx_name}_to_{rx_name}_{index}.npz"
        np.savez(path, captured=captured, payload=np.frombuffer(payload,
                                                                dtype=np.uint8))
        print(f"    saved {path}")

    result = mode.decode(captured)
    ok = result.get("payload") == payload
    snr = result.get("snr_db")
    carrier = result.get("carrier_snr_db")
    detail = ""
    if carrier is not None:
        detail = (f" per-carrier {carrier.min():.1f}/{np.median(carrier):.1f}/"
                  f"{carrier.max():.1f} dB")
    print(f"  [{tx_name}->{rx_name}] trial {index}/{trials}: keyed={keyed:.2f}s "
          f"captured={len(captured)} conf={result['confidence']:.3f} "
          f"codewords={result.get('codewords_ok', 0)}/{mode.n_codewords} "
          f"crc={result.get('crc_ok')} match={ok}"
          f"{f' snr={snr:.1f}dB' if snr is not None else ''}{detail}"
          f"{f'  TX_UNDERFLOW={under}' if under else ''}"
          f"{f'  RX_OVERFLOW={over}' if over else ''}")
    return ok, result


def run_direction(mode, tx, rx, tx_name, rx_name, payload, trials,
                  save_prefix=None, abort_after=ABORT_AFTER):
    good = 0
    run = 0
    consecutive_failures = 0
    for i in range(1, trials + 1):
        ok, _ = run_trial(mode, tx, rx, tx_name, rx_name, payload, i, trials,
                          save_prefix)
        good += int(ok)
        run = i
        consecutive_failures = 0 if ok else consecutive_failures + 1
        if abort_after and consecutive_failures >= abort_after:
            print(f"    stopping this direction: {consecutive_failures} "
                  f"consecutive failures")
            break
        time.sleep(INTER_TRIAL)
    rate = good / run
    throughput = len(payload) * 8 / mode.airtime(len(payload))
    print(f"  [{tx_name}->{rx_name}] => {good}/{run} frames "
          f"({rate * 100:.0f}%), {throughput:.0f} bps when it lands")
    return rate


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--abort-after", type=int, default=ABORT_AFTER, metavar="N",
                    help="abandon a direction after N consecutive failures "
                         "(0 disables; default: %(default)s)")
    ap.add_argument("--mode", default="vf9", choices=["vf9", "vf10", "both"])
    ap.add_argument("--payload", type=int, default=None,
                    help="payload bytes (default: the mode's full chunk)")
    ap.add_argument("--direction", choices=["both", "ic705->ht", "ht->ic705"],
                    default="both")
    ap.add_argument("--lead", default=None,
                    help="comma-separated lead-in seconds to sweep, e.g. "
                         "1.0,0.5,0.3,0.2 -- the squelch/AGC settling budget")
    ap.add_argument("--save", default=None,
                    help="prefix to save each capture as .npz for offline debugging")
    args = ap.parse_args()

    chosen = list(MODES.values()) if args.mode == "both" else [MODES[args.mode]]
    if args.lead:
        chosen = [dataclasses.replace(m, lead_in_seconds=float(x))
                  for m in chosen for x in args.lead.split(",")]
    rng = np.random.default_rng(0x5F9)
    results = {}

    with bench.radio_pair() as (t_ic705, t_ht):
        for mode in chosen:
            size = mode.chunk_size if args.payload is None else args.payload
            payload = bytes(rng.integers(0, 256, size, dtype=np.uint8))
            constellation = {2: "QPSK", 3: "8PSK"}[mode.bits_per_carrier]
            print(f"\n=== {mode.name}: {N_CARRIERS} carriers "
                  f"{CARRIER_BINS[0] * CARRIER_SPACING_HZ:.0f}-"
                  f"{CARRIER_BINS[-1] * CARRIER_SPACING_HZ:.0f} Hz, "
                  f"{constellation}, LDPC {FEC_RATE} ===")
            print(f"lead-in {mode.lead_in_seconds:.2f}s, frame "
                  f"{mode.airtime(size):.2f}s, payload {size} B, "
                  f"{size * 8 / mode.airtime(size):.0f} bps effective")
            rates = {}
            if args.direction in ("both", "ic705->ht"):
                rates["ic705->ht"] = run_direction(mode, t_ic705, t_ht,
                                                   "ic705", "ht", payload,
                                                   args.trials, args.save,
                                                   args.abort_after)
            if args.direction in ("both", "ht->ic705"):
                rates["ht->ic705"] = run_direction(mode, t_ht, t_ic705,
                                                   "ht", "ic705", payload,
                                                   args.trials, args.save,
                                                   args.abort_after)
            results[mode] = rates

    print("\n== RESULTS ==")
    worst_overall = 1.0
    for mode, rates in results.items():
        worst = min(rates.values())
        worst_overall = min(worst_overall, worst)
        detail = "  ".join(f"{n} {r * 100:.0f}%" for n, r in rates.items())
        print(f"  {mode.name} lead={mode.lead_in_seconds:.2f}s "
              f"({mode.bits_per_second:.0f} bps): {detail}"
              f"  -> worst {worst * 100:.0f}%")
    return 0 if results and worst_overall >= 0.8 else 1


if __name__ == "__main__":
    sys.exit(main())
