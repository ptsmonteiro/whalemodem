"""Bench sweep: how soon after the peer stops talking may we key up?

This is the one dead-time knob scripts/sweep_ptt_timing.py never touched.
That script measures the four allowances *around* a frame (ptt_lead,
head_pad, tail_pad, ptt_tail), all of which are inside one station's own
keying. This one measures the gap *between* two stations' keyings --
whale/link.py's TX_TURNAROUND_DELAY -- which timing the acceptance run
frame by frame showed to be the single largest item in the link's budget:
2.00s of a 3.91s exchange, against 0.67s actually spent on payload bits.

What the wait has to cover, measured from the peer's last CRC bit:

    framing.TAIL_PAD_SECONDS   the peer is still transmitting pad
    transport.PTT_TAIL         the peer is still keyed, carrier only
    the peer's T/R switch releasing, and ours engaging

Note that it is measured from the peer's audio, not from the moment we
decide to reply. whale/link.py anchors it the same way (_await_turnaround),
because everything between those two points -- the decode poll interval,
the demodulation itself -- is settling time that has already elapsed. The
responder below reproduces that anchoring exactly, so what this sweeps is
the constant as the link layer actually uses it.

Two failure modes hide behind "the reply didn't get through", and the point
of the script is to tell them apart:

  - Collision. We keyed up while the peer was still on air. Our start is
    simply not received; the peer's radio was transmitting, so its receiver
    was deaf. Shows up as a negative `gap`.
  - T/R settling. The gap was positive but our own radio had not finished
    swapping over, so the front of our transmission went out unusable.
    Shows up as a positive gap with pad bits lost.

Pass/fail over a handful of trials cannot resolve either, so the reply is
sent with a deliberately long alternating head pad -- the same continuous
probe sweep_ptt_timing.py documents. Walking the demodulated symbol stream
backwards from the sync word and finding where the alternation breaks says,
in ms, exactly how much of the front of the reply was lost. One
transmission becomes a measurement instead of a coin flip. Because the
probe pad also delays the sync word, decode success under `--probe-pad 1.0`
is *not* the production pass/fail; re-run at the real head pad to confirm a
candidate value.

Run, roughly in this order:

    python scripts/sweep_turnaround.py --turnarounds 1.0,0.5,0.25,0.1,0.0
    python scripts/sweep_turnaround.py --turnarounds 0.25 --probe-pad 0.08 --trials 10

Values are seconds. `lost` is what to read: the smallest turnaround whose
worst direction still loses 0ms is the floor, and TX_TURNAROUND_DELAY wants
margin over it because the IC-705's T/R switch is variable (129-310ms
across 28 samples), not merely slow.
"""
import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from scipy.signal import correlate

from whale import afsk, framing, link as link_mod, transport as transport_mod
from whale.transport import RadioTransport, SAMPLE_RATE

# Long enough to cover the RX stream's own 0.1s latency plus the receiver's
# squelch tail after the reply ends.
CAPTURE_TAIL = 0.45
INTER_TRIAL = 0.35


class ProbeTransport(RadioTransport):
    """RadioTransport whose post-TX buffer clear can be switched off.

    transport.send() clears the RX buffer twice per call: once before
    keying, and once in its finally, both so our own leaked audio is never
    handed to the decoder. With a short turnaround the peer's reply starts
    arriving *before* that second clear runs, so the clear discards the
    front of a frame the radios delivered perfectly well.

    That is a real hazard in production -- transport.send()'s own comment
    flags it -- but it is a software loss, and counting it here would make
    the radios look worse than they are. Suppressing only the second clear
    separates the two; the sweep reports `clipped` whenever the reply began
    early enough that production would have taken the hit.

    send() is _clear_buffer's only caller and calls it exactly twice, so
    the even-numbered call is the post-TX one.
    """

    def __init__(self, radio_name):
        super().__init__(radio_name)
        self.keep_reply_audio = True
        self._clears = 0

    def _clear_buffer(self):
        self._clears += 1
        if self.keep_reply_audio and self._clears % 2 == 0:
            return
        super()._clear_buffer()


