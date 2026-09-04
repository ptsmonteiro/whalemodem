"""Real-hardware integration test for the newly-promoted, production-wired
`whale.modes.hf4_mode.HF4` mode.

This is deliberately NOT a re-run of `experiments/hf13_fast_sync_v1`'s own
hardware validation (see its RESULTS.md) -- that experiment already proved
`sc_fast.SingleCarrierMode`'s PHY correctness and real-hardware equivalence
with the original `sc.py` sync search. What this script checks is the
*promotion* itself: that `whale/modes/hf4_mode.py`'s `Hf4Mode`/`Hf4Codec`
glue -- mode_id byte, `encode`/`decode` signatures, `chunk_size`/framing
arithmetic, registry wiring in `whale/mode_qualification.py` -- carries a
frame through byte-for-byte when driven exactly the way the real link
framework would drive it (`mode.encode(payload)` -> `tx.send()` ->
`rx.snapshot_rx()` -> `mode.decode(audio)`), the same call shape
`scripts/sweep_modes.py` uses for every other registered mode. A wiring bug
introduced during promotion (e.g. a framing-size mismatch or a broken
mode_id) would show up here even though hf13's own PHY-level tests already
pass.

Each trial reports raw (pre-CRC, hard-decision) BER against the full frame
(length+payload+CRC) ground-truth bit stream, using the diagnostic
`raw_bits` field `sc_fast.SingleCarrierMode.demodulate()` adds to its result
dict. HF4 has no FEC, so post-FEC BER is always None/null -- never
fabricated.

SAFETY: IC-705 must never transmit. This script only ever calls `.send()`
on transport_a (ic7300 by default); there is deliberately no code path here
that calls transport_b's `.send()`, and --direction is hardcoded to "ab".

Run (from the repository root):
    python scripts/hf4_promotion_hardware_test.py --trials 12
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np

import bench
from whale import framing, mode_qualification
from experiments.hf5_8psk_4k import sc  # read-only: ground-truth packing only
from whale.modes.hf4_mode import HF4_PHY  # read-only: packet_bytes for ground truth

DEFAULT_TRIALS = 12
DEFAULT_CAPTURE_TAIL = 1.0
DEFAULT_INTER_TRIAL = 0.5


def _ground_truth_bits(packet_bytes: int, payload: bytes) -> np.ndarray:
    """Same plain (pre-whitening) packet bit stream sc_fast.py's raw_bits
    is compared against -- see experiments/hf13_fast_sync_v1/hardware_test.py's
    identically-named helper for the reasoning; reused read-only here so
    both scripts compute BER the same way."""
    packet = sc._pack_packet(payload, packet_bytes)
    return np.unpackbits(np.frombuffer(packet, dtype=np.uint8))


def _compute_raw_ber(truth_bits: np.ndarray, rx_bits) -> dict:
    if rx_bits is None:
        return {"ber": None, "bit_errors": None, "bits_compared": None}
    rx_bits = np.asarray(rx_bits)
    n = min(len(truth_bits), len(rx_bits))
    if n == 0:
        return {"ber": None, "bit_errors": None, "bits_compared": None}
    errors = int(np.sum(truth_bits[:n] != rx_bits[:n]))
    return {"ber": errors / n, "bit_errors": errors, "bits_compared": n}


def run_direction(tx, rx, mode, direction, trials, seed, *, capture_tail, inter_trial):
    payload_bytes = mode.chunk_size + framing.AIR_HEADER_BYTES
    print(f"\n  {direction}: {trials} x {payload_bytes} B "
          f"(mode={mode.name} id={mode.mode_id}, production-registered)")
    records = []
    for trial in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))

        rng = np.random.default_rng(np.random.SeedSequence([seed, mode.mode_id, trial]))
        payload = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()

        # Exactly the call shape the real link framework uses: mode.encode
        # / tx.send / rx.snapshot_rx / mode.decode, through the
        # registry-resolved production Hf4Mode instance -- not sc_fast
        # called directly.
        audio = mode.encode(payload)
        keyed = tx.send(audio)
        time.sleep(capture_tail)
        captured = rx.snapshot_rx()

        t0 = time.perf_counter()
        result = mode.decode(captured)
        demod_seconds = time.perf_counter() - t0

        decoded_payload = result.get("payload")
        decoded = decoded_payload == payload
        if decoded:
            outcome = "decoded"
        elif not result.get("synced"):
            outcome = "no_sync"
        elif not result.get("crc_ok"):
            outcome = "crc_fail"
        else:
            outcome = "payload_mismatch"

        # Hf4Codec.encode() forwards `payload` 1:1 into sc_fast's modulate(),
        # which packs it into a fixed-size (HF4_PHY.packet_bytes) frame body
        # -- not `payload_bytes` (the link-level payload length); the
        # ground-truth bit stream is over that same fixed-size packing.
        truth_bits = _ground_truth_bits(HF4_PHY.packet_bytes, payload)
        raw_ber_info = _compute_raw_ber(truth_bits, result.get("raw_bits"))

        net_bps = (payload_bytes * 8) / mode.airtime(payload_bytes) if decoded else 0.0
        record = {
            "trial": trial, "direction": direction, "mode_id": mode.mode_id,
            "mode_name": mode.name, "payload_bytes": payload_bytes,
            "keyed_seconds": keyed, "rx_samples_12k": len(captured),
            "demod_seconds": demod_seconds,
            "outcome": outcome, "confidence": result.get("confidence"),
            "freq_offset_hz": result.get("freq_offset_hz"),
            "channel_snr_db": result.get("channel_snr_db"),
            "pilot_blocks": result.get("pilot_blocks"),
            "net_bps": net_bps,
            "raw_ber": raw_ber_info["ber"], "raw_bit_errors": raw_ber_info["bit_errors"],
            "raw_bits_compared": raw_ber_info["bits_compared"],
            # No FEC in this mode: never fabricate a post-FEC BER value.
            "post_fec_ber": None,
        }
        records.append(record)
        conf = record["confidence"]
        conf_text = "n/a" if conf is None else f"{conf:.3f}"
        snr = record["channel_snr_db"]
        snr_text = "" if snr is None else f" snr={snr:.1f}dB"
        foff = record["freq_offset_hz"]
        foff_text = "" if foff is None else f" foff={foff:.2f}Hz"
        raw_ber = record["raw_ber"]
        raw_ber_text = ("raw_ber=n/a" if raw_ber is None
                         else f"raw_ber={raw_ber:.5f}({record['raw_bit_errors']}/{record['raw_bits_compared']})")
        print(f"    {trial}/{trials}: keyed={keyed:.2f}s rx={len(captured)} "
              f"demod={demod_seconds*1000:.0f}ms conf={conf_text}{snr_text}{foff_text} "
              f"{raw_ber_text} post_fec_ber=null {outcome}")
        if trial != trials:
            time.sleep(inter_trial)
    return records


def main(argv=None, *, pair_factory=bench.radio_pair):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ic7300")
    ap.add_argument("--b", default="ic705")
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--direction", choices=("ab",), default="ab",
                     help="ic7300(TX) -> ic705(RX) only. ic705 must never "
                          "transmit for this task; ba/both are removed.")
    ap.add_argument("--capture-tail", type=float, default=DEFAULT_CAPTURE_TAIL)
    ap.add_argument("--inter-trial", type=float, default=DEFAULT_INTER_TRIAL)
    ap.add_argument("--output-dir", type=Path)
    args = ap.parse_args(argv)

    if args.a != "ic7300" or args.b != "ic705":
        raise SystemExit("refusing: this task requires a=ic7300 (TX), b=ic705 (RX, never keyed)")

    registry = mode_qualification.registry("hf-ssb", "experimental")
    mode = registry.resolve(11)
    if mode.mode_id != 11 or mode.name != "hf4":
        raise SystemExit(f"registry did not resolve mode_id 11 to HF4 (got {mode.name}/{mode.mode_id})")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("logs") / "mode_qualification" / "hf-ssb" / "hf4" / f"{stamp}-promotion-integration"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("HF4 promotion integration test (production whale.modes.hf4_mode.HF4, "
          "registry-resolved, not sc_fast called directly)")
    print(f"radios A={args.a}, B={args.b}; seed={args.seed}; trials={args.trials}")
    print(f"frame: {mode.chunk_size} B DATA chunk, mode_id={mode.mode_id}, "
          f"baud={mode.baud}, tx_rate={mode.tx_sample_rate}, rx_rate={mode.rx_sample_rate}")

    records = []
    with pair_factory(args.a, args.b, warmup=3.0) as (transport_a, transport_b):
        records.extend(run_direction(
            transport_a, transport_b, mode, f"A:{args.a}->B:{args.b}",
            args.trials, args.seed,
            capture_tail=args.capture_tail, inter_trial=args.inter_trial))

    decoded = sum(1 for r in records if r["outcome"] == "decoded")
    total = len(records)
    raw_bers = [r["raw_ber"] for r in records if r["raw_ber"] is not None]
    mean_raw_ber = float(np.mean(raw_bers)) if raw_bers else None
    net_bps_decoded = [r["net_bps"] for r in records if r["outcome"] == "decoded"]
    mean_net_bps = float(np.mean(net_bps_decoded)) if net_bps_decoded else 0.0

    print(f"\n== RESULTS == {decoded}/{total} decoded; mean_raw_ber={mean_raw_ber}; "
          f"mean_net_bps(decoded only)={mean_net_bps:.1f}")

    out = {
        "note": "HF4 promotion integration test: exercises the production "
                "whale.modes.hf4_mode.HF4 (registry-resolved via "
                "whale.mode_qualification.registry('hf-ssb', 'experimental')) "
                "through mode.encode()/tx.send()/rx.snapshot_rx()/mode.decode(), "
                "the same call shape scripts/sweep_modes.py uses for every "
                "registered mode -- not experiments/hf13_fast_sync_v1's own "
                "sc_fast-direct hardware_test.py, which already validated the "
                "underlying PHY (see its RESULTS.md). No FEC in this mode: "
                "post_fec_ber is always null, never fabricated.",
        "channel": {"type": "hardware", "radio_a": args.a, "radio_b": args.b},
        "mode": {"mode_id": mode.mode_id, "name": mode.name, "chunk_size": mode.chunk_size},
        "seed": args.seed, "trials": records,
        "summary": {"decoded": decoded, "total": total,
                    "mean_raw_ber": mean_raw_ber,
                    "post_fec_ber": None,
                    "mean_net_bps_decoded_only": mean_net_bps},
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    print(f"wrote {result_path}")
    return 0 if decoded == total else 1


if __name__ == "__main__":
    sys.exit(main())
