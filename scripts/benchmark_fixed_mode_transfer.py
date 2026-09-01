"""Lifecycle-free fixed-mode session-throughput diagnostic.

This measures the existing stop-and-wait DATA/DATA_ACK exchange without
constructing a connection: DATA is locked to the selected waveform and ACKs
use the policy control waveform, exactly as the operational link does.  Times
are simulated channel occupancy/dead-air seconds, never host wall-clock time.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from experiments.hf3.benchmark_hf3 import benign_static_channel
from whale import framing, link_protocol, policy, rx_audio
from whale.modes.hc0_mode import HC0
from whale.modes.hf3_mode import HF3
from whale.qualification import channel_factory, trial_seed
from whale.trials import TrialOutcome, classify_decode

from benchmark_simulated_channels import DEFAULT_SEED, git_state


MODES = {HF3.name: HF3}


def _packet(ptype: int, mode, body: bytes) -> bytes:
    header, remainder = link_protocol.encode_air_header(ptype, mode.mode_id, body)
    return header + remainder


def _decode(mode, channel, packet: bytes):
    transmitted = np.asarray(mode.encode(packet), dtype=np.float32)
    impaired = channel.process(transmitted)
    drained = channel.drain()
    captured = rx_audio.downsample(np.concatenate((
        np.asarray(impaired.audio, dtype=np.float32),
        np.asarray(drained.audio, dtype=np.float32),
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32),
    )))
    result = mode.decode(captured)
    return classify_decode(result, packet, mode.confidence_threshold), len(transmitted) / mode.tx_sample_rate


def median_confidence_interval(values: list[float], confidence: float = 0.95):
    """Exact distribution-free two-sided interval for a population median."""
    ordered = sorted(values)
    n = len(ordered)
    alpha = 1.0 - confidence
    rank = 1
    while rank + 1 <= n // 2 and 2 * sum(
            math.comb(n, i) for i in range(rank + 1)) / 2**n <= alpha:
        rank += 1
    # rank is one-based: [X_(rank), X_(n-rank+1)].
    return [ordered[rank - 1], ordered[n - rank]], rank


def run_trial(mode, model: str, point_db: float, preset: str, payload: bytes,
              seed: int, turnaround: float, timeout_slack: float,
              max_retries: int):
    chunks = [payload[i:i + mode.chunk_size]
              for i in range(0, len(payload), mode.chunk_size)]
    elapsed = 0.0
    attempts = retransmissions = 0
    delivered = bytearray()
    for index, chunk in enumerate(chunks):
        seq = index % link_protocol.SEQ_MODULO
        flags = seq | (link_protocol.EOF_BIT if index == len(chunks) - 1 else 0)
        data = _packet(link_protocol.PT_DATA, mode, bytes([flags, 0]) + chunk)
        acknowledged = False
        for attempt in range(max_retries):
            attempts += 1
            retransmissions += attempt > 0
            leg_seed = trial_seed(seed, mode.mode_id, index, attempt + 1)
            forward = (benign_static_channel(point_db, leg_seed)
                       if model == "benign-static" else
                       channel_factory("watterson", point_db,
                           watterson_preset=preset)(leg_seed))
            outcome, data_airtime = _decode(mode, forward, data)
            elapsed += data_airtime + turnaround
            if outcome is not TrialOutcome.DECODED:
                # Operational stop-and-wait cannot know the frame failed. Its
                # deadline includes the expected ACK airtime and policy slack.
                elapsed += HC0.airtime(framing.AIR_HEADER_BYTES + 2) + turnaround + timeout_slack
                continue
            ack_body = bytes([seq, (seq + 1) % link_protocol.SEQ_MODULO,
                              mode.mode_id, 0])
            ack = _packet(link_protocol.PT_DATA_ACK, HC0, ack_body)
            reverse_seed = leg_seed ^ 0xB2A
            reverse = (benign_static_channel(point_db, reverse_seed)
                       if model == "benign-static" else
                       channel_factory("watterson", point_db,
                           watterson_preset=preset)(reverse_seed))
            ack_outcome, ack_airtime = _decode(HC0, reverse, ack)
            elapsed += ack_airtime + turnaround
            if ack_outcome is TrialOutcome.DECODED:
                delivered.extend(chunk)
                acknowledged = True
                break
            elapsed += timeout_slack
        if not acknowledged:
            break
    exact = bytes(delivered) == payload
    return {
        "seed": seed, "offered_bytes": len(payload),
        "verified_bytes": len(delivered) if exact else 0,
        "exact_payload": exact, "data_frames": len(chunks),
        "data_attempts": attempts, "retransmissions": retransmissions,
        "elapsed_channel_seconds": elapsed,
        "useful_throughput_bit_s": 8 * len(payload) / elapsed if exact else 0.0,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=MODES, default="hf3")
    ap.add_argument("--model", choices=("benign-static", "watterson"), required=True)
    ap.add_argument("--point", type=float, required=True)
    ap.add_argument("--watterson-preset", default="mid_latitude_quiet")
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--bytes", type=int, default=10_000, dest="payload_bytes")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.trials < 6 or args.payload_bytes < 10_000:
        ap.error("qualification runs require at least 6 trials and 10,000 bytes")
    mode = MODES[args.mode]
    selected_policy = policy.HF_SSB
    rows = []
    for trial in range(1, args.trials + 1):
        seed = trial_seed(args.seed, mode.mode_id, 0, trial)
        payload = np.random.default_rng(seed).integers(
            0, 256, args.payload_bytes, dtype=np.uint8).tobytes()
        row = run_trial(mode, args.model, args.point, args.watterson_preset,
                        payload, seed, selected_policy.tx_turnaround_delay,
                        selected_policy.ack_timeout_slack,
                        selected_policy.max_retries)
        row["trial"] = trial
        rows.append(row)
        print(f"trial {trial}/{args.trials}: {row['useful_throughput_bit_s']:.1f} bit/s, "
              f"{row['retransmissions']} retries")
    rates = [row["useful_throughput_bit_s"] for row in rows]
    interval, rank = median_confidence_interval(rates)
    commit, dirty = git_state()
    summary = {
        "completed_trials": sum(row["exact_payload"] for row in rows),
        "total_trials": len(rows), "median_useful_throughput_bit_s": float(np.median(rates)),
        "median_distribution_free_95_ci_bit_s": interval,
        "confidence_bound_order_statistic_rank": rank,
        "reference_per_frame_floor_bit_s": 2000.0,
        "meets_reference_as_session_rate": (
            all(row["exact_payload"] for row in rows) and interval[0] >= 2000.0),
        "qualification_note": (
            "Session throughput is system evidence, not the per-mode "
            "net-throughput qualification gate."),
    }
    document = {
        "schema_version": 1, "benchmark": "fixed_mode_session_throughput",
        "created_utc": datetime.now(timezone.utc).isoformat(), "git_commit": commit,
        "git_dirty": dirty, "configuration": vars(args) | {
            "out": str(args.out), "turnaround_seconds_each_change": selected_policy.tx_turnaround_delay,
            "ack_timeout_slack_seconds": selected_policy.ack_timeout_slack,
            "ack_mode": HC0.name, "data_mode_locked": mode.name,
            "includes": ["DATA framing", "DATA_ACK", "retries", "turnaround", "waveform PTT lead/tail"],
            "excludes": ["connection", "negotiation", "adaptation", "fallback", "disconnect"],
        }, "trials": rows, "summary": summary,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, allow_nan=False) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
