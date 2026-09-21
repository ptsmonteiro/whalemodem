"""The rig the hardware and measurement scripts in this directory share.

radio_pair() opens both bench radios, warms them up, and closes both however
the body exits. noise_pad() is low-level noise to wrap a frame in.

Scripts in this directory import it as `import bench`, which resolves
because Python puts a script's own directory on sys.path.
"""

import time
from contextlib import contextmanager

import numpy as np

from whale.hw import radios as radios_mod
from whale.transport import RadioTransport, SAMPLE_RATE

# The bench is two radios, always in this order. Scripts that know which
# channel they are testing take the pair from the configured inventory with
# channel_pair(); these names are the fallback for the ones that don't.
STATION_A = "ic705"
STATION_B = "ht"

# Optional low-level noise wrapped around a frame before transmitting it.
#
# Not a channel model -- a frame placed at t=0 of the TX buffer starts
# arriving while the audio chain is still settling from PTT, so its preamble
# competes with a startup transient. Padding leaves the modulated frame
# untouched and gives the chain time to settle first.
PAD_SECONDS = 1.0
PAD_AMPLITUDE = 0.1

WARMUP_SECONDS = 2.0


@contextmanager
def radio_pair(a=STATION_A, b=STATION_B, warmup=WARMUP_SECONDS, transport_cls=RadioTransport,
               a_receive_only=False, b_receive_only=False):
    """Opens both bench radios, starts their RX streams, waits out the
    warm-up, and closes both however the body exits.

    a_receive_only or b_receive_only opens that station with no PTT backend
    at all: the bench takes its audio and nothing in the process can key it. Use it for
    one-way tests whose receiving radio must not transmit -- it makes that
    a property of the transport rather than of the caller's discipline --
    and for a receiver whose PTT control is unavailable, since the audio
    device is otherwise unreachable behind PTT discovery.

    The warm-up is not superstition: the first capture after a stream opens
    is short and sometimes empty, and a trial that runs into it reads as a
    decode failure indistinguishable from a real one.

    transport_cls is for scripts that need an instrumented transport.
    """
    print(f"opening radios ({a}, {b})...")
    t_a = transport_cls(a, receive_only=True) if a_receive_only else transport_cls(a)
    try:
        t_b = transport_cls(b, receive_only=True) if b_receive_only else transport_cls(b)
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


def channel_pair(channel, path=None):
    """The first two radios the configured inventory offers for `channel`.

    The bench pair is whatever the station has, in the order config.toml
    lists it, rather than two fixed radio keys a given station may not use.
    Fewer than two eligible radios is a configuration problem, not a run to
    start: a sweep needs a transmitter and a separate receiver.
    """
    inventory = radios_mod.radio_inventory(path)
    eligible = [id_ for id_, radio in inventory.radios.items()
                if channel in radio.channels]
    if len(eligible) < 2:
        raise ValueError(
            f"channel {channel!r} needs two configured radios; the inventory "
            f"offers {eligible if eligible else 'none'} "
            f"(radios: {sorted(inventory.radios)})")
    return eligible[0], eligible[1]


def noise_pad(seconds=PAD_SECONDS, amplitude=PAD_AMPLITUDE):
    """A block of low-level noise to wrap a frame in. See PAD_SECONDS."""
    n = int(SAMPLE_RATE * seconds)
    rng = np.random.default_rng()
    return (amplitude * rng.standard_normal(n)).astype(np.float32)
