"""hf19 -- an 8PSK, more-robust sibling of HF7 on HF7's own 49-carrier
OFDM geometry, screened in simulation against AWGN and light Watterson
fading.

This is a SIMULATION-ONLY harness. No radio hardware is touched anywhere in
this file. Its job is to find, on HF7's geometry, the constellation/coding/
guard/pilot combination that buys the largest SNR-floor improvement over HF7
for the smallest loss of net rate, and to bracket both modes' failure
boundaries on the same seeded channels.

What is reused (nothing is rewritten):
  - ``experiments/hf10_ofdm49_v6/ofdm49_v6.py`` -- the parametric OFDM PHY
    that HF6 and HF7 already wrap, used unmodified. HF7's configuration
    (``whale/modes/hf7_mode.py``) is the fixed baseline arm.
  - ``whale/channel.py`` -- ``WattersonChannel`` + ``WATTERSON_PRESETS`` +
    ``AwgnChannel`` + ``ChannelChain``.
  - ``whale/qualification.py`` -- ``trial_seed``, and the channel wiring
    convention (Watterson first, then AWGN with the ``seed ^ 0x5A5A`` noise
    seed) taken from ``channel_factory("watterson", ...)``.
  - ``whale/rx_audio.downsample`` -- the production 48 kHz -> 12 kHz receive
    decimation, so the receiver sees the sample stream it sees on hardware.
  - ``experiments/hf14_ofdm_bpsk_watterson/sweep.py`` -- this file's
    structure, Wilson interval, boundary rule and seeding scheme.

Axes
----
The carrier plan is HF7's and is not swept: ``fft_size=240`` (50 Hz spacing)
over every bin in 300-2700 Hz, 49 carriers. What is swept is everything that
trades rate for margin on that fixed plan:

  ``bits_per_symbol``  3 (8PSK) against HF7's 5 (32-QAM)
  ``cp_len``           guard time, 2.0 / 3.0 / 4.0 ms
  ``fec_rate``         LDPC 3/4 / 2/3 / 1/2
  ``pilot_interval``   OFDM symbols between full pilot symbols
  ``pilot_comb_stride`` every Nth carrier as a comb pilot present in every
                        symbol (0 = none), with ``comb_tracking`` selecting
                        how the receiver spends them

Metric
------
Primary is frame success rate versus waveform SNR per channel, and from it
the failure boundary: the lowest tested SNR at or above which every tested
SNR still met the success-rate target. Net bit rate is reported alongside but
never used for ranking -- the ranking question here is margin, and the rate
cost of each margin is read off the same table.

Usage (all from the repository root):

    # screen the constellation/coding/guard/pilot grid against HF7
    python experiments/hf19_ofdm49_8psk/sweep.py screen \
        --trials 12 --out experiments/hf19_ofdm49_8psk/screen.json

    # confirm named configurations at more trials and their real frame size
    python experiments/hf19_ofdm49_8psk/sweep.py confirm --trials 60 \
        --config 3:36:2/3:10 --config hf7 \
        --out experiments/hf19_ofdm49_8psk/confirm.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from whale import rx_audio
from whale.channel import (WATTERSON_PRESETS, AwgnChannel, ChannelChain,
                           SnrSpec, WattersonChannel)
from whale.qualification import trial_seed
from experiments.hf10_ofdm49_v6 import ofdm49_v6 as ofdm

AUDIO_RATE = 48_000
DESIGN_RATE = ofdm.DESIGN_RATE          # 12 kHz receive rate
Z_95 = 1.959963984540054

# HF7's carrier plan, held fixed for every arm: 50 Hz spacing, every bin in
# 300-2700 Hz. See whale/modes/hf7_mode.py.
FFT_SIZE = 240
BAND_LO_HZ = 300.0
BAND_HI_HZ = 2700.0

# Screening frame size, common to every arm so success rates are compared at
# equal delivered information and net bit rate stays an honest secondary
# number. Deliberately smaller than HF7's 4738-byte production frame: frame
# size is a throughput/overhead decision, not a margin decision, and a short
# common frame keeps the grid affordable. The confirmation pass re-measures
# the finalists at their real frame sizes.
DEFAULT_PACKET_BYTES = 1000

# AWGN plus the light fading classes SPEED_LADDERS.md names: quiet is
# 0.5 ms / 0.1 Hz and moderate is 1.0 ms / 0.5 Hz. Disturbed (2.0 ms / 1.0 Hz)
# is measured too, as the point where a mode on this geometry is expected to
# stop rather than as a target.
CHANNELS = ("awgn", "mid_latitude_quiet", "mid_latitude_moderate",
            "mid_latitude_disturbed")

SUCCESS_TARGET = 0.9

# HF7 as installed, for the baseline arm (whale/modes/hf7_mode.py).
HF7_BITS_PER_SYMBOL = 5
HF7_CP_LEN = 24
HF7_FEC_RATE = "3/4"
HF7_PILOT_INTERVAL = 20
HF7_PACKET_BYTES = 4738


def wilson(passed: int, total: int, z: float = Z_95) -> list[float]:
    """95% Wilson score interval, in the form the rest of the repository's
    benchmarks report."""
    if total == 0:
        return [0.0, 1.0]
    p = passed / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z / denominator * math.sqrt(
        p * (1 - p) / total + z * z / (4 * total * total))
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


@dataclass(frozen=True)
class Config:
    bits_per_symbol: int
    cp_len: int
    fec_rate: str
    pilot_interval: int
    packet_bytes: int = DEFAULT_PACKET_BYTES
    interleave: bool = True
    pilot_comb_stride: int = 0
    comb_tracking: str = "legacy"

    @property
    def key(self) -> str:
        comb = ("" if not self.pilot_comb_stride
                else f"_cs{self.pilot_comb_stride}{self.comb_tracking[:3]}")
        return (f"bps{self.bits_per_symbol}_cp{self.cp_len}"
                f"_fec{self.fec_rate.replace('/', '')}_pi{self.pilot_interval}"
                f"{comb}_pb{self.packet_bytes}")

    @property
    def mode_id(self) -> int:
        return zlib.crc32(self.key.encode()) & 0x7FFFFFFF

    def build(self) -> ofdm.OFDM49Mode:
        return ofdm.OFDM49Mode(
            fft_size=FFT_SIZE, cp_len=self.cp_len,
            active_bins=tuple(ofdm.bins_in_band(FFT_SIZE, BAND_LO_HZ, BAND_HI_HZ)),
            bits_per_symbol=self.bits_per_symbol,
            packet_bytes=self.packet_bytes,
            pilot_interval=self.pilot_interval,
            pilot_comb_stride=self.pilot_comb_stride,
            comb_tracking=self.comb_tracking,
            n_preamble_symbols=2, equalizer="gain",
            fec_rate=self.fec_rate, interleave=self.interleave)

    def geometry(self) -> dict:
        mode = self.build()
        payload_bits = mode.max_payload_bytes * 8
        frame_s = mode.frame_seconds()
        return {
            "key": self.key,
            "bits_per_symbol": self.bits_per_symbol,
            "cp_len": self.cp_len,
            "cp_ms": 1000.0 * self.cp_len / DESIGN_RATE,
            "fec_rate": self.fec_rate,
            "pilot_interval": self.pilot_interval,
            "pilot_comb_stride": self.pilot_comb_stride,
            "comb_tracking": self.comb_tracking,
            "interleave": self.interleave,
            "carrier_spacing_hz": DESIGN_RATE / FFT_SIZE,
            "n_carriers": mode.n_active,
            "n_data_carriers": mode.n_data_bins,
            "n_comb_pilots": mode.n_comb(),
            "packet_bytes": self.packet_bytes,
            "payload_bytes": mode.max_payload_bytes,
            "ofdm_symbol_rate_hz": DESIGN_RATE / mode.symbol_len,
            "ofdm_symbol_ms": 1000.0 * mode.symbol_len / DESIGN_RATE,
            "total_ofdm_symbols": mode.total_ofdm_symbols(),
            "frame_seconds": frame_s,
            "net_bps": payload_bits / frame_s,
            "crest_factor_db": mode.crest_factor_db(),
        }


def hf7_config(packet_bytes: int = DEFAULT_PACKET_BYTES) -> Config:
    return Config(HF7_BITS_PER_SYMBOL, HF7_CP_LEN, HF7_FEC_RATE,
                  HF7_PILOT_INTERVAL, packet_bytes)


def parse_config(spec: str, packet_bytes: int) -> Config:
    """``bps:cp:fec:pilot[:packet_bytes[:comb_stride:comb_tracking]]``, or the
    alias ``hf7``."""
    if spec.strip().lower() == "hf7":
        return hf7_config(packet_bytes)
    parts = spec.split(":")
    if len(parts) not in (4, 5, 7):
        raise ValueError(
            f"config {spec!r} is not "
            f"bps:cp:fec:pilot[:packet_bytes[:comb_stride:comb_tracking]]")
    pb = int(parts[4]) if len(parts) >= 5 else packet_bytes
    stride = int(parts[5]) if len(parts) == 7 else 0
    tracking = parts[6] if len(parts) == 7 else "legacy"
    return Config(int(parts[0]), int(parts[1]), parts[2], int(parts[3]), pb,
                  pilot_comb_stride=stride, comb_tracking=tracking)


def make_channel(channel: str, snr_db: float, seed: int):
    """Exactly ``whale.qualification.channel_factory``'s wiring, at the audio
    boundary: Watterson fading first, then full-Nyquist AWGN at a
    waveform-referenced SNR, with the same ``seed ^ 0x5A5A`` noise seed."""
    if channel == "awgn":
        return AwgnChannel(AUDIO_RATE, SnrSpec(snr_db), seed)
    if channel not in WATTERSON_PRESETS:
        raise ValueError(f"unknown channel {channel!r}")
    return ChannelChain((
        WattersonChannel.from_preset(AUDIO_RATE, channel, seed),
        AwgnChannel(AUDIO_RATE, SnrSpec(snr_db), seed ^ 0x5A5A),
    ))


_MODE_CACHE: dict[str, ofdm.OFDM49Mode] = {}


def _mode_for(config: Config) -> ofdm.OFDM49Mode:
    mode = _MODE_CACHE.get(config.key)
    if mode is None:
        mode = config.build()
        _MODE_CACHE[config.key] = mode
    return mode


def run_trial(config: Config, channel: str, snr_db: float, seed: int) -> bool:
    mode = _mode_for(config)
    rng = np.random.default_rng(seed)
    payload = rng.integers(0, 256, mode.max_payload_bytes, dtype=np.uint8).tobytes()
    tx = np.asarray(mode.modulate(payload), dtype=np.float32)
    # A short lead-in of silence so the correlator has somewhere to start and
    # the fading realization is not perfectly aligned with the frame.
    lead = np.zeros(int(0.05 * AUDIO_RATE), dtype=np.float32)
    ch = make_channel(channel, snr_db, seed)
    impaired = ch.process(np.concatenate((lead, tx, lead)))
    drained = ch.drain()
    capture = np.concatenate((
        np.asarray(impaired.audio, dtype=np.float32),
        np.asarray(drained.audio, dtype=np.float32),
        np.zeros(rx_audio.FILTER_DELAY_CAPTURE_SAMPLES, dtype=np.float32)))
    captured = rx_audio.downsample(capture)
    # The soft-decision path needs the "repeat" noise estimator for the same
    # reason HF7 sets it: the legacy estimator's residual is biased low and
    # mis-scales the LLRs handed to the LDPC decoder.
    result = mode.demodulate(captured, noise_estimator="repeat")
    return result.get("payload") == payload


def _point_worker(job):
    config, channel, snr_db, master_seed, point_index, trials = job
    t0 = time.time()
    ok = 0
    errors = 0
    for trial in range(1, trials + 1):
        seed = trial_seed(master_seed, config.mode_id, point_index, trial)
        try:
            if run_trial(config, channel, snr_db, seed):
                ok += 1
        except Exception as exc:  # never let one bad point kill the sweep
            errors += 1
            if errors == 1:
                print(f"    ERROR {config.key} {channel} {snr_db}: "
                      f"{type(exc).__name__}: {exc}", flush=True)
    lo, hi = wilson(ok, trials)
    return {"config": config.key, "channel": channel, "snr_db": snr_db,
            "trials": trials, "successes": ok, "errors": errors,
            "success_rate": ok / trials if trials else 0.0,
            "wilson_lo": lo, "wilson_hi": hi,
            "seconds": time.time() - t0}


def point_index_for(channel: str, snr_db: float) -> int:
    """Stable point index that does not depend on which SNRs were selected in
    a given run, so a screening point and a confirmation point at the same
    (channel, SNR) reuse the same seeded realizations."""
    return zlib.crc32(f"{channel}@{snr_db:g}".encode()) & 0xFFFF


def run_grid(configs, channels, snrs, trials, master_seed, workers, out_path):
    jobs = [(c, ch, s, master_seed, point_index_for(ch, s), trials)
            for c in configs for ch in channels for s in snrs]
    print(f"{len(jobs)} points x {trials} trials = {len(jobs) * trials} trials "
          f"on {workers} workers", flush=True)
    rows = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for i, row in enumerate(pool.map(_point_worker, jobs), 1):
            rows.append(row)
            print(f"  [{i}/{len(jobs)}] {row['config']:>30} {row['channel']:>22} "
                  f"{row['snr_db']:6.1f} dB  {row['successes']:3d}/{row['trials']:<3d} "
                  f"({row['success_rate']*100:5.1f}%) "
                  f"[{row['wilson_lo']:.2f},{row['wilson_hi']:.2f}] "
                  f"{row['seconds']:.1f}s", flush=True)
    elapsed = time.time() - t0
    doc = {
        "note": "SIMULATION ONLY -- no radio hardware involved.",
        "master_seed": master_seed, "trials_per_point": trials,
        "snr_grid_db": list(snrs), "channels": list(channels),
        "success_target": SUCCESS_TARGET,
        "geometry": {c.key: c.geometry() for c in configs},
        "points": rows,
        "elapsed_seconds": elapsed,
    }
    if out_path:
        Path(out_path).write_text(json.dumps(doc, indent=2) + "\n")
        print(f"wrote {out_path}", flush=True)
    summarize(doc)
    return doc


def boundary_snr(rows, target=SUCCESS_TARGET, use_wilson=False):
    """Lowest tested SNR at or above which EVERY tested SNR met the target.

    Returns None when even the highest tested SNR misses the target (i.e. the
    boundary was not bracketed by this grid -- reported as such, never
    extrapolated)."""
    ordered = sorted(rows, key=lambda r: r["snr_db"], reverse=True)
    best = None
    for row in ordered:
        value = row["wilson_lo"] if use_wilson else row["success_rate"]
        if value >= target:
            best = row["snr_db"]
        else:
            break
    return best


def _by_config(doc):
    grouped = {}
    for row in doc["points"]:
        grouped.setdefault(row["config"], {}).setdefault(row["channel"], []).append(row)
    return grouped


def summarize(doc):
    channels = doc["channels"]
    grouped = _by_config(doc)
    header = f"{'config':>30} {'net bps':>8} {'frame s':>8}"
    for ch in channels:
        header += f" {ch[:14]:>14}"
    for target in (doc["success_target"], 0.5):
        print(f"\n== boundary summary (lowest SNR with success rate >= "
              f"{target:.0%}, 'x' = not reached on this grid) ==")
        print(header)
        ranked = []
        for key, per_channel in grouped.items():
            geo = doc["geometry"][key]
            bounds = {ch: boundary_snr(per_channel.get(ch, []), target)
                      for ch in channels}
            fading = [bounds[ch] for ch in channels if ch != "awgn"]
            score = (sum(b for b in fading if b is not None)
                     + 1000 * sum(1 for b in fading if b is None))
            ranked.append((score, key, geo, bounds))
        for _, key, geo, bounds in sorted(ranked):
            line = f"{key:>30} {geo['net_bps']:8.1f} {geo['frame_seconds']:8.3f}"
            for ch in channels:
                b = bounds[ch]
                line += f" {('x' if b is None else f'{b:g}'):>14}"
            print(line)

    # Peak (best-case, highest-SNR) success rate per channel, so a
    # configuration that never reaches a boundary is still characterized.
    top = max(doc["snr_grid_db"])
    print(f"\n== success rate at the highest tested SNR ({top:g} dB) ==")
    print(header)
    for key in sorted(grouped):
        geo = doc["geometry"][key]
        line = f"{key:>30} {geo['net_bps']:8.1f} {geo['frame_seconds']:8.3f}"
        for ch in channels:
            hit = [r for r in grouped[key].get(ch, []) if r["snr_db"] == top]
            cell = f"{hit[0]['success_rate'] * 100:.0f}%" if hit else "-"
            line += f" {cell:>14}"
        print(line)


SCREEN_CP_LENS = (24, 36, 48)           # 2.0 / 3.0 / 4.0 ms guard
SCREEN_FEC_RATES = ("3/4", "2/3", "1/2")
SCREEN_PILOT_INTERVALS = (20, 10, 6)


def screen_configs(packet_bytes: int) -> list[Config]:
    """8PSK across the guard/coding/pilot grid, plus HF7's own configuration
    at the same frame size as the baseline arm."""
    configs = [Config(3, cp, fec, pi, packet_bytes)
               for cp in SCREEN_CP_LENS
               for fec in SCREEN_FEC_RATES
               for pi in SCREEN_PILOT_INTERVALS]
    return [hf7_config(packet_bytes)] + configs


# Second screening pass. The first pass found every (guard, code rate, block
# pilot interval) combination stuck well under the success target on the
# moderate fading class while all of them cleared AWGN at the same SNR, which
# says the binding constraint is channel tracking through a frequency-
# selective, time-varying channel rather than link margin. These arms spend
# carriers on comb pilots -- present in every OFDM symbol -- and try each
# receiver-side way of spending them.
COMB_BASES = ((24, "2/3", 10), (36, "2/3", 10))
COMB_ARMS = ((0, "legacy"), (7, "legacy"), (7, "residual"), (7, "confidence"),
             (4, "legacy"), (4, "residual"))


def comb_configs(packet_bytes: int) -> list[Config]:
    return [Config(3, cp, fec, pi, packet_bytes,
                   pilot_comb_stride=stride, comb_tracking=tracking)
            for cp, fec, pi in COMB_BASES
            for stride, tracking in COMB_ARMS]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("screen", "comb", "confirm", "geometry"))
    parser.add_argument("--trials", type=int, default=12)
    parser.add_argument("--master-seed", type=int, default=0x48463139)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--packet-bytes", type=int, default=DEFAULT_PACKET_BYTES)
    parser.add_argument("--snr", type=float, action="append", default=None,
                        help="repeatable; default grid depends on mode")
    parser.add_argument("--channel", action="append", default=None,
                        help="repeatable; default is every channel")
    parser.add_argument("--config", action="append", default=None,
                        help="bps:cp:fec:pilot[:packet_bytes], or 'hf7'")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    channels = tuple(args.channel) if args.channel else CHANNELS
    if args.mode == "screen":
        configs = screen_configs(args.packet_bytes)
        snrs = args.snr or [6.0, 9.0, 12.0, 15.0, 18.0, 21.0, 24.0]
    elif args.mode == "comb":
        configs = comb_configs(args.packet_bytes)
        snrs = args.snr or [14.0, 19.0, 24.0]
    else:
        if not args.config:
            parser.error(f"{args.mode} needs at least one --config")
        configs = [parse_config(spec, args.packet_bytes) for spec in args.config]
        snrs = args.snr or [6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0]

    if args.mode == "geometry":
        for c in configs:
            print(json.dumps(c.geometry(), indent=2))
        return 0

    run_grid(configs, channels, sorted(snrs), args.trials,
             args.master_seed, args.workers, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
