"""Bench sweep: how short can the dead time around a frame get?

Total key-to-unkey (PTT ON -> PTT OFF) air time for one frame is

    ptt_lead + output-stream startup + head_pad + frame body + tail_pad + ptt_tail

and only "frame body" carries information. The other four are settling
allowances, each picked conservatively when it was introduced and never
measured:

    ptt_lead   (whale/transport.py send())      unmodulated carrier, so the
                                                local radio's T/R switch has
                                                finished before audio starts
    head_pad   (whale/framing.py)               modulated tones, so the peer's
                                                squelch/AGC has settled before
                                                the sync word arrives
    tail_pad   (whale/framing.py)               modulated tones, so the tail
                                                of the transmission eats
                                                padding instead of the CRC
    ptt_tail   (whale/transport.py send())      carrier held after the audio

Same method as the other bench sweeps here: bypass whale.link's ARQ, one
direct modulate -> TX -> capture -> demodulate per trial, both directions,
worst direction decides. Unlike them, there is deliberately *no* noise
padding around the frame -- the settling behaviour being measured is
exactly what that padding exists to mask.

Two knobs at each end of the frame, and they trade off against each other
(ptt_lead and head_pad are both dead time before the sync word; tail_pad
and ptt_tail are both dead time after the CRC), so one-at-a-time descent
can land on a bad split. Minimise the *modulated* pad first while the
carrier-only allowance is still generous, then the carrier-only
allowance, then check alternative splits of the same total.

Pass/fail over 5 trials cannot resolve anything near a cliff edge. What
actually settled the numbers was measuring the pads *continuously*: both
pads are an alternating 0,1,0,1,... bit pattern, so walking the demodulated
symbol stream outward from the sync word and finding where the alternation
breaks says, in ms, exactly how much of each pad survived. That turns one
transmission into a measurement instead of a coin flip, and it is what
found the two real results here:

  - The ~213ms "tail corruption" the pads were originally sized against was
    audio_io.transmit() discarding ~100ms of queued audio on CallbackStop,
    not the radios. 471/600 tail bits survived before the fix, 600/600
    after. See audio_io.transmit()'s docstring.
  - The lead is dominated by the IC-705's T/R switch, which is slow *and*
    variable: 129-310ms over 28 samples, against 22-72ms for the HT. That
    variability, not a fixed turn-on time, is what the lead margin buys.

Run one stage at a time, e.g.:

    python scripts/sweep_ptt_timing.py --vary head_pad --values 0.2,0.1,0.05,0
    python scripts/sweep_ptt_timing.py --points 0.22:0.08:0.03:0.05 --trials 10

Values are seconds. A --points entry is ptt_lead:head_pad:tail_pad:ptt_tail;
with no --vary or --points it runs whatever the current constants are.
"""
import argparse
import sys
import time

import bench
from whale import afsk, framing, transport as transport_mod
from whale.hw import audio_io
from whale.transport import SAMPLE_RATE

# Long enough to cover the RX stream's own 0.1s latency plus the receiver's
# squelch tail, short enough not to dominate the sweep's runtime.
CAPTURE_TAIL = 0.45
INTER_TRIAL = 0.25

BASELINE = dict(ptt_lead=transport_mod.PTT_LEAD, head_pad=framing.HEAD_PAD_SECONDS,
                tail_pad=framing.TAIL_PAD_SECONDS, ptt_tail=transport_mod.PTT_TAIL)

# Instrument the real key-to-unkey duration rather than inferring it from
# wall time around transport.send(), which also includes buffer clears and
# the WASAPI retry wrapper.
_real_transmit = audio_io.transmit
_last_keyed = {}


def _timed_transmit(*args, **kwargs):
    duration = _real_transmit(*args, **kwargs)
    _last_keyed["seconds"] = duration
    return duration


audio_io.transmit = _timed_transmit


def data_payload(profile):
    """A DATA frame's AFSK payload at this profile: ptype + seq + chunk."""
    return bytes((i % 256 for i in range(profile.chunk_size + afsk.DATA_FRAME_HEADER_BYTES)))


def ack_payload():
    """A DATA_ACK frame's AFSK payload: ptype + seq."""
    return bytes([afsk.PROFILES[0].mode_id, 0x80])