def pad_bits_surviving(captured, profile, max_pad_bits, i_star=None):
    """How many of the reply's leading alternating pad bits arrived intact.

    The pad is head_pad_bits(): 0,1,0,1,... immediately before the sync
    word, and nothing parses it, so it can be made arbitrarily long and
    read as a ruler. Walks outward from the sync word until the alternation
    breaks and returns the count -- divide by baud for the surviving
    duration, subtract from the pad length for what was lost.
    """
    sps = round(SAMPLE_RATE / profile.baud)
    diff = afsk._tone_energy_diff(captured, SAMPLE_RATE, sps, profile.freq0, profile.freq1)
    template = afsk._sync_template(sps, SAMPLE_RATE, profile.freq0, profile.freq1)
    if len(diff) < len(template):
        return 0
    if i_star is None:
        corr = correlate(diff, template, mode="valid", method="fft")
        i_star = int(np.argmax(corr))
    first_index = i_star + (sps - 1)
    surviving = 0
    for k in range(1, max_pad_bits + 1):
        idx = first_index - k * sps
        if idx < 0:
            break
        if int(diff[idx] > 0) != (max_pad_bits - k) % 2:
            break
        surviving += 1
    return surviving


def build_frames(profile, probe_pad):
    """(DATA-sized frame at the real head pad, ACK-sized reply with the long
    probe pad). Built up front so the responder thread never touches
    framing's module-level pad constants while a trial is running."""
    baseline = framing.HEAD_PAD_SECONDS
    try:
        data_payload = bytes([link_mod.PT_DATA]) + bytes(
            (i % 256 for i in range(profile.chunk_size + 1)))
        data_audio = afsk.modulate(data_payload, profile=profile)
        framing.HEAD_PAD_SECONDS = probe_pad
        reply_payload = bytes([link_mod.PT_DATA_ACK, 0x01])
        reply_audio = afsk.modulate(reply_payload, profile=profile)
    finally:
        framing.HEAD_PAD_SECONDS = baseline
    return (data_payload, data_audio), (reply_payload, reply_audio)


def responder(rx, profile, reply_audio, turnaround, out, ready, give_up_at):
    """Stands in for whale/link.py's decode loop plus _await_turnaround:
    poll, decode, anchor the wait on where the frame ended in the captured
    audio, then reply."""
    ready.set()
    while time.monotonic() < give_up_at:
        snap = rx.snapshot_rx()
        if len(snap):
            result = afsk.demodulate(snap, profile=profile)
            if result.get("payload") is not None:
                end = result.get("end_index", len(snap))
                anchor = time.monotonic() - max(0, len(snap) - end) / SAMPLE_RATE
                rx.consume_rx(end)
                out["decoded_at"] = time.monotonic()
                out["anchor"] = anchor
                remaining = (anchor + turnaround) - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                rx.send(reply_audio)
                out["replied_at"] = time.monotonic()
                return
        time.sleep(link_mod.DECODE_POLL_INTERVAL)


def run_point(tx, rx, profile, frames, turnaround, probe_pad, trials, label, verbose):
    (_, data_audio), (reply_payload, reply_audio) = frames
    reply_seconds = len(reply_audio) / SAMPLE_RATE
    max_pad_bits = round(probe_pad * profile.baud)

    decoded = 0
    gaps, losses, latencies, clips = [], [], [], 0
    for i in range(1, trials + 1):
        out = {}
        ready = threading.Event()
        thread = threading.Thread(
            target=responder,
            args=(rx, profile, reply_audio, turnaround, out, ready,
                  time.monotonic() + 30.0),
            daemon=True)
        thread.start()
        ready.wait(2.0)

        stale = tx.snapshot_rx()
        tx.consume_rx(len(stale))
        tx.send(data_audio)
        tx_returned = time.monotonic()
        thread.join(timeout=30.0)
        if "replied_at" not in out:
            print(f"    [{label}] trial {i}/{trials}: peer never decoded our frame -- skipped")
            time.sleep(INTER_TRIAL)
            continue

        # transmit() returns only once the last sample is genuinely on air
        # and ptt_tail has elapsed, so both audio spans can be recovered by
        # subtraction -- no instrumentation inside transmit() needed.
        our_audio_end = tx_returned - transport_mod.PTT_TAIL
        reply_audio_end = out["replied_at"] - transport_mod.PTT_TAIL
        reply_audio_start = reply_audio_end - reply_seconds
        gap = reply_audio_start - our_audio_end
        latency = out["decoded_at"] - our_audio_end
        clipped = reply_audio_start < tx_returned

        time.sleep(CAPTURE_TAIL)
        captured = tx.snapshot_rx()
        result = afsk.demodulate(captured, profile=profile)
        good = result.get("payload") == reply_payload
        surviving = pad_bits_surviving(captured, profile, max_pad_bits,
                                       result.get("start_index"))
        lost_ms = (max_pad_bits - surviving) / profile.baud * 1000

        decoded += int(good)
        gaps.append(gap)
        losses.append(lost_ms)
        latencies.append(latency)
        clips += int(clipped)
        if verbose:
            print(f"    [{label}] trial {i}/{trials}: gap={gap*1000:+7.0f}ms "
                  f"lost={lost_ms:6.0f}ms decode_latency={latency*1000:5.0f}ms "
                  f"conf={result.get('confidence', 0.0):6.1f} decoded={good}"
                  f"{' CLIPPED' if clipped else ''}")
        time.sleep(INTER_TRIAL)

    if not gaps:
        return None
    n = len(gaps)
    return dict(rate=decoded / n, gap=sum(gaps) / n, lost=max(losses),
                latency=sum(latencies) / n, clipped=clips, trials=n)


