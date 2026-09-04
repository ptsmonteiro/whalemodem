"""Radio transport: continuous receive capture + keyed transmit, on top of
whale.hw (sound card lookup, PTT). The sole DSP operation here is the shared
anti-aliased conversion from the 48 kHz device stream to the 12 kHz receive
buffer; waveform-specific decoding remains outside the transport. Transmit
audio is handed to hw.audio_io.transmit() at 48 kHz.

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
import sys
import threading
import time

import numpy as np
import sounddevice as sd

from whale import afsk
from whale.hw import audio_io
from whale.hw import radios as radios_mod
from whale import rx_audio

TX_SAMPLE_RATE = audio_io.SAMPLE_RATE
CAPTURE_SAMPLE_RATE = audio_io.SAMPLE_RATE
RX_SAMPLE_RATE = rx_audio.DECODE_SAMPLE_RATE
# Backwards-compatible name for callers measuring transmitted arrays.
SAMPLE_RATE = TX_SAMPLE_RATE
if CAPTURE_SAMPLE_RATE != rx_audio.CAPTURE_SAMPLE_RATE:
    raise RuntimeError(
        f"audio capture runs at {CAPTURE_SAMPLE_RATE} Hz but the receive "
        f"decimator expects {rx_audio.CAPTURE_SAMPLE_RATE} Hz")

_COINIT_APARTMENTTHREADED = 0x2
_com_ready = threading.local()


def _ensure_com_initialized():
    """PortAudio's WASAPI backend needs COM initialized on the calling
    thread. The main thread gets this for free (implicitly, or via whatever
    else touched COM first); a plain threading.Thread does not, and opening
    a WASAPI OutputStream from one reliably fails with PaErrorCode -9999
    (WdmSyncIoctl) on this machine -- 100% reproducible, regardless of which
    radio/device, order, or timing. One CoInitializeEx call per thread
    before its first stream fixes it.

    WASAPI, and therefore COM, is Windows-only (see audio_io._DEFAULT_HOST_API);
    on macOS/Linux this is a no-op rather than an AttributeError, since
    ctypes.windll does not exist off Windows."""
    if sys.platform != "win32" or getattr(_com_ready, "done", False):
        return
    ctypes.windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    _com_ready.done = True

# The dead air a keying still carries: opening the output stream and
# filling its first buffer, between PTT assertion and the first sample
# actually leaving the card. Not a knob -- it is what the audio stack does,
# recorded here so the keying-length arithmetic can account for it.
#
# MEASURED end to end rather than reasoned from audio_io's latency=0.1: an
# acceptance run logs `Ns audio, Ms keyed` for every transmission, and
# M - N was measured after subtracting the then-configured PTT sleeps. Across
# 44 keyings spanning
# both radios, all three profiles and every frame type, that came out at
# 0.15-0.16s -- not the 0.13 previously assumed from the requested stream
# latency, which left the derived chunk sizes ~20ms over budget. Take the
# worst; a keying budget wants the pessimistic end.
STREAM_FILL = 0.16

# KEYING_OVERHEAD_SECONDS records the transport contribution to total PTT
# occupancy. It does not participate in the useful-frame size restriction.
_KEYING_OVERHEAD = STREAM_FILL
if abs(_KEYING_OVERHEAD - afsk.KEYING_OVERHEAD_SECONDS) > 0.005:
    raise RuntimeError(
        f"keying overhead drifted: transport says {_KEYING_OVERHEAD:.3f}s "
        f"(STREAM_FILL {STREAM_FILL}) "
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
# length (currently about 3 ms per second for each CPFSK candidate on the
# development machine; scripts/benchmark_rx.py keeps this reproducible).
RX_BUFFER_SECONDS = 10.0


class RadioTransport:
    """One radio: continuous RX capture + on-demand keyed TX."""

    def __init__(self, radio_name: str, radio_config=None, receive_only: bool = False):
        self.radio = radios_mod.get_radio(radio_name, radio_config)
        self.out_device, self.in_device = self.radio.devices()
        # receive_only is a safety construction, not a convenience: it takes
        # the audio and never opens a PTT backend at all, so there is no
        # object in this process capable of keying the radio. send() then
        # raises rather than keying, and close() has no transmitter to
        # account for. Characterisation benches use it for the listening end
        # of a one-way test -- the receiving radio must not transmit, and
        # this makes that a property of the object rather than of every
        # caller remembering which transport is which. It is also the only
        # way to bench a radio whose CI-V will not answer (see the IC-705's
        # `CI-V USB Port` menu setting), since PTT discovery is otherwise
        # required before the audio device can be used.
        self.receive_only = bool(receive_only)
        self.ptt = None if self.receive_only else self.radio.ptt()

        # A deque of 12 kHz receive chunks rather than one growing array: the audio
        # callback runs on PortAudio's realtime thread, and re-concatenating
        # an array that can be RX_BUFFER_SECONDS long on every
        # callback -- ~10x/sec -- is real work on that thread. Appending a
        # chunk is O(1); the expensive concatenate+trim happens lazily in
        # snapshot_rx(), called from the (non-realtime) decode thread.
        self._chunks = collections.deque()
        self._chunks_len = 0
        self._buf_lock = threading.Lock()
        self._stream = None
        self._tx_lock = threading.Lock()  # serializes TX attempts
        self._transmitting = threading.Event()
        self._rx_decimator = rx_audio.ReceiveDecimator()

    # -- receive ------------------------------------------------------

    def _in_callback(self, indata, frames, time_info, status):
        with self._buf_lock:
            if not hasattr(self, "_rx_decimator"):
                self._rx_decimator = rx_audio.ReceiveDecimator()
            decoded = self._rx_decimator.process(indata[:, 0])
            self._chunks.append(decoded)
            self._chunks_len += len(decoded)
            max_len = int(RX_BUFFER_SECONDS * RX_SAMPLE_RATE)
            while self._chunks_len - len(self._chunks[0]) > max_len:
                self._chunks_len -= len(self._chunks.popleft())

    def start_receiving(self):
        if self._stream is not None:
            return
        _ensure_com_initialized()
        self._stream = sd.InputStream(
            device=self.in_device, samplerate=CAPTURE_SAMPLE_RATE, channels=1,
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
            if hasattr(self, "_rx_decimator"):
                self._rx_decimator.reset()
            else:
                # Some safety tests construct a transport without running
                # __init__; keep the emergency send/close paths valid.
                self._rx_decimator = rx_audio.ReceiveDecimator()

    def snapshot_rx(self):
        """Everything captured so far, flattened into one array."""
        with self._buf_lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            flat = np.concatenate(self._chunks)
            self._chunks.clear()
            self._chunks.append(flat)
            max_len = int(RX_BUFFER_SECONDS * RX_SAMPLE_RATE)
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

    def send(self, tx_audio, ptt_lead=None, ptt_tail=None, retries=5):
        """Keys PTT, plays tx_audio, un-keys. Returns the key-to-unkey
        duration in seconds -- the frame's actual air time. RX capture keeps
        running throughout (see module docstring); half duplex is enforced
        by dropping whatever it picked up around our own transmission rather
        than by stopping the stream.

        A short retry loop remains as a fallback for ordinary WASAPI
        flakiness, on top of the COM-init fix in _ensure_com_initialized().
        It has one hard limit: it will not go round again while the PTT
        cannot account for the transmitter. audio_io.transmit() un-keys in a
        finally that cannot raise, but "tried to un-key" is not "un-keyed" --
        if the radio never acknowledged it (ptt.key_state_unknown), keying
        again stacks a second transmission on top of a state nobody can read,
        over the same bus that just failed. Giving up and raising is the
        correct outcome there; the caller sees a failed send, which is what
        happened, and the operator sees ptt.py's alarm about the radio.

        This is the exact shape of the incident the un-keying work exists for:
        RF from the transmission desensed the USB bus, the output stream
        raised PaErrorCode -9996, and the retry loop's natural instinct was
        to key again -- into a radio whose CI-V had stopped answering.
        """
        if self.receive_only:
            raise RuntimeError(
                f"{self.radio.name} was opened receive-only and must not transmit")
        ptt_lead = 0.0 if ptt_lead is None else ptt_lead
        ptt_tail = 0.0 if ptt_tail is None else ptt_tail
        with self._tx_lock:
            _ensure_com_initialized()
            self._transmitting.set()
            self._clear_buffer()
            try:
                last_exc = None
                keyed_seconds = None
                log = logging.getLogger(__name__)
                for attempt in range(1, retries + 1):
                    try:
                        keyed_seconds = audio_io.transmit(
                            tx_audio, self.out_device, self.ptt,
                            samplerate=TX_SAMPLE_RATE, ptt_lead=ptt_lead, ptt_tail=ptt_tail)
                        last_exc = None
                        break
                    except sd.PortAudioError as exc:
                        last_exc = exc
                        log.warning(
                            "OutputStream start failed (attempt %d/%d): %s", attempt, retries, exc)
                        if getattr(self.ptt, "key_state_unknown", False):
                            log.error(
                                "not retrying: the PTT could not confirm the un-key, so the "
                                "transmitter's state is unknown. Check the radio.")
                            break
                        time.sleep(min(1.0, 0.2 * attempt))
                        self._reresolve_out_device()
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

    def _reresolve_out_device(self):
        """Looks the TX device up by name again, in case it re-enumerated.

        A USB sound card that dropped off the bus and came back can return at
        a different PortAudio index, and a retry aimed at the old one would
        then fail forever for a reason that has nothing to do with the radio.
        Re-resolving by name (the same lookup __init__ used, via radios.py) is
        cheap and removes that whole class of stuck retry.

        Deliberately *not* treated as the diagnosis, though. In the incident
        that prompted this the indices did not move at all -- the bus was
        desensed by our own RF and the devices came back where they were --
        so a recovery path built around a stale index would have fixed
        nothing. Both indices are logged when they differ so the next
        occurrence can be told apart from that one, rather than a silent
        rebind quietly hiding which failure happened.

        Its real limit is worth knowing: sd.query_devices() reads the device
        table PortAudio built when it initialised, so a card that moved while
        this process was running may still read as its old index here.
        Forcing a rescan means sd._terminate()/sd._initialize(), which would
        destroy the input stream this transport keeps open for its whole life
        -- and stop/start churn on this hardware is its own documented
        failure (see the module docstring). So this is best-effort: if the
        indices really did move under a live PortAudio, the process needs
        restarting and no amount of retrying here will substitute.

        Never raises. It runs on the recovery path, where the interesting
        exception is the one already in flight.
        """
        try:
            index = audio_io.find_device(self.radio.audio_name, "output")
        except Exception as exc:
            # LookupError (the card is genuinely gone or now ambiguous), or
            # anything PortAudio throws while enumerating a sick bus.
            logging.getLogger(__name__).warning(
                "could not re-resolve the TX device for %s: %s", self.radio.name, exc)
            return
        if index != self.out_device:
            logging.getLogger(__name__).warning(
                "TX device for %s moved from index %d to %d; using the new one",
                self.radio.name, self.out_device, index)
            self.out_device = index

    def close(self):
        self.stop_receiving()
        if self.ptt is None:
            return
        try:
            # ptt.close() un-keys first, and IcomCivPtt.key(False) no longer
            # raises -- but this is the last un-key of the process, so a
            # failure here is exactly the one that must not be swallowed
            # silently. It stays non-fatal (close() is called from shutdown
            # paths that have nothing better to do with an exception) and
            # becomes loud instead.
            self.ptt.close()
        except Exception as exc:
            logging.getLogger(__name__).error(
                "closing PTT for %s failed (%s: %s). THE TRANSMITTER MAY STILL BE KEYED.",
                self.radio.name, type(exc).__name__, exc)
