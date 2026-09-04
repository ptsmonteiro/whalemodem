#!/usr/bin/env python3
"""Deterministic arithmetic screen for the HR1 first-pass architecture.

This is intentionally not a modem or a channel simulation.  It makes the
rate, delay/Doppler ratios, frame classes, and SNR/Eb/N0 conversions in
DESIGN.md reproducible without importing experimental or production DSP.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass


NYQUIST_BANDWIDTH_HZ = 24_000.0
MAX_WATTERSON_DELAY_SECONDS = 0.007
MAX_WATTERSON_SPREAD_HZ = 30.0  # F.1487 2-sigma convention in whale.channel


@dataclass(frozen=True)
class Geometry:
    name: str
    signaling: str
    tones_or_carriers: int
    symbol_rate: float
    tone_or_carrier_spacing_hz: float
    coded_bits_per_symbol: float
    simultaneous_tones: int

    @property
    def symbol_seconds(self) -> float:
        return 1.0 / self.symbol_rate

    @property
    def raw_coded_bit_rate(self) -> float:
        return self.symbol_rate * self.coded_bits_per_symbol

    @property
    def delay_fraction_of_symbol(self) -> float:
        return MAX_WATTERSON_DELAY_SECONDS / self.symbol_seconds

    @property
    def spread_to_spacing(self) -> float:
        return MAX_WATTERSON_SPREAD_HZ / self.tone_or_carrier_spacing_hz


@dataclass(frozen=True)
class FrameClass:
    name: str
    max_physical_payload_bytes: int
    packet_overhead_bytes: int = 6  # uint16 length + CRC32, as PacketCodec
    termination_and_pad_input_bits: int = 8  # six K=7 tail + two state-0 pad

    @property
    def fec_input_bits(self) -> int:
        return ((self.max_physical_payload_bytes + self.packet_overhead_bytes) * 8
                + self.termination_and_pad_input_bits)

    @property
    def coded_bits(self) -> int:
        return self.fec_input_bits * 3  # frozen rate-1/3 convolutional code

    @property
    def data_symbols(self) -> int:
        return math.ceil(self.coded_bits / 4)  # 16-FSK

    @property
    def pilot_symbols(self) -> int:
        return (self.data_symbols - 1) // 31

    @property
    def body_symbols(self) -> int:
        return self.data_symbols + self.pilot_symbols

    @property
    def keyed_seconds(self) -> float:
        # Current common HF lead floor, 80 guarded sync symbols, body, HC0 tail.
        return 0.128 + 80 * 0.024 + self.body_symbols * 0.024 + 0.020


GEOMETRIES = (
    Geometry("HC0", "one-of-16 MFSK", 16, 93.75, 93.75, 4, 1),
    Geometry("hypothetical 32-parallel/23-Hz grid",
             "32 parallel BFSK carriers", 32,
             23.0, 23.0, 32, 32),
    Geometry("one-of-32 MFSK example", "one-of-32 MFSK", 32,
             46.875, 46.875, 5, 1),
    Geometry("HR1-A", "guarded one-of-16 MFSK", 16,
             1.0 / 0.024, 125.0, 4, 1),
)

FRAME_CLASSES = (
    FrameClass("tiny", 12),   # 10-byte air header plus DATA_ACK remainder
    FrameClass("short", 26),  # 10-byte air header plus 16-byte short DATA
    FrameClass("full", 64),   # 10-byte air header plus 54-byte DATA body
)


def ebn0_offset_db(useful_bit_rate: float) -> float:
    return 10.0 * math.log10(NYQUIST_BANDWIDTH_HZ / useful_bit_rate)


def report() -> dict:
    geometries = []
    for geometry in GEOMETRIES:
        row = asdict(geometry)
        row.update(
            symbol_seconds=geometry.symbol_seconds,
            raw_coded_bit_rate=geometry.raw_coded_bit_rate,
            delay_fraction_of_symbol=geometry.delay_fraction_of_symbol,
            spread_to_spacing=geometry.spread_to_spacing,
        )
        geometries.append(row)

    frames = []
    for frame in FRAME_CLASSES:
        row = asdict(frame)
        row.update(
            fec_input_bits=frame.fec_input_bits,
            coded_bits=frame.coded_bits,
            data_symbols=frame.data_symbols,
            pilot_symbols=frame.pilot_symbols,
            body_symbols=frame.body_symbols,
            keyed_seconds=frame.keyed_seconds,
        )
        frames.append(row)

    tiny, _, full = FRAME_CLASSES
    useful_data_bits = 54 * 8
    full_frame_rate = useful_data_bits / full.keyed_seconds
    clean_exchange_seconds = full.keyed_seconds + tiny.keyed_seconds + 2 * 0.3
    clean_session_rate = useful_data_bits / clean_exchange_seconds
    return {
        "schema": "whalemodem.hr1.architecture-screen.v1",
        "assumptions": {
            "nyquist_noise_bandwidth_hz": NYQUIST_BANDWIDTH_HZ,
            "max_watterson_delay_seconds": MAX_WATTERSON_DELAY_SECONDS,
            "max_watterson_frequency_spread_hz_2sigma": MAX_WATTERSON_SPREAD_HZ,
            "air_header_bytes": 10,
            "hf_turnaround_each_direction_seconds": 0.3,
            "hr1_tone_observation_seconds": 0.008,
            "hr1_guarded_tone_dwell_seconds": 0.024,
        },
        "geometries": geometries,
        "hr1_frame_classes": frames,
        "budgets": {
            "full_data_body_bytes": 54,
            "full_frame_useful_bit_rate": full_frame_rate,
            "clean_stop_and_wait_exchange_seconds": clean_exchange_seconds,
            "clean_long_session_useful_bit_rate": clean_session_rate,
            "full_frame_ebn0_offset_db": ebn0_offset_db(full_frame_rate),
            "clean_session_ebn0_offset_db": ebn0_offset_db(clean_session_rate),
            "waveform_minus24_db_full_frame_ebn0_db": (
                -24.0 + ebn0_offset_db(full_frame_rate)),
            "waveform_minus20_db_clean_session_ebn0_db": (
                -20.0 + ebn0_offset_db(clean_session_rate)),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    print(json.dumps(report(), indent=None if args.compact else 2, sort_keys=True))


if __name__ == "__main__":
    main()
