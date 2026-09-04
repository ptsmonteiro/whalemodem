"""Audio device lookup and a TX-play/RX-record harness for over-the-air tests.

The radios' USB sound cards are used through whichever PortAudio host API
sits closest to the hardware on the running OS -- WASAPI on Windows, Core
Audio on macOS, ALSA on Linux -- rather than a higher-level shared-mixer API
(MME/DirectSound on Windows; a PulseAudio/JACK server sitting in front of
ALSA on Linux) for lower and more predictable buffering. See _host_api_index()
for the per-platform default and how to override it. Which card belongs to
which radio lives in radios.py, not here.

Copied from radiomodem's shark/hw/audio_io.py, since diverged: both keyed
paths below now key *inside* their try block and un-key through ptt.unkey(),
so that neither a failed key-on nor a failed key-off can leave the
transmitter up. See the un-keying notes in ptt.py for the bench incident that
forced it.
"""

import logging
import os
import sys
import threading
import time

import numpy as np
import sounddevice as sd

from whale.hw import ptt as ptt_mod

SAMPLE_RATE = 48000

_log = logging.getLogger(__name__)

# Per-platform default host API, matched by substring against
# sd.query_hostapis()'s "name" field. Overridable with WHALE_AUDIO_HOST_API
# for setups that need something other than the default -- a Linux station
# deliberately routed through PulseAudio or JACK instead of raw ALSA, or a
# Windows fallback to MME/DirectSound on a card that WASAPI won't open.
_DEFAULT_HOST_API = {"win32": "wasapi", "darwin": "core audio"}.get(sys.platform, "alsa")


def _host_api_index():
    wanted = os.environ.get("WHALE_AUDIO_HOST_API", _DEFAULT_HOST_API).lower()
    hostapis = sd.query_hostapis()
    for i, api in enumerate(hostapis):
        if wanted in api["name"].lower():
            return i
    available = ", ".join(api["name"] for api in hostapis)
    raise LookupError(
        f"no host API matching {wanted!r} found (platform {sys.platform!r}); "
        f"available: {available}. Set WHALE_AUDIO_HOST_API to override the default.")


def find_device(name_substr, kind):
    """Finds a device index on the selected host API by partial name and
    direction ('input'/'output'). See _host_api_index() for which host API
    that is on this platform."""
    hostapi = _host_api_index()
    api_name = sd.query_hostapis()[hostapi]["name"]
    channel_key = "max_input_channels" if kind == "input" else "max_output_channels"
    matches = [
        i
        for i, d in enumerate(sd.query_devices())
        if d["hostapi"] == hostapi
        and name_substr.lower() in d["name"].lower()
        and d[channel_key] >= 1
    ]
    if not matches:
        raise LookupError(f"no {api_name} {kind} device matching {name_substr!r}")
    if len(matches) > 1:
        raise LookupError(f"ambiguous {api_name} {kind} device matches for {name_substr!r}: {matches}")
    return matches[0]


