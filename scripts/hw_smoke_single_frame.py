"""Single-frame over-the-air test: no Link/ARQ, no VARA server, no sockets.

Opens both radios directly in one process, starts continuous RX on both,
sends ONE frame from ic705 -> ht, waits and tries to decode
it from ht's captured audio, then does the same ht -> ic705. This isolates
"can we get one frame across cleanly" from all the ARQ/threading/socket
machinery layered on top.

Run: python scripts/hw_smoke_single_frame.py
    python scripts/hw_smoke_single_frame.py --profile fm1
    python scripts/hw_smoke_single_frame.py --profile vf14-16
    python scripts/hw_smoke_single_frame.py --profile vf14-8 --payload-bytes 11
"""

import argparse
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

import bench
from whale import afsk
from whale.modes import vf14

PAYLOAD = (b"hello whale " * 4)

PROFILES_BY_NAME = {f"fm{p.mode_id}": p for p in afsk.PROFILES}
# Waveform modes, driven through their own encode/decode.
PROFILES_BY_NAME.update({m.name: m for m in vf14.PROFILES.values()})


def try_one_way(tx_name, tx, rx_name, rx, profile, payload=PAYLOAD,
                settle_s=2.0, listen_s=6.0):
    print(f"\n== {tx_name} -> {rx_name} ({profile.name}) ==")
    rx.snapshot_rx()  # clear whatever accumulated so far
    time.sleep(settle_s)
    rx.snapshot_rx()

    cpfsk = isinstance(profile, afsk.Profile)
    audio = (afsk.modulate(payload, profile=profile) if cpfsk
             else profile.encode(payload))
    print(f"   sending {len(payload)} bytes, tx audio {len(audio)/afsk.SAMPLE_RATE:.2f}s")
    # Deliberately the production PTT margins rather than generous ones, so
    # this stays a real test of them (they are measured, not guessed -- see
    # whale/transport.py PTT_LEAD/PTT_TAIL).
    keyed = tx.send(audio)
    print(f"   keyed {keyed:.2f}s (PTT on -> PTT off)")

    print(f"   listening on {rx_name} for {listen_s}s...")
    time.sleep(listen_s)
    captured = rx.snapshot_rx()
    print(f"   captured {len(captured)/afsk.RX_SAMPLE_RATE:.2f}s of audio, decoding...")
    if cpfsk:
        result = afsk.demodulate(captured, profile=profile, sample_rate=afsk.RX_SAMPLE_RATE)
    else:
        result = profile.decode(captured)
    print(f"   synced={result.get('synced')} confidence={result.get('confidence')}"
          + (f" tone_snr_db={result['tone_snr_db']:.1f}" if "tone_snr_db" in result else ""))
    if result.get("synced"):
        got = result.get("payload")
        ok = got == payload
        print(f"   payload match: {ok}  (got {len(got) if got is not None else 'no'} bytes)")
        return ok
    else:
        print("   NOT SYNCED -- frame not found")
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="fm0", choices=sorted(PROFILES_BY_NAME),
                    help="which CPFSK profile or waveform mode to put on the air")
    ap.add_argument("--payload-bytes", type=int, default=None,
                    help=f"payload size (default {len(PAYLOAD)}; vf14 carries 0-64, "
                         "and 11 or fewer uses its short grid)")
    args = ap.parse_args()
    profile = PROFILES_BY_NAME[args.profile]
    payload = (PAYLOAD if args.payload_bytes is None
               else (PAYLOAD * 8)[:args.payload_bytes])
    if args.payload_bytes is not None and args.payload_bytes > len(PAYLOAD * 8):
        ap.error("--payload-bytes is too large")
    listen_s = 6.0
    if not isinstance(profile, afsk.Profile):
        # send() blocks until PTT-off, so only the RX path delay is left; a
        # long listen pushes the frame start out of the 10 s RX buffer.
        listen_s = 2.0

    with bench.radio_pair(warmup=3.0) as (ic705, ht):
        r1 = try_one_way("ic705", ic705, "ht", ht, profile, payload, listen_s=listen_s)
        time.sleep(1)
        r2 = try_one_way("ht", ht, "ic705", ic705, profile, payload, listen_s=listen_s)

        print("\n== RESULTS ==")
        print(f"ic705 -> ht: {'OK' if r1 else 'FAIL'}")
        print(f"ht -> ic705: {'OK' if r2 else 'FAIL'}")
    sys.exit(0 if (r1 and r2) else 1)


if __name__ == "__main__":
    main()
