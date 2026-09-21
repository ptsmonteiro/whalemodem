"""Receive-side implementation for :mod:`whale.link`.

This module is internal.  ``whale.link.Link`` remains the public link class;
the mixin only keeps its audio acquisition and decode machinery separate from
session and ARQ orchestration.
"""

import logging
import os
import sys
import threading
import time

import numpy as np

from whale import link_protocol as protocol
from whale.streaming import ReceiveStream


logger = logging.getLogger("whale.link")

DECODE_POLL_INTERVAL = 0.15
DECODE_POLL_BUDGET_FRACTION = 0.5
DECODE_EXPENSIVE_DUTY = 0.2
DECODE_RECENT_SUCCESS_SECONDS = 60.0
DECODE_COST_REPORT_INTERVAL = 30.0


def _link_setting(name):
    """Read a public decoder setting, including live legacy overrides."""
    link_module = sys.modules.get("whale.link")
    return getattr(link_module, name, globals()[name])


def _decode_snr(result):
    """Return this decode's receive SNR as ``(dB, kind)``, or ``(None, None)``."""
    tone_snr = result.get("tone_snr_db")
    if tone_snr is not None and np.isfinite(tone_snr):
        return float(tone_snr), "tone"

    carrier_snr = result.get("carrier_snr_db")
    if carrier_snr is not None:
        finite = np.asarray(carrier_snr, dtype=float)
        finite = finite[np.isfinite(finite)]
        if finite.size:
            return float(np.median(finite)), "median carrier"

    snr = result.get("snr_db")
    if snr is not None and np.isfinite(snr):
        return float(snr), "effective sync"

    return None, None


def _decode_snr_summary(result):
    """Return the mode's receive-SNR diagnostic in a common log format."""
    snr_db, kind = _decode_snr(result)
    if snr_db is None:
        return "SNR unavailable"
    return f"SNR {snr_db:.1f} dB ({kind})"


def net_bits_per_second(mode):
    """Net application bit/s carried by a full-capacity DATA frame."""
    airtime = mode.airtime(mode.chunk_size)
    if not airtime:
        return 0.0
    return mode.chunk_size * 8.0 / airtime


class _DecodeCost:
    """Per-profile CPU and wall time spent inside ``profile.decode()``."""

    __slots__ = ("attempts", "frames", "cpu", "wall", "max_cpu",
                 "audio_seconds", "last_attempt_at", "last_frame_at")

    def __init__(self):
        self.attempts = self.frames = 0
        self.cpu = self.wall = self.max_cpu = 0.0
        self.audio_seconds = 0.0
        self.last_attempt_at = None
        self.last_frame_at = None

    def add(self, cpu, wall, audio_seconds=0.0, now=None):
        self.attempts += 1
        self.cpu += cpu
        self.wall += wall
        self.max_cpu = max(self.max_cpu, cpu)
        self.audio_seconds += max(0.0, audio_seconds)
        self.last_attempt_at = time.monotonic() if now is None else now

    def estimate(self, audio_seconds):
        if self.attempts == 0 or self.audio_seconds <= 0.0:
            return None
        return self.wall / self.audio_seconds * audio_seconds

    def summary(self):
        mean = (self.cpu / self.attempts * 1000.0) if self.attempts else 0.0
        rate = ((self.wall / self.audio_seconds * 1000.0)
                if self.audio_seconds > 0.0 else 0.0)
        return (f"{self.attempts} attempt(s)/{self.frames} frame(s), "
                f"cpu {self.cpu:.2f}s total, {mean:.1f} ms mean, "
                f"{self.max_cpu * 1000.0:.1f} ms max, wall {self.wall:.2f}s, "
                f"{rate:.1f} ms per audio second")


