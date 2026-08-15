"""Single-frame over-the-air test: no Link/ARQ, no VARA server, no sockets.

Opens both radios directly in one process, starts continuous RX on both,
sends ONE afsk-modulated frame from ic705 -> ht, waits and tries to decode
it from ht's captured audio, then does the same ht -> ic705. This isolates
"can we get one frame across cleanly" from all the ARQ/threading/socket
machinery layered on top.

Run: python scripts/hw_smoke_single_frame.py
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from whale import afsk
from whale.transport import RadioTransport

PAYLOAD = (b"hello whalemodem " * 4)


def try_one_way(tx_name, tx, rx_name, rx, settle_s=2.0, listen_s=6.0):
    print(f"\n== {tx_name} -> {rx_name} ==")
    rx.snapshot_rx()  # clear whatever accumulated so far
    time.sleep(settle_s)
    rx.snapshot_rx()

    audio = afsk.modulate(PAYLOAD)
    print(f"   sending {len(PAYLOAD)} bytes, tx audio {len(audio)/afsk.SAMPLE_RATE:.2f}s")
    # Deliberately the production PTT margins rather than generous ones, so
    # this stays a real test of them (they are measured, not guessed -- see
    # whale/transport.py PTT_LEAD/PTT_TAIL).
    keyed = tx.send(audio)
    print(f"   keyed {keyed:.2f}s (PTT on -> PTT off)")

    print(f"   listening on {rx_name} for {listen_s}s...")
    time.sleep(listen_s)
    captured = rx.snapshot_rx()
    print(f"   captured {len(captured)/afsk.SAMPLE_RATE:.2f}s of audio, decoding...")
    result = afsk.demodulate(captured)
    print(f"   synced={result.get('synced')} confidence={result.get('confidence')}")
    if result.get("synced"):
        ok = result["payload"] == PAYLOAD
        print(f"   payload match: {ok}  (got {len(result['payload'])} bytes)")
        return ok
    else:
        print("   NOT SYNCED -- frame not found")
        return False


def main():
    print("opening radios...")
    ic705 = RadioTransport("ic705")
    ht = RadioTransport("ht")
    try:
        ic705.start_receiving()
        ht.start_receiving()
        print("warming up 3s...")
        time.sleep(3)

        r1 = try_one_way("ic705", ic705, "ht", ht)
        time.sleep(1)
        r2 = try_one_way("ht", ht, "ic705", ic705)

        print("\n== RESULTS ==")
        print(f"ic705 -> ht: {'OK' if r1 else 'FAIL'}")
        print(f"ht -> ic705: {'OK' if r2 else 'FAIL'}")
        sys.exit(0 if (r1 and r2) else 1)
    finally:
        ic705.close()
        ht.close()


if __name__ == "__main__":
    main()
