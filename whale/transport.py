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
that isn't. So RX just stays open, and half duplex is enforced by voiding
whatever it captured immediately before/during/after our own TX instead --
see discard_rx(): the capture position keeps counting, and everything before
the discard marker is simply declared void, so a live decoder learns it has
been cut off rather than silently seeing the stream restart at zero.
"""

import collections
import ctypes
import logging
import sys
import threading
import time
from typing import NamedTuple

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

# How much captured audio the ring keeps before the oldest is dropped.
# Generous relative to one frame's ~7s worst case (255-byte payload at 300
# baud) so a frame straddling two reads is never lost.
#
# This is the transport's own bound and nothing else's: each live decoder
# keeps its own, much shorter, working window (whale/streaming.py), so decode
# cost does not grow with this. What this length buys is slack -- a reader
# that falls this far behind is told it lost audio (RxRead.gap) instead of
# being handed a discontinuity it cannot see.
RX_BUFFER_SECONDS = 10.0


class RxRead(NamedTuple):
    """One read of the receive stream.

    `start`/`end` are stream positions (see RadioTransport.rx_stream_position),
    not indices into anything the caller previously held. `gap` says audio
    between the requested position and `start` is gone -- trimmed by the ring
    or voided by discard_rx() -- so the reader must resynchronise rather than
    treat `audio` as continuous with its last read.
    """

    audio: np.ndarray
    start: int
    end: int
    gap: bool


class ReceiveStream:
    """The bounded receive ring and its one monotonic coordinate.

    Split out from RadioTransport because it is the whole receive contract:
    samples are written at one end, read by position at the other, and the
    only thing that can move discontinuously is announced (`RxRead.gap`).
    A test transport that reuses this class cannot drift from what the radio
    actually does, which is how a coordinate bug reached the air the last
    time the two were written separately.

    Writes come off the audio callback's realtime thread, so `write` is a
    deque append -- O(1) -- and the concatenate happens lazily in `read`, on
    whatever thread is decoding.
    """

    def __init__(self, sample_rate=RX_SAMPLE_RATE, seconds=RX_BUFFER_SECONDS):
        self.max_samples = int(seconds * sample_rate)
        self._chunks = collections.deque()
        self._chunks_len = 0
        # _total counts every sample ever written and never restarts, so a
        # position stays meaningful across a trim and across discard().
        # _origin is the position of _chunks[0][0]; _discarded is the
        # position before which the capture has been declared void, or None
        # if nothing ever has been.
        self._total = 0
        self._origin = 0
        self._discarded = None
        self._lock = threading.Lock()

    def write(self, samples):
        with self._lock:
            self._chunks.append(np.asarray(samples, dtype=np.float32))
            self._chunks_len += len(samples)
            self._total += len(samples)
            while self._chunks_len - len(self._chunks[0]) > self.max_samples:
                dropped = len(self._chunks.popleft())
                self._chunks_len -= dropped
                self._origin += dropped
            return self._total

    def discard(self):
        """Declares everything written so far void; returns the new floor."""
        with self._lock:
            self._chunks.clear()
            self._chunks_len = 0
            self._origin = self._total
            self._discarded = self._total
            return self._discarded

    def read(self, since=None):
        """Retained audio from stream position `since` onward.

        Non-destructive: the stream owns its ring and no reader may shorten
        it. `since=None` means "whatever is retained". A read comes back with
        `gap` set whenever the reader has to resynchronise instead of joining
        what follows onto what it already had:

          - it starts later than asked, because the ring trimmed that audio;
          - or it starts at or after a discard(), because everything before
            that marker is void -- including audio the reader has already
            taken and may still be holding in a decode window, which is
            exactly what half duplex has to throw away.
        """
        with self._lock:
            flat = self._flatten_locked()
            origin, total = self._origin, self._total
            discarded = self._discarded
            floor = origin if discarded is None else max(origin, discarded)
            if since is None:
                start, gap = floor, False
            else:
                since = int(since)
                start = min(max(since, floor), total)
                gap = start > since or (discarded is not None
                                        and since <= discarded)
            return RxRead(flat[start - origin:].copy(), start,
                          origin + len(flat), gap)

    def _flatten_locked(self):
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        flat = np.concatenate(self._chunks)
        if len(flat) > self.max_samples:
            self._origin += len(flat) - self.max_samples
            flat = flat[-self.max_samples:]
        self._chunks.clear()
        self._chunks.append(flat)
        self._chunks_len = len(flat)
        return flat

    @property
    def position(self):
        """Total samples ever written: the stream's write position."""
        with self._lock:
            return self._total

    @property
    def discard_position(self):
        """Position before which the capture is void; 0 if none ever was."""
        with self._lock:
            return 0 if self._discarded is None else self._discarded


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

        # The receive ring and its monotonic coordinate live in
        # ReceiveStream, shared with the test transports so the fake and the
        # radio cannot disagree about the contract.
        self._rx = ReceiveStream()
        self._stream = None
        self._tx_lock = threading.Lock()  # serializes TX attempts
        self._transmitting = threading.Event()
        self._rx_decimator = rx_audio.ReceiveDecimator()

    # -- receive ------------------------------------------------------

    def _in_callback(self, indata, frames, time_info, status):
        if not hasattr(self, "_rx_decimator"):
            self._rx_decimator = rx_audio.ReceiveDecimator()
        self._rx.write(self._rx_decimator.process(indata[:, 0]))

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

    def discard_rx(self):
        """Declares everything captured so far void and returns the stream
        position the capture resumes from.

        This is how half duplex is enforced (see send()) and how the bench
        scripts flush audio left over from a previous trial. Deliberately
        *not* a reset: rx_stream_position keeps counting, so a reader holding
        an older position is told it was cut off (read_rx().gap) instead of
        being handed audio whose coordinates silently moved under it.
        """
        # Some safety tests construct a transport without running __init__;
        # keep the emergency send/close paths valid.
        if not hasattr(self, "_rx_decimator"):
            self._rx_decimator = rx_audio.ReceiveDecimator()
        if not hasattr(self, "_rx"):
            self._rx = ReceiveStream()
        self._rx_decimator.reset()
        return self._rx.discard()

    def read_rx(self, since=None):
        """Captured audio from stream position `since` onward. See
        ReceiveStream.read -- this is the whole receive interface."""
        return self._rx.read(since)

    def snapshot_rx(self):
        """Everything currently retained, flattened into one array."""
        return self._rx.read().audio

    @property
    def rx_stream_position(self):
        """Total 12 kHz samples ever captured: the stream's write position.

        Monotonic for the transport's whole life -- it survives the ring
        trimming its front and it survives discard_rx() -- so it is the one
        coordinate every receive index in the system is expressed in.
        """
        return self._rx.position

    @property
    def rx_discard_position(self):
        """Stream position before which the capture has been declared void."""
        return self._rx.discard_position

    def is_transmitting(self):
        """True for the whole span of a send() call, so callers polling the
        RX buffer (the decode thread) can sit out our own TX instead of
        racing send()'s pre/post clears and decoding our own leaked audio."""
        return self._transmitting.is_set()

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
            self.discard_rx()
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
                self.discard_rx()
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
            index = audio_io.find_device(self.radio.audio_output_name, "output")
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