def run_point(tx, rx, profile, payload, cfg, trials, label, verbose=True):
    framing.HEAD_PAD_SECONDS = cfg["head_pad"]
    framing.TAIL_PAD_SECONDS = cfg["tail_pad"]
    ok = 0
    confidences = []
    keyed = []
    for i in range(1, trials + 1):
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))
        audio = afsk.modulate(payload, profile=profile)
        tx.send(audio, ptt_lead=cfg["ptt_lead"], ptt_tail=cfg["ptt_tail"])
        keyed.append(_last_keyed.get("seconds", float("nan")))
        time.sleep(CAPTURE_TAIL)
        captured = rx.snapshot_rx()
        result = afsk.demodulate(captured, profile=profile)
        good = result.get("payload") == payload
        confidences.append(result.get("confidence", 0.0))
        ok += int(good)
        if verbose:
            print(f"    [{label}] trial {i}/{trials}: keyed={keyed[-1]:.3f}s "
                  f"audio={len(audio)/SAMPLE_RATE:.3f}s confidence={result.get('confidence', 0.0):.1f} "
                  f"decoded={good}")
        time.sleep(INTER_TRIAL)
    rate = ok / trials
    return rate, sum(confidences) / len(confidences), sum(keyed) / len(keyed)


def run_both(t_a, t_b, profile, payload, cfg, trials, verbose=True):
    rate_ab, conf_ab, keyed = run_point(t_a, t_b, profile, payload, cfg, trials, "ic705->ht", verbose)
    rate_ba, conf_ba, _ = run_point(t_b, t_a, profile, payload, cfg, trials, "ht->ic705", verbose)
    worst = min(rate_ab, rate_ba)
    print(f"  ic705->ht {rate_ab*100:3.0f}% (conf {conf_ab:4.1f}) | "
          f"ht->ic705 {rate_ba*100:3.0f}% (conf {conf_ba:4.1f}) | "
          f"worst {worst*100:3.0f}% | keyed {keyed:.3f}s")
    return worst, keyed


def parse_point(text):
    lead, head, tail, ptt_tail = (float(x) for x in text.split(":"))
    return dict(ptt_lead=lead, head_pad=head, tail_pad=tail, ptt_tail=ptt_tail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="1200baud",
                    help="afsk profile name, or 'all'")
    ap.add_argument("--frame", default="data", choices=["data", "ack", "both"])
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--vary", choices=["ptt_lead", "head_pad", "tail_pad", "ptt_tail"])
    ap.add_argument("--values", help="comma-separated seconds for --vary")
    ap.add_argument("--points", help="comma-separated ptt_lead:head_pad:tail_pad:ptt_tail")
    ap.add_argument("--verbose", action="store_true", help="print every trial")
    for knob, default in BASELINE.items():
        ap.add_argument(f"--{knob.replace('_', '-')}", type=float, default=default)
    args = ap.parse_args()

    base = {k: getattr(args, k) for k in BASELINE}
    if args.points:
        points = [(text, parse_point(text)) for text in args.points.split(",")]
    elif args.vary:
        points = []
        for value in (float(x) for x in args.values.split(",")):
            cfg = dict(base)
            cfg[args.vary] = value
            points.append((f"{args.vary}={value}", cfg))
    else:
        points = [("baseline", base)]

    profiles = afsk.PROFILES if args.profile == "all" else \
        [p for p in afsk.PROFILES if p.name == args.profile]
    if not profiles:
        ap.error(f"unknown profile {args.profile!r}")

    results = []
    with bench.radio_pair() as (t_ic705, t_ht):
        for profile in profiles:
            frames = []
            if args.frame in ("data", "both"):
                frames.append(("DATA", data_payload(profile)))
            if args.frame in ("ack", "both"):
                frames.append(("ACK", ack_payload()))
            for frame_name, payload in frames:
                for label, cfg in points:
                    print(f"\n-- {profile.name} {frame_name}({len(payload)}B) {label} "
                          f"[lead={cfg['ptt_lead']} head={cfg['head_pad']} "
                          f"tail={cfg['tail_pad']} ptt_tail={cfg['ptt_tail']}] --")
                    worst, keyed = run_both(t_ic705, t_ht, profile, payload, cfg,
                                            args.trials, verbose=args.verbose)
                    results.append((profile.name, frame_name, label, worst, keyed))
        print("\n===== SUMMARY =====")
        for profile_name, frame_name, label, worst, keyed in results:
            print(f"{profile_name:9s} {frame_name:4s} {label:24s} worst={worst*100:3.0f}%  keyed={keyed:.3f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
