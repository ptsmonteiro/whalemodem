"""Radio transport: continuous receive capture + keyed transmit, on top of
whale.hw (sound card lookup, PTT). No DSP lives here -- only wiring a
continuously running input stream to a buffer, and handing transmit audio to
hw.audio_io.transmit() (which keys PTT, plays, un-keys).

The input stream is opened once and left running for the transport's whole
life, including while transmitting -- stopping/restarting it around every TX
(the natural-looking way to get "half duplex") makes the IC-705's WASAPI
output intermittently refuse to start right afterwards (PaErrorCode -9999,
WdmSyncIoctl), evidently a driver settling-time issue on this USB codec.
Simultaneous in+out on this hardware is fine; it is the stop/start churn
that isn't. So RX just stays open, and half duplex is enforced by discarding
whatever it captured immediately before/during/after our own TX instead.
"""

import collections
import ctypes
import logging
import threading
import time

import numpy as np
import sounddevice as sd

from whale import afsk
from whale.hw import audio_io
from whale.hw import radios as radios_mod

SAMPLE_RATE = audio_io.SAMPLE_RATE

_COINIT_APARTMENTTHREADED = 0x2
_com_ready = threading.local()


def _ensure_com_initialized():
    """PortAudio's WASAPI backend needs COM initialized on the calling
    thread. The main thread gets this for free (implicitly, or via whatever
    else touched COM first); a plain threading.Thread does not, and opening
    a WASAPI OutputStream from one reliably fails with PaErrorCode -9999
    (WdmSyncIoctl) on this machine -- 100% reproducible, regardless of which
    radio/device, order, or timing. One CoInitializeEx call per thread
    before its first stream fixes it."""
    if getattr(_com_ready, "done", False):
        return
    ctypes.windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    _com_ready.done = True

# Dead time held around each transmission, and the reason for each.
#
# PTT_LEAD covers the gap between keying and actually being usably on air.
# Measured on the bench (scripts/sweep_ptt_timing.py, plus the head-pad bit
# probe it documents): send a frame with a long alternating head pad and
# ptt_lead=0, then count how many of those pad bits decode at the far end.
# ht->ic705 loses only 22-72ms, but ic705->ht loses 129-310ms across 28
# samples -- the IC-705's T/R switch is slow and, more awkwardly, variable.
# Worst direction decides, so budget the full 310ms.
#
# One constant for every radio, deliberately. A per-radio lead was tried
# (0.08 for the HT against 0.22 for the IC-705) and rolled back: this is a
# modem meant to work with whatever is plugged into it, and a table of
# measured values only helps the two radios on this bench while quietly
# under-provisioning every radio not in it. The conservative figure costs
# the HT ~0.14s of dead carrier per frame, which is the price of not
# needing to know what the radio is.
#
# Opening the output stream and filling its first buffer already spends
# ~130ms of that with the carrier up, so PTT_LEAD only has to find the
# rest: 0.22 + 0.13 + framing.HEAD_PAD_SECONDS puts the sync word ~500ms
# after keying, comfortably clear of the 310ms worst case. Note that
# shrinking the stream latency would buy nothing -- it is dead air the
# radio needs anyway, and PTT_LEAD would simply have to grow to replace it.
#
# This constant covers the *transmitter* coming up. A receiver that is slow
# to settle is a separate allowance and is not bought here -- see
# framing.HEAD_PAD_SECONDS, which is sized against a receiver that blacks
# out for ~110ms after its squelch opens, and framing.SYNC_SECONDS, which is
# what makes that allowance cost the same at every profile.
#
# PTT_TAIL is carrier held after the last sample is genuinely on air (see
# audio_io.transmit, which now waits for playout rather than returning when
# the last block was merely queued). The measured on-air tail costs 1-2
# bits even when PTT drops immediately, so this is nearly all margin.
#
# Validated at 100% over 120 trials -- both directions, all three profiles,
# DATA and ACK frame sizes.
PTT_LEAD = 0.22
PTT_TAIL = 0.05

