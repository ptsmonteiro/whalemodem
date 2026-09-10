"""Connection-owned audio history and incremental OFDM acquisition."""

import numpy as np
from whale.rx_audio import DECODE_SAMPLE_RATE


class AudioHistory:
    """A bounded ring indexed in absolute receive samples."""

    def __init__(self, capacity):
        self.data = np.empty(capacity, dtype=np.float32)
        self.start = self.end = 0

    def append(self, start, samples):
        samples = np.asarray(samples, dtype=np.float32)
        if start != self.end:
            self.start = self.end = start
        end = start + len(samples)
        if len(samples) > len(self.data):
            samples = samples[-len(self.data):]
            start = end - len(samples)
        split = min(len(samples), len(self.data) - start % len(self.data))
        self.data[start % len(self.data):start % len(self.data) + split] = samples[:split]
        self.data[:len(samples) - split] = samples[split:]
        self.end = end
        self.start = max(self.start, end - len(self.data))

    def discard(self, end):
        self.start = min(self.end, max(self.start, end))

    def read(self, start=None, end=None):
        start = self.start if start is None else start
        end = self.end if end is None else end
        if not self.start <= start <= end <= self.end:
            raise ValueError("audio is outside retained history")
        offset = start % len(self.data)
        split = min(end - start, len(self.data) - offset)
        return np.concatenate((self.data[offset:offset + split],
                               self.data[:end - start - split]))


class OfdmSearch:
    """Search fixed new start-position intervals, shared by matching preambles."""

    def __init__(self, phy, start):
        self.phy = phy
        self.cursor = start
        self.block = int(0.25 * DECODE_SAMPLE_RATE)
        self.guard = int(0.02 * DECODE_SAMPLE_RATE)
        self.preamble = phy.n_preamble_symbols * phy.symbol_len
        self.candidates = []
        self.windows = 0

    def update(self, audio):
        self.cursor = max(self.cursor, audio.start)
        self.candidates = [c for c in self.candidates if c[1] >= audio.start]
        # Fixed windows make acquisition independent of callback chunk sizes.
        # Context on each side keeps the Hilbert envelope's edges outside the
        # searched interval. Only the overlap needed by acquisition is reread.
        while self.cursor + self.block + self.preamble - 1 + self.guard <= audio.end:
            low = max(audio.start, self.cursor - self.guard)
            high = self.cursor + self.block + self.preamble - 1 + self.guard
            window = audio.read(low, high)
            confidence, at, hz = self.phy.acquire(
                window,
                search_slice=slice(self.cursor - low, self.cursor - low + self.block))
            at += low
            self.windows += 1
            if confidence >= 0.12:
                # Confidence uses each window's RMS. Compare unnormalized
                # correlation across windows, otherwise a partial preamble
                # preceded by silence can outrank the correctly aligned one.
                candidate = (confidence, at, hz, confidence * float(np.std(window)))
                nearby = next((i for i, old in enumerate(self.candidates)
                               if abs(old[1] - at) < self.preamble), None)
                if nearby is None:
                    self.candidates.append(candidate)
                elif candidate[3] > self.candidates[nearby][3]:
                    self.candidates[nearby] = candidate
            self.cursor += self.block


class OfdmReceiver:
    def __init__(self, profile, search):
        self.profile = profile
        self.search = search
        self.attempted = set()
        self.frame_samples = (search.phy.symbol_len
                              * profile.codec.streaming_phy.total_ofdm_symbols())

    def decode(self, audio):
        self.search.update(audio)
        self.attempted = {key for key in self.attempted if key[0] >= audio.start}
        for confidence, start, hz, _ in sorted(self.search.candidates, key=lambda c: c[1]):
            key = (start, hz)
            end = start + self.frame_samples
            if key in self.attempted or end > audio.end:
                continue
            self.attempted.add(key)
            # The mode's existing estimator, demapper, FEC and CRC remain in
            # charge. Acquisition is reused; no whole-buffer search runs here.
            result = self.profile.decode(audio.read(start, end),
                                         acquisition=(confidence, 0, hz))
            result["start_sample"] = start - audio.start
            if result.get("payload") is not None:
                result["start_index"] = start - audio.start
                result["end_index"] = end - audio.start
                return result
        pending = [c for c in self.search.candidates
                   if c[1] + self.frame_samples > audio.end]
        return {"payload": None, "confidence": max((c[0] for c in pending), default=0.0)}


class ReceiveStream:
    def __init__(self, capacity):
        self.audio = AudioHistory(capacity)
        self.generation = None
        self.searches = {}
        self.receivers = {}

    @property
    def cursor(self):
        return (self.generation, self.audio.end)

    def append(self, generation, start, samples):
        if generation != self.generation or start != self.audio.end:
            self.searches.clear()
            self.receivers.clear()
            self.audio.start = self.audio.end = start
        self.generation = generation
        self.audio.append(start, samples)

    def decode(self, profile):
        phy = getattr(getattr(profile, "codec", None), "streaming_phy", None)
        if phy is None:
            return None
        if profile.name not in self.receivers:
            key = (phy.fft_size, phy.cp_len, tuple(phy.active_bins),
                   phy.n_preamble_symbols, phy._preamble_bin_symbols.tobytes())
            if key not in self.searches:
                self.searches[key] = OfdmSearch(phy, self.audio.start)
            self.receivers[profile.name] = OfdmReceiver(profile, self.searches[key])
        return self.receivers[profile.name].decode(self.audio)
