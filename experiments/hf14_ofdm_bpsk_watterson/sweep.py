"""hf14 -- simulation sweep for the most ROBUST BPSK OFDM geometry in the
HF SSB 300-2700 Hz passband, judged on Watterson fading channels.

This is a SIMULATION-ONLY harness. No radio hardware is touched anywhere in
this file; its purpose is to narrow a large geometry grid down to a handful
of configurations worth spending real airtime on later.

What is reused (nothing is rewritten):
  - ``experiments/hf10_ofdm49_v6/ofdm49_v6.py`` -- the parametric OFDM PHY
    (fft_size / cp_len / bits_per_symbol / pilot_interval /
    pilot_comb_stride / packet_bytes, and ``bins_in_band()`` for the
    300-2700 Hz passband). Used unmodified, always with
    ``bits_per_symbol=1`` (BPSK) and ``fec_rate=None``.
  - ``whale/channel.py`` -- ``WattersonChannel`` + ``WATTERSON_PRESETS`` +
    ``AwgnChannel`` + ``ChannelChain``.
  - ``whale/qualification.py`` -- ``trial_seed`` for stable, independent,
    reproducible per-trial seeds, and the channel wiring convention
    (Watterson first, then AWGN with the ``seed ^ 0x5A5A`` noise seed) copied
    from ``channel_factory("watterson", ...)``.
  - ``whale/rx_audio.downsample`` -- the production 48 kHz -> 12 kHz receive
    decimation, so the receiver sees exactly the sample stream it sees on
    hardware.

Geometry axes
-------------
``fft_size`` sets both the carrier spacing (12000/fft_size Hz) and the OFDM
symbol rate; the active bin set is always *every* bin that falls inside
300-2700 Hz, so each geometry fills the passband. ``cp_len`` is swept as a
secondary axis under an explicit rule (see ``cp_candidates``), and
``pilot_interval`` / ``pilot_comb_stride`` as a third.

Metric
------
Primary is the frame success rate versus waveform SNR per channel, and from
it the *failure boundary*: the lowest tested SNR at or above which every
tested SNR still met the success-rate target. Secondary is net bit rate
(payload bits / frame airtime), reported but never used for ranking.

Usage (all from the repository root):

    # coarse screening pass over the whole spacing grid
    python experiments/hf14_ofdm_bpsk_watterson/sweep.py screen \
        --trials 8 --out experiments/hf14_ofdm_bpsk_watterson/screen.json

    # secondary CP / pilot axes on the survivors
    python experiments/hf14_ofdm_bpsk_watterson/sweep.py cp --trials 8 --out ...
    python experiments/hf14_ofdm_bpsk_watterson/sweep.py pilot --trials 8 --out ...

    # confirmation pass, larger trial count, explicit configs
    python experiments/hf14_ofdm_bpsk_watterson/sweep.py confirm \
        --trials 40 --config 400:50:8:0 --config 240:36:8:0 --out ...
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

# Fixed frame payload for every geometry, so success rates are compared at
# equal delivered information and net bit rate stays an honest secondary
# number rather than a hidden axis. 70 packet bytes = 64 B payload
# (LENGTH_BYTES=2 + payload + CRC_BYTES=4).
DEFAULT_PACKET_BYTES = 70

# The four channels every configuration is measured on.
CHANNELS = ("awgn", "mid_latitude_moderate", "mid_latitude_disturbed",
            "high_latitude_moderate")

# Screening spacing grid: ~15 Hz (many narrow carriers) to 100 Hz (few wide
# carriers), always filling 300-2700 Hz.
SCREEN_FFT_SIZES = (800, 600, 480, 400, 300, 240, 200, 160, 120)

# Cyclic-prefix rule (see RESULTS.md "CP rule"): the CP must span the worst
# Watterson differential delay under test (7 ms exists in the preset table;
# 3 ms is the worst among the three presets swept here) plus the receive
# decimation filter's own smear, and must also be a usable fraction of the
# symbol. cp_len = max(CP_FLOOR_SAMPLES, round(frac * fft_size)).
CP_FLOOR_SAMPLES = 36           # 3.0 ms at 12 kHz == high_latitude_moderate
CP_DEFAULT_FRACTION = 0.125
CP_FRACTIONS = (0.0625, 0.125, 0.25)

SUCCESS_TARGET = 0.9


def cp_len_for(fft_size: int, fraction: float = CP_DEFAULT_FRACTION) -> int:
    return int(max(CP_FLOOR_SAMPLES, round(fraction * fft_size)))


def cp_candidates(fft_size: int) -> list[int]:
    return sorted({cp_len_for(fft_size, f) for f in CP_FRACTIONS})


def wilson(passed: int, total: int, z: float = Z_95) -> list[float]:
    """95% Wilson score interval, identical in form to the one
    ``experiments/hc2_32qam/benchmark_hc2_snr.py`` reports."""
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
    fft_size: int
    cp_len: int
    pilot_interval: int
    pilot_comb_stride: int
    packet_bytes: int = DEFAULT_PACKET_BYTES

    @property
    def key(self) -> str:
        return (f"fft{self.fft_size}_cp{self.cp_len}_pi{self.pilot_interval}"
                f"_cs{self.pilot_comb_stride}_pb{self.packet_bytes}")

    @property
    def mode_id(self) -> int:
        return zlib.crc32(self.key.encode()) & 0x7FFFFFFF

    def build(self) -> ofdm.OFDM49Mode:
        return ofdm.OFDM49Mode(
            fft_size=self.fft_size, cp_len=self.cp_len,
            active_bins=tuple(ofdm.bins_in_band(self.fft_size)),
            bits_per_symbol=1, packet_bytes=self.packet_bytes,
            pilot_interval=self.pilot_interval,
            pilot_comb_stride=self.pilot_comb_stride,
            n_preamble_symbols=2, equalizer="gain", fec_rate=None)

    def geometry(self) -> dict:
        mode = self.build()
        spacing = DESIGN_RATE / self.fft_size
        payload_bits = mode.max_payload_bytes * 8
        frame_s = mode.frame_seconds()
        return {
            "key": self.key,
            "fft_size": self.fft_size, "cp_len": self.cp_len,
            "cp_ms": 1000.0 * self.cp_len / DESIGN_RATE,
            "carrier_spacing_hz": spacing,
            "n_carriers": mode.n_active,
            "n_data_carriers": mode.n_data_bins,
            "pilot_interval": self.pilot_interval,
            "pilot_comb_stride": self.pilot_comb_stride,
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
    result = mode.demodulate(captured)
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
            print(f"  [{i}/{len(jobs)}] {row['config']:>34} {row['channel']:>24} "
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


def summarize(doc):
    rows = doc["points"]
    channels = doc["channels"]
    by_config = {}
    for row in rows:
        by_config.setdefault(row["config"], {}).setdefault(row["channel"], []).append(row)
    for target in (doc["success_target"], 0.5):
        print(f"\n== boundary summary (lowest SNR with success rate >= "
              f"{target:.0%}, 'x' = not reached on this grid) ==")
        header = f"{'config':>34} {'net bps':>8} {'frame s':>8}"
        for ch in channels:
            header += f" {ch[:14]:>14}"
        print(header)
        ranked = []
        for key, per_channel in by_config.items():
            geo = doc["geometry"][key]
            bounds = {ch: boundary_snr(per_channel.get(ch, []), target)
                      for ch in channels}
            fading = [bounds[ch] for ch in channels if ch != "awgn"]
            score = (sum(b for b in fading if b is not None)
                     + 1000 * sum(1 for b in fading if b is None))
            ranked.append((score, key, geo, bounds))
        for _, key, geo, bounds in sorted(ranked):
            line = f"{key:>34} {geo['net_bps']:8.1f} {geo['frame_seconds']:8.3f}"
            for ch in channels:
                b = bounds[ch]
                line += f" {('x' if b is None else f'{b:g}'):>14}"
            print(line)
        # peak (best-case, highest-SNR) success rate per channel, so a
        # configuration that never reaches the target is still characterized
        if target == 0.5:
            print(f"\n== peak success rate at the highest tested SNR "
                  f"({max(doc['snr_grid_db']):g} dB) ==")
            print(header)
            for _, key, geo, _ in sorted(ranked):
                line = f"{key:>34} {geo['net_bps']:8.1f} {geo['frame_seconds']:8.3f}"
                for ch in channels:
                    rows_ch = by_config[key].get(ch, [])
                    top = max(rows_ch, key=lambda r: r["snr_db"]) if rows_ch else None
                    text = "n/a" if top is None else f"{top['success_rate'] * 100:.0f}%"
                    line += f" {text:>14}"
                print(line)


def parse_config(text: str) -> Config:
    parts = text.split(":")
    if len(parts) not in (4, 5):
        raise argparse.ArgumentTypeError(
            "config must be fft:cp:pilot_interval:comb_stride[:packet_bytes]")
    fft, cp, pi, cs = (int(p) for p in parts[:4])
    pb = int(parts[4]) if len(parts) == 5 else DEFAULT_PACKET_BYTES
    return Config(fft, cp, pi, cs, pb)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pass_name", choices=("screen", "cp", "pilot", "confirm", "geometry"))
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--snrs", type=float, nargs="+",
                    default=[24, 20, 16, 12, 8, 4, 0])
    ap.add_argument("--channels", nargs="+", default=list(CHANNELS))
    ap.add_argument("--config", action="append", type=parse_config, default=None,
                    help="fft:cp:pilot_interval:comb_stride[:packet_bytes]; "
                         "repeatable (used by the 'cp'/'pilot'/'confirm' passes)")
    ap.add_argument("--pilot-interval", type=int, default=8,
                    help="pilot interval used by the 'screen' and 'cp' passes")
    ap.add_argument("--packet-bytes", type=int, default=DEFAULT_PACKET_BYTES)
    ap.add_argument("--master-seed", type=int, default=20260903)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if args.pass_name == "screen":
        configs = [Config(f, cp_len_for(f), args.pilot_interval, 0, args.packet_bytes)
                   for f in SCREEN_FFT_SIZES]
    elif args.pass_name == "cp":
        if not args.config:
            raise SystemExit("--config is required for the 'cp' pass")
        configs = []
        for base in args.config:
            for cp in cp_candidates(base.fft_size):
                configs.append(Config(base.fft_size, cp, base.pilot_interval,
                                      base.pilot_comb_stride, base.packet_bytes))
    elif args.pass_name == "pilot":
        if not args.config:
            raise SystemExit("--config is required for the 'pilot' pass")
        configs = []
        for base in args.config:
            for pi, cs in ((0, 0), (4, 0), (8, 0), (16, 0), (8, 6), (8, 3), (0, 3)):
                configs.append(Config(base.fft_size, base.cp_len, pi, cs,
                                      base.packet_bytes))
    else:  # confirm / geometry
        if not args.config:
            raise SystemExit(f"--config is required for the '{args.pass_name}' pass")
        configs = list(args.config)

    # de-duplicate while preserving order
    seen, unique = set(), []
    for c in configs:
        if c.key not in seen:
            seen.add(c.key)
            unique.append(c)
    configs = unique

    print("configurations:")
    for c in configs:
        g = c.geometry()
        print(f"  {g['key']:>34}: {g['n_carriers']:3d} carriers @ "
              f"{g['carrier_spacing_hz']:5.1f} Hz, symbol {g['ofdm_symbol_ms']:6.2f} ms "
              f"({g['ofdm_symbol_rate_hz']:6.2f} Bd), cp {g['cp_ms']:4.1f} ms, "
              f"{g['total_ofdm_symbols']:3d} symbols, frame {g['frame_seconds']:6.3f} s, "
              f"payload {g['payload_bytes']} B, net {g['net_bps']:7.1f} bps, "
              f"crest {g['crest_factor_db']:.1f} dB")
    if args.pass_name == "geometry":
        return 0

    run_grid(configs, args.channels, args.snrs, args.trials,
             args.master_seed, args.workers, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