# The rest of the dead air a keying carries: opening the output stream and
# filling its first buffer, between PTT_LEAD elapsing and the first sample
# actually leaving the card. Not a knob -- it is what the audio stack does,
# recorded here so the keying-length arithmetic can account for it.
#
# MEASURED end to end rather than reasoned from audio_io's latency=0.1: an
# acceptance run logs `Ns audio, Ms keyed` for every transmission, and
# M - N - PTT_LEAD - PTT_TAIL is this figure. Across 44 keyings spanning
# both radios, all three profiles and every frame type, that came out at
# 0.15-0.16s -- not the 0.13 previously assumed from the requested stream
# latency, which left the derived chunk sizes ~20ms over budget. Take the
# worst; a keying budget wants the pessimistic end.
STREAM_FILL = 0.16

# whale/afsk.py sizes every profile's chunk_size so that a whole keying fits
# inside afsk.MAX_KEYING_SECONDS, and the overhead it budgets for is exactly
# the three constants above. It cannot import them -- that would drag the
# sound-card stack into the DSP module, which the software-only tests run
# without -- so the agreement is checked here instead, where both modules are
# already loaded. If this fires, the sizing is stale: re-derive
# afsk.KEYING_OVERHEAD_SECONDS from the new figures rather than silencing it,
# because the chunk sizes it produced are now over budget.
_KEYING_OVERHEAD = PTT_LEAD + STREAM_FILL + PTT_TAIL
if abs(_KEYING_OVERHEAD - afsk.KEYING_OVERHEAD_SECONDS) > 0.005:
    raise RuntimeError(
        f"keying overhead drifted: transport says {_KEYING_OVERHEAD:.3f}s "
        f"(PTT_LEAD {PTT_LEAD} + STREAM_FILL {STREAM_FILL} + PTT_TAIL {PTT_TAIL}) "
        f"but afsk.KEYING_OVERHEAD_SECONDS is {afsk.KEYING_OVERHEAD_SECONDS:.3f}s; "
        "the profiles' chunk_size was derived from the latter")

# How much recent audio the receiver keeps around for the decoder to search.
# Generous relative to one frame's ~7s worst case (255-byte payload at 300
# baud) so a frame straddling two decode attempts is never lost.
#
# Note this is the *cap*, not the working size -- whale/link.py's decode
# loop prunes audio it has already searched and found nothing in, so the
# buffer only approaches this length while a frame is actually arriving.
# That matters because demodulate() costs time proportional to buffer
# length (~14ms per second of audio per profile).
RX_BUFFER_SECONDS = 10.0