def run_both(t_a, t_b, profile, frames, turnaround, probe_pad, trials, verbose):
    ab = run_point(t_a, t_b, profile, frames, turnaround, probe_pad, trials, "ic705->ht", verbose)
    ba = run_point(t_b, t_a, profile, frames, turnaround, probe_pad, trials, "ht->ic705", verbose)
    if ab is None or ba is None:
        print("  no usable trials in one or both directions")
        return None
    for name, r in (("ic705->ht", ab), ("ht->ic705", ba)):
        print(f"  {name} {r['rate']*100:3.0f}% decoded | gap {r['gap']*1000:+7.0f}ms | "
              f"worst lost {r['lost']:5.0f}ms | decode latency {r['latency']*1000:4.0f}ms | "
              f"clipped {r['clipped']}/{r['trials']}")
    return dict(rate=min(ab["rate"], ba["rate"]), lost=max(ab["lost"], ba["lost"]),
                gap=min(ab["gap"], ba["gap"]),
                latency=max(ab["latency"], ba["latency"]),
                clipped=ab["clipped"] + ba["clipped"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="1200baud", help="afsk profile name, or 'all'")
    ap.add_argument("--turnarounds", default="1.0,0.5,0.25,0.1,0.0",
                    help="comma-separated seconds to try")
    ap.add_argument("--probe-pad", type=float, default=1.0,
                    help="reply head pad, seconds -- the ruler the loss is measured against")
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--keep-post-tx-clear", action="store_true",
                    help="leave transport.send()'s post-TX buffer clear in place, so the "
                         "measurement includes audio production would have discarded")
    ap.add_argument("--verbose", action="store_true", help="print every trial")
    args = ap.parse_args()

    profiles = afsk.PROFILES if args.profile == "all" else \
        [p for p in afsk.PROFILES if p.name == args.profile]
    if not profiles:
        ap.error(f"unknown profile {args.profile!r}")
    turnarounds = [float(x) for x in args.turnarounds.split(",")]

    print("opening radios...")
    t_ic705 = ProbeTransport("ic705")
    t_ht = ProbeTransport("ht")
    t_ic705.keep_reply_audio = t_ht.keep_reply_audio = not args.keep_post_tx_clear
    results = []
    try:
        t_ic705.start_receiving()
        t_ht.start_receiving()
        print("warming up 2s...")
        time.sleep(2)
        for profile in profiles:
            frames = build_frames(profile, args.probe_pad)
            for turnaround in turnarounds:
                print(f"\n-- {profile.name} turnaround={turnaround}s "
                      f"probe_pad={args.probe_pad}s --")
                worst = run_both(t_ic705, t_ht, profile, frames, turnaround,
                                 args.probe_pad, args.trials, args.verbose)
                if worst is not None:
                    results.append((profile.name, turnaround, worst))
        print("\n===== SUMMARY (worst direction) =====")
        print(f"{'profile':9} {'turnaround':>10} {'decoded':>8} {'gap':>9} "
              f"{'lost':>8} {'latency':>8} {'clipped':>8}")
        for name, turnaround, r in results:
            print(f"{name:9} {turnaround:10.2f} {r['rate']*100:7.0f}% {r['gap']*1000:+8.0f}ms "
                  f"{r['lost']:6.0f}ms {r['latency']*1000:6.0f}ms {r['clipped']:8d}")
    finally:
        t_ic705.close()
        t_ht.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