def transmit(tx_signal, tx_device, ptt, samplerate=SAMPLE_RATE, ptt_lead=0.3, ptt_tail=0.2):
    """Keys `ptt`, plays `tx_signal` out `tx_device`, unkeys. Returns the
    key-to-unkey duration in seconds.

    The transmit half of capture_while_transmitting(), split out for the
    cases where the receiver is not a sound card we own -- a remote KiwiSDR
    recording in its own process, for instance, which cannot be started and
    stopped around each transmission because it runs ten seconds behind. Same
    callback-driven output stream and same generous latency, for the same
    reason: PortAudio's callback thread does not suffer the GIL and scheduler
    jitter that starve a blocking write loop into micro-dropouts.

    The callback pads with zeros once the signal runs out and the *caller*
    decides when playing has finished, rather than the callback raising
    CallbackStop on the last partial block. CallbackStop tears the stream
    down as soon as that block is handed over, and on this WASAPI output
    that discards whatever is still queued in the device buffer -- one
    `latency` worth, ~100ms, silently chopped off the end of every single
    transmission. Measured on the bench by modulating a long alternating
    tail pad and counting how many of its bits came back intact: 471/600
    with CallbackStop, 600/600 with the zero-fill below. That missing 100ms
    was formerly masked by a tail pad; the pad was removed after this output
    truncation bug was fixed and radio acceptance runs showed no need for it.

    ptt_tail is therefore carrier held after the last sample is genuinely on
    air, not after the last sample was queued.

    Everything between the key and the un-key is inside one try/finally whose
    finally cannot raise. That is a hardware-safety property, not tidiness:

      - ptt.key(True) is *inside* the try. It used to sit in front of it, so a
        radio that did not acknowledge the key-on raised straight past the
        finally and never got an un-key -- even though an unacknowledged
        key-on is precisely the case where the transmitter may well be up.
      - the finally calls ptt.unkey(), which never raises. It used to call
        ptt.key(False) directly, and on the bench that raised TimeoutError out
        of the finally: the exception that was already propagating (PaErrorCode
        -9996, the USB bus dropping under our own RF) was replaced by a
        TimeoutError, and the transmitter stayed keyed with nothing left to
        turn it off. Both halves of that failed together, which is why both
        halves are fixed here.
    """
    tx_signal = np.asarray(tx_signal, dtype=np.float32).reshape(-1, 1)
    tx_duration = len(tx_signal) / samplerate

    tx_pos = 0
    output_underflows = 0
    queued = threading.Event()

    def out_callback(outdata, frames, time_, status):
        nonlocal tx_pos, output_underflows
        if status.output_underflow:
            output_underflows += 1
        end = tx_pos + frames
        chunk = tx_signal[tx_pos:end]
        if len(chunk) < frames:
            outdata[: len(chunk)] = chunk
            outdata[len(chunk):] = 0
            tx_pos = len(tx_signal)
            queued.set()
            return
        outdata[:] = chunk
        tx_pos = end

    started = time.time()
    try:
        ptt.key(True)
        time.sleep(ptt_lead)
        stream = sd.OutputStream(device=tx_device, samplerate=samplerate, channels=1,
                                 dtype="float32", latency=0.1, callback=out_callback)
        with stream:
            playing_since = time.time()
            # A stream whose device disappears mid-transmission simply stops
            # calling the callback, so this wait is what bounds a keying in
            # that case -- and it bounds it at tx_duration + 5s of keyed
            # transmitter, well past the expected total keying duration. The timeout is
            # left as it is (shortening it would start cutting real
            # transmissions off), but a wait that expires is now said out
            # loud, because in the logs it is otherwise indistinguishable
            # from a normal keying that merely took longer.
            if not queued.wait(tx_duration + 5.0):
                _log.warning("output stream stopped consuming audio before the end of "
                             "the signal (%d/%d samples played); un-keying",
                             tx_pos, len(tx_signal))
            # Sound starts leaving the card about `latency` after the stream
            # starts, so the last sample lands that much after the last one
            # was handed to the callback.
            remaining = (playing_since + tx_duration + stream.latency) - time.time()
            if remaining > 0:
                time.sleep(remaining)
        time.sleep(ptt_tail)
    finally:
        ptt_mod.unkey(ptt)

    if output_underflows:
        print(f"audio_io: {output_underflows} output underflow(s) during transmit")
    return time.time() - started


