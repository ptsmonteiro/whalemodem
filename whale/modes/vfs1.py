"""VFS1: the robust rung of the FM handheld SC-FDE ladder -- differential
BPSK over the shared SC-FDE core, rate-1/2 LDPC.

Everything about the waveform -- the 34 ms block, the 64 occupied bins, the
CAZAC preamble, the scattered pilot blocks, the MMSE per-bin equalizer --
comes from `whale.phy.scfde.ScFdeMode` and is identical to the coherent rungs
above. VFS1 changes exactly one thing: detection is differential.

That is deliberate and confined to this rung. DBPSK costs about 2.3 dB
against coherent BPSK, and in exchange it does not have to hold phase lock:
a handheld in a pocket, or mobile flutter, produces phase disturbance that
a coherent tracker loses lock on and a differential detector merely rides.
The rungs above VFS1 are coherent, because they are for conditions where
holding lock is not the problem.

Differential structure: symbol 0 of every data block is a known +1 reference
and carries no bit, so the differential chain re-anchors every 34 ms and a
lost block cannot propagate. That costs 1/64 of the symbols. Pilot blocks
are spaced further apart here than on the coherent rungs (16 data blocks
rather than 10): what they are still needed for is the static per-bin
amplitude and group-delay shape of the mic/speaker path, and the fast phase
that would otherwise set the spacing is handled by the differential
detector itself.

Simulated flat_nbfm C/N floor: not measured.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from whale import waveform
from whale.dsp import ldpc
from whale.phy import scfde
from whale.phy.scfde import SPREAD, ScFdeMode, despread, spread

MODE_ID = 24


@dataclass(frozen=True)
class Vfs1Mode(ScFdeMode):
    name: str = "vfs1"
    mode_id: int = MODE_ID
    bits_per_symbol_order: int = field(default=1, init=False)  # DBPSK
    fec_rate: str = "1/2"
    n_codewords: int = 10
    pilot_block_stride: int = 16

    @property
    def bits_per_block(self):
        """One reference symbol per block anchors the differential chain."""
        return SPREAD - 1

    def _differential(self, stream):
        """Bit stream -> BPSK symbols, +1 reference first in every block."""
        steps = 1.0 - 2.0 * stream.reshape(-1, self.bits_per_block).astype(float)
        symbols = np.ones((self.payload_blocks, SPREAD))
        symbols[:, 1:] = np.cumprod(steps, axis=1)
        return symbols.astype(complex)

    def encode(self, payload):
        info = np.unpackbits(np.frombuffer(self._pack(bytes(payload)), np.uint8))
        k = ldpc.INFORMATION_BITS[self.fec_rate]
        info = np.pad(info, (0, self.n_codewords * k - len(info)))
        coded = np.concatenate([ldpc.encode(row, self.fec_rate)
                                for row in info.reshape(self.n_codewords, k)])
        stream = self.interleaver.spread(coded) ^ self.whitener
        stream = np.pad(stream, (0, self.payload_blocks * self.bits_per_block - len(stream)))

        pilots, data, total = self.block_plan
        section = np.empty((total, SPREAD), dtype=complex)
        section[pilots] = self.pilot_values
        section[data] = spread(self._differential(stream))

        blocks = np.vstack((self.sync_values, self.train_values, section))
        lead = np.resize(self._build(self.pilot_values, factor=scfde.TX_FACTOR, guard=False),
                         self.lead_in_samples * scfde.TX_FACTOR).copy()
        fade = min(480, len(lead))
        lead[:fade] *= np.linspace(0, 1, fade)
        audio = np.concatenate((lead, self._build(blocks, factor=scfde.TX_FACTOR),
                                np.zeros(round(self.tail_seconds * self.tx_sample_rate))))
        return (audio / np.max(np.abs(audio))).astype(np.float32)

    def decode(self, audio):
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

        power = np.abs(channel) ** 2
        weights = np.conj(channel) / (power + noise)
        gain = np.maximum(np.mean(power / (power + noise), axis=1), 1e-6)
        equalized = despread(section[data] * weights) / gain[:, None]

        # Differential detection. No residual-phase estimate and no decision
        # feedback: the product of adjacent symbols cancels whatever common
        # phase the block carries, which is the whole point of this rung.
        # Both factors are noisy, so the effective noise is twice the
        # per-symbol MMSE residual (1 - gain) / gain.
        products = equalized[:, 1:] * np.conj(equalized[:, :-1])
        symbol_noise = np.maximum((1 - gain) / gain, 1e-6)
        llr = (np.real(products) / symbol_noise[:, None]).reshape(-1)[:self.coded_bits]
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

    @property
    def modulation(self) -> str:
        pilots = len(self.block_plan[0])
        return (f"{SPREAD}-bin differential BPSK SC-FDE, "
                f"{scfde.SAMPLE_RATE / self.symbol_samples * SPREAD:.0f} Bd "
                f"({self.payload_blocks} data blocks, {pilots} pilot blocks)")


def mode_for(**kwargs):
    return Vfs1Mode(**kwargs)


VFS1 = Vfs1Mode()
#: This module is the link-facing adapter; the waveform itself is in whale/phy.
assert isinstance(VFS1, waveform.WaveformMode)
MODES = (VFS1,)