class _ReceiverMixin:
    """Audio acquisition and frame decoding for ``whale.link.Link``.

    Reads from the host ``Link``: ``mycall``, ``policy``, ``state``,
    ``modes``, ``peer_supported_modes``, ``rx_profile``, ``on_event``,
    ``_rx_packets``, ``transport``, ``_rx_keep_seconds`` and
    ``_confirm_rx_profile`` (the last defined on ``Link`` itself, since
    profile adoption is session state, not receive machinery). Owns and
    initializes the receive-thread/decode-cost attributes itself via
    ``_init_receiver``.
    """

    def _init_receiver(self):
        """Initialize receive-thread and session observation state."""
        self._rx_frequency_hint_hz = None
        self._last_peer_frame_at = None
        self._peer_unkeyed_at = None
        self._peer_unkeyed_observed_at = None
        self._stop = threading.Event()
        self._decode_cost = {}
        self._receive_stream = None
        self._decode_cost_reported_at = None
        self._decode_thread = threading.Thread(
            target=self._decode_loop, daemon=True)

    def _start_receiver(self):
        self.transport.start_receiving()
        self._decode_thread.start()

    def _stop_receiver(self):
        self._stop.set()
        self.transport.stop_receiving()
        if self._decode_thread.is_alive():
            self._decode_thread.join(
                timeout=2 * _link_setting("DECODE_POLL_INTERVAL") + 1.0)
        self._report_decode_cost(final=True)

    def _candidate_decode_profiles(self, snap=None):
        """Return the control mode and every plausible inbound DATA mode."""
        if self.state in {"CONNECTING", "LISTENING"}:
            return (self.modes.control,)
        candidates = [self.modes.control]
        data_profiles = [p for p in self.modes.modes
                         if p.mode_id in self.peer_supported_modes]
        for profile in (self.rx_profile, *data_profiles):
            if profile is not None and profile not in candidates:
                candidates.append(profile)
        return tuple(candidates)

    @staticmethod
    def _offset_decode_result(result, offset):
        for key in ("start_index", "sync_end_index", "end_index"):
            if result.get(key) is not None:
                result[key] += offset
        return result

    def _decode_attempt(self, profile, audio, offset=0):
        cpu0, wall0 = time.thread_time(), time.perf_counter()
        hint = (self._rx_frequency_hint_hz
                if self.policy.track_frequency_offset else None)
        stream_result = (self._receive_stream.decode(profile)
                         if self._receive_stream is not None else None)
        if stream_result is not None:
            result = stream_result
        elif hint is None or not getattr(profile, "supports_frequency_hint", False):
            result = profile.decode(audio)
        else:
            result = profile.decode(audio, freq_hint_hz=hint)
            start = result.get("start_sample")
            complete = (start is not None and
                        start + int(np.ceil(profile.airtime(profile.chunk_size)
                                            * profile.rx_sample_rate)) <= len(audio))
            if (start is None or complete) and result.get("payload") is None:
                result = profile.decode(audio)
        cpu = time.thread_time() - cpu0
        self._decode_cost.setdefault(profile.name, _DecodeCost()).add(
            cpu, time.perf_counter() - wall0,
            len(audio) / profile.rx_sample_rate)
        result["decode_cpu_seconds"] = cpu
        return self._offset_decode_result(result, offset)

    def _decode_loop(self):
        """Continuously transfer transport audio through the frame decoder."""
        retry = False
        if hasattr(self.transport, "read_rx"):
            from whale.transport import RX_BUFFER_SECONDS, RX_SAMPLE_RATE
            self._receive_stream = ReceiveStream(int(RX_BUFFER_SECONDS * RX_SAMPLE_RATE))
        while not self._stop.is_set():
            poll_interval = _link_setting("DECODE_POLL_INTERVAL")
            if self.transport.is_transmitting():
                time.sleep(poll_interval)
                continue
            if self._receive_stream is not None:
                stream = self._receive_stream
                generation, start, samples = self.transport.read_rx(stream.cursor)
                changed = generation != stream.generation or start != stream.audio.end
                stream.append(generation, start, samples)
                if not len(samples) and not changed and not retry:
                    self._report_decode_cost()
                    time.sleep(poll_interval)
                    continue
                snap = stream.audio.read()
            else:
                snap = self.transport.snapshot_rx()
            if len(snap) > 0:
                if self._decode_one(snap):
                    retry = True
                    self._report_decode_cost()
                    continue
            retry = False
            self._report_decode_cost()
            time.sleep(poll_interval)

    def _consume_rx(self, end):
        if self._receive_stream is None:
            self.transport.consume_rx(end)
        else:
            stream = self._receive_stream
            end += stream.audio.start
            self.transport.consume_rx_through(stream.generation, end)
            stream.audio.discard(end)

    def _report_decode_cost(self, final=False):
        now = time.monotonic()
        if self._decode_cost_reported_at is None:
            self._decode_cost_reported_at = now
            if not final:
                return
        interval = _link_setting("DECODE_COST_REPORT_INTERVAL")
        if not final and now - self._decode_cost_reported_at < interval:
            return
        self._decode_cost_reported_at = now
        for name, cost in sorted(self._decode_cost.items()):
            logger.info("[%s] decode cost%s at %s: %s", self.mycall,
                        " (session total)" if final else "", name, cost.summary())

    def _decode_one(self, snap) -> bool:
        """Decode and consume one frame or near-miss from ``snap``."""
        snap_observed_at = time.monotonic()
        profiles = self._candidate_decode_profiles(snap)
        results = []

        def accept_checked(profile, result):
            payload = result.get("payload")
            if payload is None:
                return False
            if (self._receive_stream is not None and
                    self.transport.rx_generation != self._receive_stream.generation):
                return False
            decoded = protocol.decode_air_header(payload[:protocol.AIR_HEADER_LEN])
            if decoded is None:
                return False
            ptype, mode_id, body_len, inline = decoded
            body_profile = self.modes.by_id.get(mode_id)
            remainder = payload[protocol.AIR_HEADER_LEN:]
            if (body_profile is not profile or len(remainder) != body_len
                    or not protocol.valid_air_shape(
                        ptype, body_profile, body_len, inline,
                        self.modes.control.mode_id)):
                return False
            freq_offset = result.get("freq_offset_hz", result.get("cfo_hz"))
            if (self.policy.track_frequency_offset and freq_offset is not None
                    and np.isfinite(freq_offset)):
                self._rx_frequency_hint_hz = float(freq_offset)
            end = result.get("end_index", len(snap))
            self._consume_rx(end)
            self._finish_air_packet(ptype, inline + remainder, profile, snap, end,
                                    result, snap_observed_at)
            return True

        control = self.modes.control
        control_result = self._decode_attempt(control, snap)
        results.append((control, control_result))
        if accept_checked(control, control_result):
            return True

        data_profiles = tuple(profile for profile in profiles if profile is not control)
        for profile in (self._budgeted_candidates(data_profiles, snap)
                        if data_profiles else ()):
            result = self._decode_attempt(profile, snap)
            results.append((profile, result))
            if accept_checked(profile, result):
                return True
        # TODO: only streaming.py's OfdmReceiver publishes end_index, so a
        # non-streaming OFDM mode (hf6, hf5) whose CRC fails always lands in
        # pending and is read as "frame still arriving" -- never consumed,
        # never counted as a near miss. Its failures are invisible in
        # acquisition_count. Have ofdm49 publish end_index/sync_end_index, or
        # key this on the mode rather than on the field's presence.
        pending = [result for candidate, result in results
                   if result.get("confidence", 0) >= candidate.confidence_threshold
                   and "end_index" not in result]
        if not pending:
            near_misses = [result for _, result in results if "end_index" in result]
            if near_misses:
                result = min(near_misses,
                             key=lambda item: item.get("sync_end_index",
                                                       item["end_index"]))
                skip = result.get("sync_end_index", result["end_index"])
                self._capture_near_miss(snap, result.get("confidence", 0))
                self._consume_rx(skip)
                return True
        self._prune_stale(len(snap))
        return False

    def _budgeted_candidates(self, profiles, snap):
        """Schedule candidates within the retained audio window's budget."""
        now = time.monotonic()
        seconds = len(snap) / max(p.rx_sample_rate for p in profiles)
        budget = (_link_setting("DECODE_POLL_BUDGET_FRACTION")
                  * self._rx_keep_seconds)

        def estimate(profile):
            cost = self._decode_cost.get(profile.name)
            return 0.0 if cost is None else (cost.estimate(seconds) or 0.0)

        plan, deferred, spent = [], [], 0.0
        for profile in sorted(profiles, key=estimate):
            cost = self._decode_cost.get(profile.name)
            recent = (cost is not None and cost.last_frame_at is not None
                      and now - cost.last_frame_at
                      <= _link_setting("DECODE_RECENT_SUCCESS_SECONDS"))
            estimated = estimate(profile)
            if recent or spent + estimated <= budget:
                plan.append(profile)
                spent += estimated
                continue
            since = now - (cost.last_attempt_at or 0.0)
            deferred.append((since - estimated
                             / _link_setting("DECODE_EXPENSIVE_DUTY"), profile))
        if deferred:
            overdue, profile = max(deferred, key=lambda item: item[0])
            if overdue >= 0.0:
                plan.append(profile)
        return plan

    def _finish_air_packet(self, ptype, body, profile, snap, end, decode_result,
                           snap_observed_at):
        trailing = max(0, len(snap) - end)
        self._peer_unkeyed_at = (snap_observed_at
                                 - trailing / profile.rx_sample_rate)
        self._peer_unkeyed_observed_at = time.monotonic()
        cost = self._decode_cost.get(profile.name)
        if cost is not None:
            cost.frames += 1
            cost.last_frame_at = time.monotonic()
        cpu = decode_result.get("decode_cpu_seconds")
        logger.info("[%s] decoded %s body at profile %s (%s; %s)", self.mycall,
                    protocol.ptype_name(ptype), profile.name,
                    "decode cpu unmeasured" if cpu is None
                    else f"decode cpu {cpu * 1000.0:.1f} ms",
                    _decode_snr_summary(decode_result))
        if ptype == protocol.PT_DATA:
            snr_db, _ = _decode_snr(decode_result)
            if snr_db is not None:
                self.on_event("SNR", snr_db=snr_db)
            self.on_event("BITRATE", direction="RX", mode_id=profile.mode_id,
                          bits_per_second=net_bits_per_second(profile))
        self._handle_raw(bytes([ptype]) + body, profile)

    def _capture_near_miss(self, snap, confidence):
        """Save diagnostic audio when ``WHALE_CAPTURE_DIR`` is configured."""
        directory = os.environ.get("WHALE_CAPTURE_DIR")
        if not directory:
            return
        try:
            os.makedirs(directory, exist_ok=True)
            name = (f"nearmiss_{self.mycall}_{time.time():.3f}_c{confidence:.2f}"
                    f"_rx{self.rx_profile.name}.npy")
            np.save(os.path.join(directory, name), np.asarray(snap, dtype=np.float32))
        except Exception:
            logger.exception("[%s] near-miss capture failed", self.mycall)

    def _prune_stale(self, snap_len):
        """Discard searched audio while retaining one full frame window."""
        keep = int(self._rx_keep_seconds * max(
            p.rx_sample_rate for p in self._candidate_decode_profiles()))
        if snap_len > keep:
            self._consume_rx(snap_len - keep)

    def _handle_raw(self, raw: bytes, profile):
        """Validate receive provenance and queue one decoded link packet."""
        if len(raw) < 1:
            return
        ptype, body = raw[0], raw[1:]
        if ptype in (protocol.PT_CONNECT, protocol.PT_CONNECT_ACK):
            src, _ = protocol.decode_call_pair(body)
            if src == self.mycall:
                logger.info("[%s] dropping self-echoed %s", self.mycall,
                            protocol.ptype_name(ptype))
                return
        self._last_peer_frame_at = time.monotonic()
        if ptype in protocol.DATA_PLANE_TYPES:
            self._confirm_rx_profile(profile)
        logger.info("[%s] RX %s at %s (%d body byte(s))", self.mycall,
                    protocol.ptype_name(ptype), profile.name, len(body))
        self._rx_packets.put((ptype, body))