def capture_while_transmitting(
    tx_signal,
    tx_device,
    rx_device,
    ptt,
    samplerate=SAMPLE_RATE,
    pre_roll=0.3,
    post_roll=0.5,
    ptt_lead=0.2,
    ptt_tail=0.2,
):
    """Keys `ptt`, plays `tx_signal` out `tx_device`, and records `rx_device`
    for the whole timeline. Returns the recording as a 1-D float32 array.

    Timeline: [pre_roll] [ptt_lead] [output-stream latency] [tx_signal]
    [ptt_tail] [post_roll], all covered by one continuous recording started
    at t=0.

    Both streams are callback-driven rather than using the blocking
    read()/write() API: the blocking API's record loop runs its own
    stream.read() calls from a plain Python thread, subject to GIL and OS
    scheduler jitter, which is enough to starve a USB audio device's buffer
    and produce periodic micro-dropouts in the transmitted/received audio.
    PortAudio's own callback thread is not subject to that. This also lets
    us directly detect and report input overflow / output underflow (xrun)
    events instead of just inferring them from a distorted spectrum.
    """
    # Default PortAudio latency for these devices is ~3ms (their reported
    # default_low_output_latency), which is thin enough to underrun under
    # any scheduling jitter. Request explicit, generous latency on both
    # streams to give PortAudio enough buffer headroom.
    stream_latency = 0.1

    tx_signal = np.asarray(tx_signal, dtype=np.float32).reshape(-1, 1)
    tx_duration = len(tx_signal) / samplerate
    total_duration = pre_roll + ptt_lead + stream_latency + tx_duration + ptt_tail + post_roll
    rec_frames = int(round(total_duration * samplerate))
    recorded = np.zeros((rec_frames, 1), dtype=np.float32)

    rec_pos = 0
    input_overflows = 0
    rec_done = threading.Event()

    def in_callback(indata, frames, time_, status):
        nonlocal rec_pos, input_overflows
        if status.input_overflow:
            input_overflows += 1
        end = min(rec_pos + frames, rec_frames)
        n = end - rec_pos
        if n > 0:
            recorded[rec_pos:end] = indata[:n]
        rec_pos = end
        if rec_pos >= rec_frames:
            rec_done.set()

    tx_pos = 0
    output_underflows = 0
    tx_queued = threading.Event()

    # Zero-fill rather than CallbackStop, so the device buffer is not
    # discarded with ~`latency` of the signal still in it -- see transmit().
    def out_callback(outdata, frames, time_, status):
        nonlocal tx_pos, output_underflows
        if status.output_underflow:
            output_underflows += 1
        end = tx_pos + frames
        chunk = tx_signal[tx_pos:end]
        if len(chunk) < frames:
            outdata[: len(chunk)] = chunk
            outdata[len(chunk) :] = 0
            tx_pos = len(tx_signal)
            tx_queued.set()
            return
        outdata[:] = chunk
        tx_pos = end

    in_stream = sd.InputStream(
        device=rx_device, samplerate=samplerate, channels=1, dtype="float32",
        latency=stream_latency, callback=in_callback,
    )
    in_stream.start()
    try:
        time.sleep(pre_roll)
        # Keyed inside the try, un-keyed through a finally that cannot raise,
        # for the reasons spelled out in transmit(). Same code, same hazard.
        try:
            ptt.key(True)
            time.sleep(ptt_lead)
            out_stream = sd.OutputStream(
                device=tx_device, samplerate=samplerate, channels=1, dtype="float32",
                latency=stream_latency, callback=out_callback,
            )
            with out_stream:
                playing_since = time.time()
                if not tx_queued.wait(tx_duration + 5.0):
                    _log.warning("output stream stopped consuming audio before the end "
                                 "of the signal (%d/%d samples played); un-keying",
                                 tx_pos, len(tx_signal))
                remaining = (playing_since + tx_duration + out_stream.latency) - time.time()
                if remaining > 0:
                    time.sleep(remaining)
            time.sleep(ptt_tail)
        finally:
            ptt_mod.unkey(ptt)
        time.sleep(post_roll)
    finally:
        rec_done.wait(1.0)
        in_stream.stop()
        in_stream.close()

    if input_overflows or output_underflows:
        print(f"audio_io: xrun detected -- {input_overflows} input overflow(s), "
              f"{output_underflows} output underflow(s) during capture")

    return recorded[:, 0]
