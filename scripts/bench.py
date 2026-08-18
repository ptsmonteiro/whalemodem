"""The rig the sweep and measurement scripts in this directory share.

Every characterisation script here answers the same shape of question --
"does a frame with these parameters survive the real link?" -- by the same
method, deliberately: bypass whale.link's ARQ entirely and do one direct
modulate -> TX -> capture -> demodulate per trial, so a data point is a
property of the channel and the DSP rather than of the retry logic sitting
on top of them. Each script grew its own copy of that loop, and the copies
drifted. This is the one copy.

Three things live here, in increasing order of how much they were worth
extracting:

  - radio_pair(), the open/warm-up/close dance. Nine scripts had it; two of
    those leaked a transport when the body raised, because the constructor
    ran outside the try.

  - run_trials(), the trial loop itself, and near_miss_diag(), the
    frame-span diagnostic it prints on a failure. The diagnostic is the
    reason this file exists at all: it was wrong in its first form,
    comparing an absolute end_index against a bare frame duration and so
    reporting "~1.2x expected" for frames that had in fact been read
    perfectly. That number was taken as evidence of a false sync lock on
    garbage, which it was not. The fix -- measure start_index..end_index,
    the span the decoder actually believed in -- had to be made twice, and
    the second copy carries a comment pointing at the first. A third copy
    would have been the one nobody fixed.

  - walk(), the "worst direction decides, stop at the first failure" search
    that four scripts implement as hand-rolled loops. Both directions are
    always tested because the two legs of this bench have materially
    different SNR (see whale/afsk.py) -- a limit that holds one way and not
    the other is not a limit, and taking the better direction would have
    quietly overstated every number in the README.

Nothing here imports whale.link. That is the point: these scripts measure
the layer underneath it.

Scripts in this directory import it as `from bench import ...`, which
resolves because Python puts a script's own directory on sys.path.
"""

import time
from contextlib import contextmanager

import numpy as np

from whale import afsk, framing
from whale.transport import RadioTransport, SAMPLE_RATE

# The bench is two radios, always these two, always in this order.
STATION_A = "ic705"
STATION_B = "ht"

TRIALS = 5
SUCCESS_THRESHOLD = 0.8  # fraction of trials that must decode cleanly to call a point "good"

# Seconds captured after tx.send() returns. send() blocks until PTT drops,
# but the receiver's stream has its own latency and the squelch has a tail,
# so the frame's last samples are still arriving when send() returns.
CAPTURE_TAIL = 1.0
INTER_TRIAL = 0.5

# Optional low-level noise wrapped around a frame before transmitting it.
#
# Not a channel model -- it exists to separate two confounded failures. A
# frame placed at t=0 of the TX buffer starts arriving while the audio chain
# is still settling from PTT, so its sync preamble competes with a startup
# transient, and a "this baud does not decode" result may really be "this
# baud does not decode *in the first 100ms after keying*". The 700-baud
# cliff in the unpadded sweeps was exactly that. Padding leaves the
# modulated frame untouched and just gives the chain a second to settle
# first, which makes a padded run a passband/frame-size measurement and an
# unpadded one a timing measurement. Pick deliberately.
PAD_SECONDS = 1.0
PAD_AMPLITUDE = 0.1

WARMUP_SECONDS = 2.0


@contextmanager
def radio_pair(a=STATION_A, b=STATION_B, warmup=WARMUP_SECONDS, transport_cls=RadioTransport):
    """Opens both bench radios, starts their RX streams, waits out the
    warm-up, and closes both however the body exits.

    The warm-up is not superstition: the first capture after a stream opens
    is short and sometimes empty, and a trial that runs into it reads as a
    decode failure indistinguishable from a real one.

    transport_cls is for scripts that need an instrumented transport --
    sweep_turnaround.py subclasses RadioTransport to keep the audio that
    send() would otherwise discard.
    """
    print(f"opening radios ({a}, {b})...")
    t_a = transport_cls(a)
    try:
        t_b = transport_cls(b)
    except Exception:
        t_a.close()
        raise
    try:
        t_a.start_receiving()
        t_b.start_receiving()
        if warmup:
            print(f"warming up {warmup:g}s...")
            time.sleep(warmup)
        yield t_a, t_b
    finally:
        t_a.close()
        t_b.close()


def noise_pad(seconds=PAD_SECONDS, amplitude=PAD_AMPLITUDE):
    """A block of low-level noise to wrap a frame in. See PAD_SECONDS."""
    n = int(SAMPLE_RATE * seconds)
    rng = np.random.default_rng()
    return (amplitude * rng.standard_normal(n)).astype(np.float32)


