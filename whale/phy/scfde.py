"""Single-carrier frequency-domain-equalized PHY for the FM handheld family.

Shared by the FM SC-FDE rungs. A block is a DFT-spread single carrier: 64
constellation symbols are transformed to 64 occupied bins of a 384-point
spectrum at 12 kHz, so the transmitted waveform is a filtered single carrier
with OFDM's per-bin equalizer and roughly 4-5 dB less peak-to-average than
an OFDM block of the same geometry. That matters because the mic path of a
handheld carries a compressor, a limiter and a deviation clipper.

Geometry, common to every rung:

* 12 kHz receive rate, 48 kHz transmit rate (the shared RX front end).
* FFT 384 samples (32 ms), cyclic prefix 24 samples (2 ms), block 34 ms.
* Bins 15..78 at 31.25 Hz, i.e. 468.75-2437.5 Hz: 64 bins, 1,882 symbol/s.
* ~0.74 s preamble: a ramped constant-modulus burst that opens squelch and
  settles receive AGC, then two CAZAC sync blocks and two CAZAC training
  blocks.
* Scattered pilot blocks every `pilot_block_stride` data blocks, bracketed
  by one at each end of the payload section, giving a per-bin channel
  estimate that is linearly interpolated in time across the frame.

FM discriminator output is baseband audio, so there is no carrier frequency
offset to recover; only soundcard clock offset (~+-50 ppm) moves the
constellation. Detection is therefore coherent, with the residual common
phase of each data block tracked decision-directed on top of the
interpolated pilot channel. There is no differential-detection penalty.

Transmit pre-distortion tilts the occupied band upward in level so that the
receiver's de-emphasis leaves signal-to-noise roughly flat across the band.
The tilt is a fixed filter, so the per-bin equalizer removes it without
being told about it.

Simulated flat_nbfm C/N floor: not measured (see each rung's module).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property

import numpy as np
from scipy.signal import correlate, hilbert

from whale import framing, waveform
from whale.dsp import bits, interleave, ldpc
from whale.dsp.constellation import soft_bit_llrs
from whale.dsp.framing import PacketFrame
from whale.phy.ofdm49 import bits_to_symbols as _mapper
from whale.dsp.constellation import constellation_table

#: Receive-side sample rate; the transmit chain runs at 4x this.
SAMPLE_RATE = 12000
TX_FACTOR = 4
FFT_SIZE = 384
CP_LEN = 24
BIN_SPACING_HZ = SAMPLE_RATE / FFT_SIZE  # 31.25
FIRST_BIN = 15
SPREAD = 64  # occupied bins, and constellation symbols per block
SYNC_ROOT = 25
TRAIN_ROOT = 37
PILOT_ROOT = 43


def cazac(root: int, length: int = SPREAD) -> np.ndarray:
    """Constant-amplitude zero-autocorrelation sequence of `length`.

    Used both as the time-domain symbol vector of a pilot block, which keeps
    the preamble's peak-to-average at the constellation's own, and -- through
    its transform -- as the flat known spectrum the per-bin channel estimate
    divides by.
    """
    n = np.arange(length)
    return np.exp(-1j * np.pi * root * n * n / length)


def spread(symbols: np.ndarray) -> np.ndarray:
    """Symbol vectors -> occupied-bin values (the DFT spread of SC-FDE)."""
    return np.fft.fft(np.atleast_2d(symbols), axis=-1) / np.sqrt(SPREAD)


def despread(values: np.ndarray) -> np.ndarray:
    """Occupied-bin values -> symbol vectors."""
    return np.fft.ifft(np.atleast_2d(values), axis=-1) * np.sqrt(SPREAD)


def qpsk_points() -> np.ndarray:
    return constellation_table(2, mapper=_mapper)[0]


@dataclass(frozen=True)
class ScFdeMode(waveform.ModeDescription):
    """A parametric SC-FDE waveform FAMILY BASE, not itself a shippable mode.

    Every rung of this family shares the geometry above and the coherent
    detection below; a concrete rung (Vfs1Mode, Vfs2Mode, Vfs3Mode, ...)
    subclasses this and fixes `name`, `mode_id`, the constellation order and
    the LDPC rate -- that is the entire contributor cost of a new rung.

    `family_base` marks that pattern for tests/test_layering.py: a module in
    whale/modes/ that only parameterises a class carrying this marker is a
    legitimate family rung and not a PHY that wandered into the adapters
    package. `name` and `mode_id` are left as sentinels here on purpose so
    the bare base cannot be mistaken for a real mode; `__post_init__`
    refuses to construct one that has not overridden them.
    """

    family_base = True

    name: str = ""
    mode_id: int = -1
    bits_per_symbol_order: int = 2  # QPSK
    fec_rate: str = "1/2"
    n_codewords: int = 24
    pilot_block_stride: int = 10
    lead_in_seconds: float = framing.FM_SETTLING_HEAD_SECONDS
    tail_seconds: float = 0.1
    tilt_db: float = 6.0
    confidence_threshold: float = 0.55
    tx_sample_rate: int = field(default=48000, init=False)
    rx_sample_rate: int = field(default=SAMPLE_RATE, init=False)

    def __post_init__(self):
        if not self.name or self.mode_id < 0:
            raise ValueError(
                "ScFdeMode is a parametric waveform family base, not a "
                "shippable mode -- instantiate a concrete rung (e.g. "
                "Vfs2Mode) that sets its own name and mode_id")
        if self.bits_per_symbol_order not in (1, 2, 3):
            raise ValueError("SC-FDE family carries BPSK, QPSK or 8PSK")
        if self.fec_rate not in ldpc.INFORMATION_BITS:
            raise ValueError("unsupported FEC rate")
        if not isinstance(self.n_codewords, int) or self.n_codewords < 1:
            raise ValueError("frame budget cannot hold one codeword")
        if not isinstance(self.pilot_block_stride, int) or self.pilot_block_stride < 1:
            raise ValueError("pilot_block_stride must be an int >= 1")
        if self.lead_in_seconds < 0 or self.tail_seconds < 0:
            raise ValueError("lead-in and tail must be non-negative")
        if self.chunk_size <= 0 or self.max_payload_bytes > 65535:
            raise ValueError("payload capacity is outside native framing limits")

    # -- geometry ---------------------------------------------------------

    @cached_property
    def active_bins(self):
        return np.arange(FIRST_BIN, FIRST_BIN + SPREAD)

    @property
    def symbol_samples(self):
        return FFT_SIZE + CP_LEN

    @property
    def bits_per_block(self):
        return SPREAD * self.bits_per_symbol_order

    @property
    def coded_bits(self):
        return self.n_codewords * ldpc.N

    @property
    def payload_blocks(self):
        return (self.coded_bits + self.bits_per_block - 1) // self.bits_per_block

    @cached_property
    def block_plan(self):
        """(pilot block indices, data block indices, total) in the payload section."""
        pilots, data, index, remaining = [], [], 0, self.payload_blocks
        while remaining > 0:
            pilots.append(index)
            index += 1
            take = min(self.pilot_block_stride, remaining)
            data.extend(range(index, index + take))
            index += take
            remaining -= take
        pilots.append(index)
        index += 1
        return np.array(pilots), np.array(data), index

    @property
    def section_blocks(self):
        return self.block_plan[2]

    @property
    def preamble_blocks(self):
        return 4

    @property
    def total_blocks(self):
        return self.preamble_blocks + self.section_blocks

    @property
    def packet_bytes(self):
        return self.n_codewords * ldpc.INFORMATION_BITS[self.fec_rate] // 8

    @property
    def max_payload_bytes(self):
        return self.packet_bytes - 6  # native length and CRC32

    @property
    def chunk_size(self):
        return self.max_payload_bytes - framing.AIR_HEADER_BYTES

    @property
    def lead_in_samples(self):
        return round(self.lead_in_seconds * SAMPLE_RATE)

    # -- fixed sequences --------------------------------------------------

    @cached_property
    def tilt(self):
        """Per-bin transmit pre-distortion, unit mean power.

        A gentle rise across the occupied band so that after the receiver's
        de-emphasis the per-bin signal-to-noise is roughly flat. Deliberately
        a fixed shape rather than an analytic model of the radio: the real
        +-10 dB of mic, speaker and emphasis tilt is measured per frame by
        the per-bin equalizer, not predicted here.
        """
        freq = self.active_bins * BIN_SPACING_HZ
        span = np.log(freq / freq[0]) / np.log(freq[-1] / freq[0])
        gain = 10 ** (self.tilt_db / 20 * span)
        return gain / np.sqrt(np.mean(gain ** 2))

    @cached_property
    def sync_values(self):
        return np.repeat(spread(cazac(SYNC_ROOT)), 2, axis=0)

    @cached_property
    def train_values(self):
        return np.repeat(spread(cazac(TRAIN_ROOT)), 2, axis=0)

    @cached_property
    def pilot_values(self):
        """Known occupied-bin values of a pilot block, constant modulus.

        Constant modulus is what makes `observed / pilot` an unbiased per-bin
        channel estimate with the same noise on every bin.
        """
        value = spread(cazac(PILOT_ROOT))[0]
        return value / np.abs(value)

    @cached_property
    def points(self):
        return constellation_table(self.bits_per_symbol_order, mapper=_mapper)[0]

    @cached_property
    def interleaver(self):
        return interleave.multiplicative(self.coded_bits, 8101)

    @cached_property
    def whitener(self):
        return bits.pn_bits(self.coded_bits, 0x17E35)

    # -- waveform construction --------------------------------------------

    def _build(self, values, factor=1, guard=True):
        values = np.atleast_2d(values) * self.tilt
        core = FFT_SIZE * factor
        cp = CP_LEN * factor if guard else 0
        spectrum = np.zeros((len(values), core // 2 + 1), dtype=complex)
        spectrum[:, self.active_bins] = values
        body = np.fft.irfft(spectrum, n=core, axis=1)
        return (np.concatenate((body[:, -cp:], body), axis=1)
                if cp else body).reshape(-1)

    @cached_property
    def _frame(self) -> PacketFrame:
        return PacketFrame(self.packet_bytes)

    def _pack(self, payload):
        return self._frame.pack(payload)

    def encode(self, payload):
        payload = bytes(payload)
        order = self.bits_per_symbol_order
        info = np.unpackbits(np.frombuffer(self._pack(payload), np.uint8))
        k = ldpc.INFORMATION_BITS[self.fec_rate]
        info = np.pad(info, (0, self.n_codewords * k - len(info)))
        coded = np.concatenate([ldpc.encode(row, self.fec_rate)
                                for row in info.reshape(self.n_codewords, k)])
        stream = self.interleaver.spread(coded) ^ self.whitener
        stream = np.pad(stream, (0, self.payload_blocks * self.bits_per_block - len(stream)))
        labels = stream.reshape(-1, order)
        symbols = self.points[labels @ (1 << np.arange(order - 1, -1, -1))]
        symbols = symbols.reshape(self.payload_blocks, SPREAD)

        pilots, data, total = self.block_plan
        section = np.empty((total, SPREAD), dtype=complex)
        section[pilots] = self.pilot_values
        section[data] = spread(symbols)

        blocks = np.vstack((self.sync_values, self.train_values, section))
        body = self._build(blocks, factor=TX_FACTOR)
        lead = np.resize(self._build(self.pilot_values, factor=TX_FACTOR, guard=False),
                         self.lead_in_samples * TX_FACTOR).copy()
        fade = min(480, len(lead))
        lead[:fade] *= np.linspace(0, 1, fade)
        tail = np.zeros(round(self.tail_seconds * self.tx_sample_rate))
        audio = np.concatenate((lead, body, tail))
        return (audio / np.max(np.abs(audio))).astype(np.float32)

    # -- reception ---------------------------------------------------------

    def _extract(self, audio, start, count):
        """Occupied-bin values of `count` blocks starting at sample `start`."""
        if start < 0 or start + count * self.symbol_samples > len(audio):
            return None
        block = audio[start:start + count * self.symbol_samples]
        block = block.reshape(count, self.symbol_samples)[:, CP_LEN:]
        return np.fft.rfft(block, axis=1)[:, self.active_bins]

    def _fit(self, observed, reference):
        channel = (np.sum(observed * np.conj(reference), axis=0)
                   / np.sum(np.abs(reference) ** 2, axis=0))
        model = channel * reference
        power = float(np.mean(np.abs(model) ** 2))
        residual = float(np.mean(np.abs(observed - model) ** 2))
        return channel, residual / power if power > 0 else np.inf

    def _acquire(self, audio):
        """(start sample of the first sync block, EVM on the training blocks).

        The search metric is the per-bin training fit, not a time-domain
        correlation against the sync blocks. On a real handheld path that
        distinction decides whether the frame is found at all: mic and
        speaker filtering plus pre/de-emphasis put ~10 dB of tilt and a lot
        of group-delay ripple across 400-2500 Hz, which smears a matched
        filter's peak into the noise -- measured on an IC-705 capture, the
        correct alignment ranked 68,450th by correlation while fitting the
        training blocks to an EVM of 0.001. A per-bin fit is immune to
        exactly the phase distortion that defeats the correlator, because
        it solves for one complex gain per bin.

        Cost is kept down by gating on the keyed region and scanning it at a
        stride before refining to the sample.
        """
        span = self.total_blocks * self.symbol_samples
        if len(audio) < span:
            return None
        # Fit the WHOLE preamble, sync pair and training pair together. Each
        # pair on its own is one constant-modulus block repeated, and `_fit`
        # solves a free complex gain per bin, so a repeated pair fits any
        # CAZAC root exactly -- EVM against the training pair alone is ~0 on
        # the sync pair too, and acquisition slips by two blocks. Spanning
        # both roots makes one gain per bin explain both, which it can only
        # do at the true alignment.
        reference = np.vstack((self.sync_values, self.train_values)) * self.tilt

        def evm_at(start):
            observed = self._extract(audio, start, self.preamble_blocks)
            if observed is None:
                return np.inf
            return self._fit(observed, reference)[1]

        window = self.symbol_samples
        energy = np.convolve(np.asarray(audio, dtype=float) ** 2,
                             np.ones(window) / window, mode="same")
        live = np.flatnonzero(energy > 0.05 * energy.max()) if energy.max() > 0 else []
        if len(live) == 0:
            return None
        lo = max(0, int(live[0]) - window)
        hi = min(int(live[-1]), len(audio) - span)
        if hi <= lo:
            lo, hi = 0, max(1, len(audio) - span)

        stride = CP_LEN // 2  # never step past the guard interval
        coarse = min(range(lo, hi + 1, stride), key=evm_at)
        start = min(range(coarse - stride, coarse + stride + 1), key=evm_at)
        return start, evm_at(start)

    def _channel_track(self, section):
        """Per-bin channel for every data block, plus the per-bin noise power.

        The pilot blocks give an exact per-bin estimate at their own instants;
        between them the channel is interpolated linearly in the complex
        plane, which is what tracks the slow phase ramp that soundcard clock
        offset puts across the band. Noise is measured inside each pilot
        block against a lightly smoothed version of itself: the real channel
        is smooth across neighbouring bins (group-delay ripple, not delay
        spread), so what the smoother rejects is noise.
        """
        pilots, data, _ = self.block_plan
        estimates = section[pilots] / self.pilot_values

        window = 5
        kernel = np.ones(window) / window
        smoothed = np.apply_along_axis(
            lambda row: np.convolve(row, kernel, mode="same"), 1, estimates)
        edge = window // 2
        interior = slice(edge, -edge)
        residual = estimates[:, interior] - smoothed[:, interior]
        noise = float(np.mean(np.abs(residual) ** 2)) * window / (window - 1)

        channel = np.empty((len(data), SPREAD), dtype=complex)
        for bin_index in range(SPREAD):
            column = estimates[:, bin_index]
            channel[:, bin_index] = (
                np.interp(data, pilots, column.real)
                + 1j * np.interp(data, pilots, column.imag))
        return channel, max(noise, 1e-9)

    def decode(self, audio, **kwargs):
        # No option this family understands yet (no carrier frequency offset
        # to hint at an FM discriminator's baseband output); accept and
        # ignore whatever the shared callers pass, in the idiom every other
        # mode's decode() wrapper already uses.
        del kwargs
        result = {"synced": False, "payload": None, "crc_ok": False, "confidence": 0.0}
        audio = np.asarray(audio, dtype=float)
        if audio.ndim != 1 or not np.all(np.isfinite(audio)):
            return result
        acquired = self._acquire(audio)
        if acquired is None:
            return result
        start, evm = acquired
        confidence = 1 / (1 + evm)
        result.update(confidence=confidence, start_index=start)
        if confidence < self.confidence_threshold:
            return result
        result["synced"] = True

        section = self._extract(audio, start + self.preamble_blocks * self.symbol_samples,
                                self.section_blocks)
        if section is None:
            return result
        channel, noise = self._channel_track(section)
        _, data, _ = self.block_plan
        observed = section[data]

        # MMSE frequency-domain equalization. Zero-forcing would be wrong
        # here: SC-FDE folds every bin into every symbol, so a nulled bin
        # amplified by a ZF weight poisons the whole block.
        power = np.abs(channel) ** 2
        weights = np.conj(channel) / (power + noise)
        equalized = despread(observed * weights)
        # For MMSE the output is a scaled symbol plus residual: y = g*s + n
        # with E|n|^2 = g - g^2, g the mean per-bin MMSE gain of the block.
        gain = np.mean(power / (power + noise), axis=1)
        gain = np.maximum(gain, 1e-6)
        equalized = equalized / gain[:, None]

        # Coherent residual phase. There is no carrier offset on an FM
        # discriminator, so this is only the clock-offset phase that has
        # accumulated since the bracketing pilots; one decision-directed
        # estimate over 64 symbols is enough and costs no pilot overhead.
        decided = self.points[np.argmin(
            np.abs(equalized[..., None] - self.points), axis=-1)]
        phase = np.sum(equalized * np.conj(decided), axis=1)
        equalized = equalized * np.exp(-1j * np.angle(phase))[:, None]

        symbol_noise = np.repeat((1 - gain) / gain, SPREAD)
        llr = soft_bit_llrs(equalized.reshape(-1), self.bits_per_symbol_order,
                            np.maximum(symbol_noise, 1e-6),
                            mapper=_mapper)[:self.coded_bits]
        llr = self.interleaver.gather(llr * (1 - 2 * self.whitener.astype(float)))
        info, _, ok = ldpc.decode_batch(llr.reshape(self.n_codewords, ldpc.N),
                                        rate=self.fec_rate)
        packet = np.packbits(info.reshape(-1)[:self.packet_bytes * 8]).tobytes()
        payload, frame_meta = self._frame.unpack(packet)
        result.update(frame_meta)
        result.update(payload=payload, codewords_ok=int(np.count_nonzero(ok)),
                      snr_db=float(10 * np.log10(np.mean(gain / (1 - gain + 1e-12)))),
                      end_index=start + self.total_blocks * self.symbol_samples)
        return result

    # -- description --------------------------------------------------------

    def airtime(self, payload_len):
        return (self.lead_in_samples
                + self.total_blocks * self.symbol_samples
                + round(self.tail_seconds * SAMPLE_RATE)) / SAMPLE_RATE

    @property
    def bits_per_second(self):
        return self.chunk_size * 8 / self.airtime(self.max_payload_bytes)

    @property
    def band_hz(self) -> tuple[float, float]:
        return (float(self.active_bins[0] * BIN_SPACING_HZ),
                float(self.active_bins[-1] * BIN_SPACING_HZ))

    @property
    def modulation(self) -> str:
        names = {1: "BPSK", 2: "QPSK", 3: "8PSK"}
        pilots = len(self.block_plan[0])
        return (f"{SPREAD}-bin coherent {names[self.bits_per_symbol_order]} SC-FDE, "
                f"{SAMPLE_RATE / self.symbol_samples * SPREAD:.0f} Bd "
                f"({self.payload_blocks} data blocks, {pilots} pilot blocks)")

    @property
    def fec(self) -> str:
        return f"QC-LDPC {self.fec_rate}"

    def geometry(self):
        pilots, data, total = self.block_plan
        return {"fft_size": FFT_SIZE, "cp_len": CP_LEN, "bins": SPREAD,
                "bin_spacing_hz": BIN_SPACING_HZ,
                "band_lo_hz": self.band_hz[0], "band_hi_hz": self.band_hz[1],
                "bits_per_symbol": self.bits_per_symbol_order,
                "fec_rate": self.fec_rate, "n_codewords": self.n_codewords,
                "pilot_block_stride": self.pilot_block_stride,
                "pilot_blocks": len(pilots), "data_blocks": len(data),
                "section_blocks": total, "tilt_db": self.tilt_db,
                "net_bps": self.bits_per_second}
