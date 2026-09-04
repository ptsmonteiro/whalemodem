"""Small deterministic screen for a possible HR0 direct-sequence BPSK family.

This is deliberately a disposable, production-tree prototype: it imports the
real K=9 codec and HF scenario, but it is not a negotiable mode.  The receiver
is given the frame boundary to one chip.  It has no transmitted-data or channel
oracle: coded bits are genuinely differentially encoded across PN-spread bit
intervals and recovered from adjacent despread symbols.

The geometry is intentionally useful as a quick rejection test.  A data frame
carries 30 application bytes in 7.78 s at 1.6 kchip/s (592 K9 coded bits plus
one reference bit, with twenty-one PN chips per bit).  A separately coded empty
ACK would need 112 coded bits and, at twelve chips/bit, 0.840 s before common
lead/tail, leaving ample room under a two-second compact-ACK budget.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np
from scipy.signal import hilbert

from whale import dsp
from whale.channel import SnrKind, SnrSpec
from whale.scenario import HfSsbScenario


FS = 48_000
CHIP_RATE = 1_600
SPS = FS // CHIP_RATE
CARRIER_HZ = 1_500.0
CODED_BITS = 592
CHIPS_PER_BIT = 21
REFERENCE_BITS = 1
TX_BITS = CODED_BITS + REFERENCE_BITS
CODEC = dsp.PacketCodec(CODED_BITS, dsp.interleave.multiplicative(CODED_BITS, 181),
                        0x2D51, code=dsp.K9)


@dataclass(frozen=True)
class Waveform:
    audio: np.ndarray
    chips: np.ndarray
    pn: np.ndarray


def _code() -> np.ndarray:
    rng = np.random.default_rng(0xD555)
    return rng.choice((-1.0, 1.0), CHIPS_PER_BIT)


def modulate(payload: bytes) -> Waveform:
    coded = 1.0 - 2.0 * CODEC.encode(payload)
    # states[0] is the transmitted DBPSK reference.  Each following state is
    # rotated by the corresponding coded bit, so no coded-bit phase is ever
    # observed against transmitted data or oracle CSI at the receiver.
    states = np.concatenate(([1.0], np.cumprod(coded)))
    pn = _code()
    rows = states[:, None] * pn[None, :]
    chips = rows.reshape(-1)
    # Half-sine chip pulse suppresses the rectangular pulse's distant sidelobes.
    pulse = np.sin(np.pi * (np.arange(SPS) + 0.5) / SPS)
    base = np.repeat(chips, SPS) * np.tile(pulse, len(chips))
    t = np.arange(len(base)) / FS
    audio = (0.20 * base * np.cos(2 * np.pi * CARRIER_HZ * t)).astype(np.float32)
    return Waveform(audio, chips, pn)


def symbol_candidates(audio: np.ndarray, waveform: Waveform) -> list[np.ndarray]:
    analytic = hilbert(np.asarray(audio, dtype=np.float64))
    t = np.arange(len(analytic)) / FS
    bb = analytic * np.exp(-2j * np.pi * CARRIER_HZ * t)
    pulse = np.sin(np.pi * (np.arange(SPS) + 0.5) / SPS)
    needed = TX_BITS * CHIPS_PER_BIT
    candidates: list[tuple[float, np.ndarray]] = []
    # Resolve fractional timing from PN correlation.  Integer offsets 0..3
    # form a small causal RAKE/search spanning 1.875 ms at 1.6 kchip/s.  This
    # is deliberately bounded by the disturbed preset's 2 ms spread.
    for offset in range(SPS):
        for delay in range(4):
            start = offset + delay * SPS
            if len(bb) - start < needed * SPS:
                continue
            values = (bb[start:start + needed * SPS].reshape(needed, SPS) @ pulse
                      / np.dot(pulse, pulse))
            rows = values.reshape(TX_BITS, CHIPS_PER_BIT)
            symbols = rows @ waveform.pn / CHIPS_PER_BIT
            # Signal-bearing PN correlations have persistent energy; retain a
            # few alternatives because the strongest echo need not be cleanest.
            candidates.append((float(np.sum(np.abs(symbols) ** 2)), symbols))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [symbols for _, symbols in candidates[:8]]


def soft_bits(symbols: np.ndarray, phase: float) -> np.ndarray:
    # One complex product per coded bit.  A constant residual CFO rotates all
    # products; phase hypotheses below estimate it without knowing the data.
    metric = np.real(symbols[1:] * np.conj(symbols[:-1]) * np.exp(-1j * phase))
    # The production K9 metric uses positive values as the bit-zero hypothesis.
    scale = np.median(np.abs(metric)) + 1e-12
    return metric / scale


def decode(received: np.ndarray, waveform: Waveform) -> tuple[bytes | None, dict]:
    last_meta: dict = {"failure": "no timing/CFO hypothesis decoded"}
    # +/-90 degrees covers residual offsets up to about +/-32 Hz over a bit;
    # wider rotations are ambiguous with DBPSK data polarity.
    phases = np.linspace(-np.pi / 2, np.pi / 2, 17)
    for symbols in symbol_candidates(received, waveform):
        for phase in phases:
            decoded, meta = CODEC.decode_soft(soft_bits(symbols, float(phase)))
            if decoded is not None:
                return decoded, meta
            last_meta = meta
    return None, last_meta


def screen(snr_db: float, trials: int) -> None:
    payload = bytes(range(CODEC.max_payload_bytes))
    waveform = modulate(payload)
    print(f"geometry payload={len(payload)}B coded_bits={CODED_BITS} "
          f"tx_bits={TX_BITS} chips={len(waveform.chips)} "
          f"seconds={len(waveform.audio)/FS:.3f} "
          f"main_lobe_hz~{CHIP_RATE + CHIP_RATE/4:.0f}")
    print("compact_ack: 112 coded bits * 12 chips / 1600 = 0.840 s body")
    for preset in ("quiet", "moderate", "disturbed"):
        score = 0
        failures: dict[str, int] = {}
        for trial in range(trials):
            channel = HfSsbScenario.from_preset(
                preset, sample_rate=FS,
                snr=SnrSpec(snr_db, SnrKind.WAVEFORM,
                            reference_start=0, reference_stop=len(waveform.audio)),
                seed=0xD500 + trial).build()
            received = channel.process(waveform.audio).audio
            decoded, meta = decode(received, waveform)
            if decoded == payload:
                score += 1
            else:
                why = str(meta.get("failure", "wrong payload"))
                failures[why] = failures.get(why, 0) + 1
        print(f"{preset:9s} dbpsk-rake  {score}/{trials} failures={failures}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snr", type=float, default=-24.0309)
    ap.add_argument("--trials", type=int, default=5)
    args = ap.parse_args()
    screen(args.snr, args.trials)


if __name__ == "__main__":
    main()