class RadioTransport:
    """One radio: continuous RX capture + on-demand keyed TX."""

    def __init__(self, radio_name: str):
        if radio_name not in radios_mod.RADIOS:
            raise ValueError(f"unknown radio {radio_name!r}; have {list(radios_mod.RADIOS)}")
        self.radio = radios_mod.RADIOS[radio_name]
        self.out_device, self.in_device = self.radio.devices()
        self.ptt = self.radio.ptt()

        # A deque of raw chunks rather than one growing array: the audio
        # callback runs on PortAudio's realtime thread, and re-concatenating
        # an array that can be RX_BUFFER_SECONDS long (480k samples) on every
        # callback -- ~10x/sec -- is real work on that thread. Appending a
        # chunk is O(1); the expensive concatenate+trim happens lazily in
        # snapshot_rx(), called from the (non-realtime) decode thread.
        self._chunks = collections.deque()
        self._chunks_len = 0
        self._buf_lock = threading.Lock()
        self._stream = None
        self._tx_lock = threading.Lock()  # serializes TX attempts
        self._transmitting = threading.Event()

    # -- receive ------------------------------------------------------

    def _in_callback(self, indata, frames, time_info, status):
        with self._buf_lock:
            self._chunks.append(indata[:, 0].copy())
            self._chunks_len += frames
            max_len = int(RX_BUFFER_SECONDS * SAMPLE_RATE)
            while self._chunks_len - len(self._chunks[0]) > max_len:
                self._chunks_len -= len(self._chunks.popleft())

    def start_receiving(self):
        if self._stream is not None:
            return
        _ensure_com_initialized()
        self._stream = sd.InputStream(
            device=self.in_device, samplerate=SAMPLE_RATE, channels=1,
            dtype="float32", latency=0.1, callback=self._in_callback,
        )
        self._stream.start()

    def stop_receiving(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def _clear_buffer(self):
        with self._buf_lock:
            self._chunks.clear()
            self._chunks_len = 0

    def snapshot_rx(self):
        """Everything captured so far, flattened into one array."""
        with self._buf_lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            flat = np.concatenate(self._chunks)
            self._chunks.clear()
            self._chunks.append(flat)
            max_len = int(RX_BUFFER_SECONDS * SAMPLE_RATE)
            if len(flat) > max_len:
                flat = flat[-max_len:]
                self._chunks[0] = flat
                self._chunks_len = len(flat)
            return flat.copy()

    def is_transmitting(self):
        """True for the whole span of a send() call, so callers polling the
        RX buffer (the decode thread) can sit out our own TX instead of
        racing send()'s pre/post clears and decoding our own leaked audio."""
        return self._transmitting.is_set()

    def consume_rx(self, upto_sample: int):
        """Drops everything up to `upto_sample` (index into the array
        `snapshot_rx()` returned) from the buffer. Must be called shortly
        after snapshot_rx() -- it assumes the buffer's front chunk is still
        the array snapshot_rx() flattened, so it can just slice it."""
        with self._buf_lock:
            if not self._chunks:
                return
            front = self._chunks[0]
            n = min(upto_sample, len(front))
            if n > 0:
                self._chunks[0] = front[n:]
                self._chunks_len -= n

    # -- transmit -------------------------------------------------------

    def send(self, tx_audio, ptt_lead=PTT_LEAD, ptt_tail=PTT_TAIL, retries=5):
        """Keys PTT, plays tx_audio, un-keys. Returns the key-to-unkey
        duration in seconds -- the frame's actual air time. RX capture keeps
        running throughout (see module docstring); half duplex is enforced
        by dropping whatever it picked up around our own transmission rather
        than by stopping the stream.

        A short retry loop remains as a fallback for ordinary WASAPI
        flakiness, on top of the COM-init fix in _ensure_com_initialized().
        """
        with self._tx_lock:
            _ensure_com_initialized()
            self._transmitting.set()
            self._clear_buffer()
            try:
                last_exc = None
                keyed_seconds = None
                for attempt in range(1, retries + 1):
                    try:
                        keyed_seconds = audio_io.transmit(
                            tx_audio, self.out_device, self.ptt,
                            samplerate=SAMPLE_RATE, ptt_lead=ptt_lead, ptt_tail=ptt_tail)
                        last_exc = None
                        break
                    except sd.PortAudioError as exc:
                        last_exc = exc
                        logging.getLogger(__name__).warning(
                            "OutputStream start failed (attempt %d/%d): %s", attempt, retries, exc)
                        time.sleep(min(1.0, 0.2 * attempt))
                if last_exc is not None:
                    raise last_exc
                return keyed_seconds
            finally:
                # Whatever leaked into the RX buffer during our own keyed
                # transmission (sidetone, RF front-end artifacts) is not a
                # frame worth decoding -- start listening fresh. Any residual
                # self-echo that still slips through is filtered at the
                # protocol layer (Link._handle_raw), by callsign, since a
                # blanket post-TX mute risks eating the peer's real reply
                # when turnaround is fast.
                self._clear_buffer()
                self._transmitting.clear()

    def close(self):
        self.stop_receiving()
        try:
            self.ptt.close()
        except Exception:
            pass
