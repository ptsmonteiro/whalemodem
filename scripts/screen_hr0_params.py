"""Small deterministic HR0 MFSK geometry screen (not qualification evidence).

The screen deliberately uses a known payload boundary.  It answers whether a
waveform/coding geometry has enough payload margin before acquisition and CFO
search are spent on it.  The 8 kHz simulation SNR is adjusted to preserve the
repository's real-AWGN power density at the 48 kHz audio boundary.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whale import dsp
from whale.channel import AwgnChannel, ChannelChain, SnrSpec, WattersonChannel
from whale.dsp import mfsk


RATE = 8_000
REFERENCE_NYQUIST = 24_000.0
BODY_SECONDS = 7.0
FIRST_BIN = 5


def _coprime(size: int) -> int:
    for value in range(size // 2 + 1, size):
        if math.gcd(value, size) == 1:
            return value
    raise AssertionError("no interleaver multiplier")


def _rate13_encode(bits: np.ndarray) -> np.ndarray:
    """Terminated K=7, rate-1/3 (171, 133, 165) convolutional code."""
    state = 0
    out = []
    for bit in bits:
        register = ((state << 1) | int(bit)) & 0x7f
        out.extend(((register & 0o171).bit_count() & 1,
                    (register & 0o133).bit_count() & 1,
                    (register & 0o165).bit_count() & 1))
        state = register & 0x3f
    return np.asarray(out, np.uint8)


def _rate13_decode(soft: np.ndarray) -> np.ndarray:
    received = np.asarray(soft).reshape(-1, 3)
    metrics = np.full(64, np.inf)
    metrics[0] = 0.0
    previous = np.empty((len(received), 64), np.uint8)
    inputs = np.arange(64, dtype=np.uint8) & 1
    for at, symbol in enumerate(received):
        new = np.full(64, np.inf)
        for nxt in range(64):
            bit = nxt & 1
            for branch in (0, 1):
                old = (nxt >> 1) | (branch << 5)
                register = ((old << 1) | bit) & 0x7f
                coded = ((register & 0o171).bit_count() & 1,
                         (register & 0o133).bit_count() & 1,
                         (register & 0o165).bit_count() & 1)
                metric = metrics[old] + sum((2 * value - 1) * symbol[i]
                                            for i, value in enumerate(coded))
                if metric < new[nxt]:
                    new[nxt] = metric
                    previous[at, nxt] = old
        metrics = new
    decoded = np.empty(len(received), np.uint8)
    state = 0
    for at in range(len(received) - 1, -1, -1):
        decoded[at] = inputs[state]
        state = previous[at, state]
    return decoded


def run_candidate(tones: int, symbol_samples: int, diversity: int,
                  snr_48k: float, seeds: range, rate13: bool = False) -> dict:
    slots = tones * diversity
    bank = mfsk.ToneBank(RATE, symbol_samples, FIRST_BIN, slots)
    bits_per_symbol = int(math.log2(tones))
    payload_symbols = int(BODY_SECONDS * RATE // symbol_samples)
    divisor = 3 if rate13 else 2
    while (payload_symbols * bits_per_symbol) % divisor:
        payload_symbols -= 1
    coded_bits = payload_symbols * bits_per_symbol
    payload_symbols = coded_bits // bits_per_symbol
    if coded_bits % bits_per_symbol:
        return {"error": "coded-bit alignment"}
    interleaver = dsp.interleave.multiplicative(coded_bits,
                                                _coprime(coded_bits))
    if rate13:
        information_bits = coded_bits // 3
        information = np.random.default_rng(991).integers(
            0, 2, information_bits, dtype=np.uint8)
        information[-6:] = 0
        coded = interleaver.spread(_rate13_encode(information))
        max_payload = (information_bits - 6) // 8 - 6
    else:
        codec = dsp.PacketCodec(coded_bits, interleaver, 0x51A7, dsp.K9)
        payload = bytes((i * 73 + 19) & 255 for i in range(codec.max_payload_bytes))
        coded = codec.encode(payload)
        max_payload = codec.max_payload_bytes
    labels = coded.reshape(-1, bits_per_symbol)
    values = np.zeros(len(labels), dtype=np.int64)
    for column in range(bits_per_symbol):
        values = (values << 1) | labels[:, column]
    # Use the same Gray-labelled first bank as ToneBank.  Extra banks repeat
    # the tone in separated frequency slots and their received energies add.
    tone = bank._ungray[:tones][values]
    phase = 2 * np.pi * np.arange(symbol_samples) / symbol_samples
    audio = np.zeros((len(tone), symbol_samples), dtype=np.float64)
    for branch in range(diversity):
        bins = bank.bins[tone + branch * tones]
        audio += np.cos(bins[:, None] * phase[None, :]) / np.sqrt(diversity)
    audio = (0.13 * np.sqrt(2.0) * audio).reshape(-1).astype(np.float32)

    # A 48 kHz full-band figure has six times as much integrated noise as an
    # 8 kHz real-audio simulation at the same noise spectral density.
    snr_8k = snr_48k + 10 * np.log10(REFERENCE_NYQUIST / (RATE / 2))
    presets = ("mid_latitude_quiet", "mid_latitude_moderate",
               "mid_latitude_disturbed")
    success = {preset: 0 for preset in presets}
    for preset_index, preset in enumerate(presets):
        for seed in seeds:
            channel = ChannelChain((
                WattersonChannel.from_preset(RATE, preset,
                    seed + 1000 * preset_index, oscillators=64),
                AwgnChannel(RATE, SnrSpec(snr_8k), seed ^ 0x5A5A),
            ))
            received = channel.process(audio).audio[:len(audio)]
            spectrum = np.fft.fft(received.reshape(payload_symbols,
                                                   symbol_samples), axis=1)
            energies = np.zeros((payload_symbols, tones))
            for branch in range(diversity):
                bins = bank.bins[:tones] + branch * tones
                energies += np.abs(spectrum[:, bins]) ** 2
            # soft_bits only needs the labels and tone count.  The first
            # ``tones`` entries form exactly that logical Gray-labelled bank.
            logical = mfsk.ToneBank(RATE, symbol_samples, FIRST_BIN, tones)
            soft = interleaver.gather(mfsk.soft_bits(logical, energies))
            if rate13:
                decoded = _rate13_decode(soft)
                success[preset] += np.array_equal(decoded, information)
            else:
                decoded, _ = codec._unpack(codec.code.decode_soft(soft))
                success[preset] += decoded == payload
    return {
        "M": tones, "diversity": diversity, "code": "K7-R1/3" if rate13 else "K9-R1/2",
        "symbol_ms": 1000 * symbol_samples / RATE,
        "bandwidth_hz": bank.bandwidth_hz,
        "payload_symbols": payload_symbols,
        "airtime_s": len(audio) / RATE,
        "max_payload_B": max_payload,
        "application_payload_B": max_payload - 10,
        "nominal_application_bit_s": max(0, max_payload - 10) * 8
                                      / (len(audio) / RATE),
        "success": success,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snr", type=float, default=-24.0309,
                        help="48 kHz repository waveform SNR")
    parser.add_argument("--seeds", type=int, default=4)
    args = parser.parse_args(argv)
    print("known-boundary K9 payload screen; successes per Watterson class")
    for tones, diversity in ((32, 1), (64, 1), (128, 1), (256, 1),
                             (16, 2), (32, 2), (64, 2), (128, 2)):
        slots = tones * diversity
        samples = math.ceil(RATE * slots / 2300)
        # Exact FFT geometry; rounding upward guarantees the bandwidth cap.
        row = run_candidate(tones, samples, diversity, args.snr,
                            range(1, args.seeds + 1))
        print(row)
    for tones in (64, 128, 256):
        samples = math.ceil(RATE * tones / 2300)
        print(run_candidate(tones, samples, 1, args.snr,
                            range(1, args.seeds + 1), rate13=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