def near_miss_diag(result, payload, profile):
    """One line describing a frame the decoder synced on but could not
    verify, or "" if there is nothing to say.

    A near miss is the interesting failure: the correlator found a sync
    word, read a length, and then the CRC did not hold. Comparing the span
    the decoder believed in against the span the payload should occupy says
    which half went wrong -- a span near 1.0x means the length byte was read
    correctly and the payload bits underneath it were not, while a span
    wildly off means the length field itself was corrupted or the sync lock
    was spurious.

    end_index is an absolute offset into the capture buffer, and the frame
    starts roughly a second into it (PTT lead, stream fill, head pad), so
    the span has to be taken against start_index. Measured against a bare
    frame duration instead -- as this did originally -- every clean frame
    reports as ~1.2x and looks like a false lock.
    """
    if "end_index" not in result:
        return ""
    expected_bits = len(framing.SYNC_BITS) + 8 + 8 * len(payload) + 16
    expected = round(expected_bits / profile.baud * SAMPLE_RATE)
    span = result["end_index"] - result.get("start_index", 0)
    return (f" [near-miss: frame span={span} vs expected {expected} samples"
            f" ({span / expected:.2f}x)]")


def run_trials(tx, rx, profile, payload, label, trials=None, pad=False,
               capture_tail=CAPTURE_TAIL, inter_trial=INTER_TRIAL, verbose=True):
    """Sends `payload` at `profile` from tx to rx `trials` times and reports
    what fraction came back intact.

    Returns (rate, confidences). `pad` wraps each transmission in noise --
    see PAD_SECONDS for when that is the measurement you want and when it
    hides the one you want.
    """
    trials = TRIALS if trials is None else trials
    ok = 0
    confidences = []
    for i in range(1, trials + 1):
        # snapshot_rx() does not consume the buffer -- flush stale audio
        # from prior trials or captures accumulate across the whole run.
        stale = rx.snapshot_rx()
        rx.consume_rx(len(stale))

        frame_audio = afsk.modulate(payload, profile=profile)
        tx_audio = np.concatenate([noise_pad(), frame_audio, noise_pad()]) if pad else frame_audio

        t0 = time.time()
        tx.send(tx_audio)
        tx_wall = time.time() - t0

        # With padding the frame ends a full pad before the transmission
        # does, so the same post-frame margin needs the pad added back.
        time.sleep(capture_tail + (PAD_SECONDS if pad else 0.0))

        captured = rx.snapshot_rx()
        result = afsk.demodulate(captured, profile=profile)
        confidence = result.get("confidence", 0.0)
        good = result.get("payload") == payload
        confidences.append(confidence)
        ok += int(good)
        if verbose:
            diag = "" if good else near_miss_diag(result, payload, profile)
            print(f"  [{label}] trial {i}/{trials}: tx={tx_wall:.2f}s "
                  f"captured={len(captured)}samp confidence={confidence:.1f} "
                  f"decoded={good}{diag}")
        time.sleep(inter_trial)

    rate = ok / trials
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    print(f"  [{label}] => {ok}/{trials} ok ({rate * 100:.0f}%), avg confidence={avg_conf:.1f}")
    return rate, confidences


def run_both_directions(t_a, t_b, profile, payload, names=(STATION_A, STATION_B), **kw):
    """run_trials() each way; returns the worst of the two rates.

    The legs differ in SNR, so the weaker one is the honest answer for the
    link as a whole.
    """
    name_a, name_b = names
    rate_ab, _ = run_trials(t_a, t_b, profile, payload, f"{name_a}->{name_b}", **kw)
    rate_ba, _ = run_trials(t_b, t_a, profile, payload, f"{name_b}->{name_a}", **kw)
    worst = min(rate_ab, rate_ba)
    print(f"  worst-direction success: {worst * 100:.0f}%")
    return worst


def walk(candidates, measure, describe=str, threshold=SUCCESS_THRESHOLD):
    """Tries `candidates` in order until one fails, returning the last that
    passed (or None if the first already failed).

    `measure(candidate)` returns a success rate. Candidates are ordered so
    that the run gets steadily harder, so the first failure ends the walk:
    the sweeps are looking for an edge, and once past it every further point
    costs a minute of airtime to confirm what is already known.

    Returning the last *passing* candidate rather than the first failing one
    matters -- the answer these scripts report is a limit that holds, not
    the point at which it stopped holding.
    """
    last_good = None
    for candidate in candidates:
        if measure(candidate) >= threshold:
            last_good = candidate
        else:
            print(f"  {describe(candidate)} failed the {threshold * 100:.0f}% bar -- stopping")
            break
    return last_good


def counting_payload(size):
    """`size` bytes of 0,1,2,...  -- the sweeps' standard test payload."""
    return bytes(i % 256 for i in range(size))


# A DATA_ACK-shaped frame: the smallest thing the link ever puts on air, and
# so the worst case for a sync search that has almost no signal to lock onto.
ACK_SHAPED_PAYLOAD = bytes([0x06, 0x00])
