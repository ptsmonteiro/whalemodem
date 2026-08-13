"""Audio device lookup and a TX-play/RX-record harness for over-the-air tests.

The radios' USB sound cards are used via WASAPI (lower latency / more
predictable buffering than MME or DirectSound on Windows). Which card belongs
to which radio lives in radios.py, not here.

Copied from radiomodem's shark/hw/audio_io.py (unchanged).
"""

import threading
import time

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 48000


def _wasapi_index():
    for i, api in enumerate(sd.query_hostapis()):
        if "wasapi" in api["name"].lower():
            return i
    raise LookupError("no WASAPI host API found")


def find_device(name_substr, kind):
    """Finds a WASAPI device index by partial name and direction ('input'/'output')."""
    hostapi = _wasapi_index()
    channel_key = "max_input_channels" if kind == "input" else "max_output_channels"
    matches = [
        i
        for i, d in enumerate(sd.query_devices())
        if d["hostapi"] == hostapi
        and name_substr.lower() in d["name"].lower()
        and d[channel_key] >= 1
    ]
    if not matches:
        raise LookupError(f"no WASAPI {kind} device matching {name_substr!r}")
    if len(matches) > 1:
        raise LookupError(f"ambiguous WASAPI {kind} device matches for {name_substr!r}: {matches}")
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
    """
    tx_signal = np.asarray(tx_signal, dtype=np.float32).reshape(-1, 1)
    tx_duration = len(tx_signal) / samplerate

    tx_pos = 0
    output_underflows = 0
    done = threading.Event()

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
            done.set()
            raise sd.CallbackStop
        outdata[:] = chunk
        tx_pos = end

    started = time.time()
    ptt.key(True)
    try:
        time.sleep(ptt_lead)
        with sd.OutputStream(device=tx_device, samplerate=samplerate, channels=1,
                             dtype="float32", latency=0.1, callback=out_callback):
            done.wait(tx_duration + 5.0)
        time.sleep(ptt_tail)
    finally:
        ptt.key(False)

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

    Timeline: [pre_roll] [ptt_lead] [tx_signal] [ptt_tail] [post_roll], all
    covered by one continuous recording started at t=0.

    Both streams are callback-driven rather than using the blocking
    read()/write() API: the blocking API's record loop runs its own
    stream.read() calls from a plain Python thread, subject to GIL and OS
    scheduler jitter, which is enough to starve a USB audio device's buffer
    and produce periodic micro-dropouts in the transmitted/received audio.
    PortAudio's own callback thread is not subject to that. This also lets
    us directly detect and report input overflow / output underflow (xrun)
    events instead of just inferring them from a distorted spectrum.
    """
    tx_signal = np.asarray(tx_signal, dtype=np.float32).reshape(-1, 1)
    tx_duration = len(tx_signal) / samplerate
    total_duration = pre_roll + ptt_lead + tx_duration + ptt_tail + post_roll
    rec_frames = int(round(total_duration * samplerate))
    recorded = np.zeros((rec_frames, 1), dtype=np.float32)

    # Default PortAudio latency for these devices is ~3ms (their reported
    # default_low_output_latency), which is thin enough to underrun under
    # any scheduling jitter. Request explicit, generous latency on both
    # streams to give PortAudio enough buffer headroom.
    stream_latency = 0.1

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
    tx_done = threading.Event()

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
            tx_done.set()
            raise sd.CallbackStop
        outdata[:] = chunk
        tx_pos = end

    in_stream = sd.InputStream(
        device=rx_device, samplerate=samplerate, channels=1, dtype="float32",
        latency=stream_latency, callback=in_callback,
    )
    in_stream.start()
    try:
        time.sleep(pre_roll)
        ptt.key(True)
        try:
            time.sleep(ptt_lead)
            with sd.OutputStream(
                device=tx_device, samplerate=samplerate, channels=1, dtype="float32",
                latency=stream_latency, callback=out_callback,
            ):
                tx_done.wait(tx_duration + 5.0)
            time.sleep(ptt_tail)
        finally:
            ptt.key(False)
        time.sleep(post_roll)
    finally:
        rec_done.wait(1.0)
        in_stream.stop()
        in_stream.close()

    if input_overflows or output_underflows:
        print(f"audio_io: xrun detected -- {input_overflows} input overflow(s), "
              f"{output_underflows} output underflow(s) during capture")

    return recorded[:, 0]
