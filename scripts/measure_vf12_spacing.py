"""vf12 carrier-spacing sweep on the FM bench: EVM and raw (pre-LDPC) BER.

Everything about vf12 is kept -- 500-3000 Hz passband, 16-QAM, 36-sample CP
(3 ms), 0.5 s lead-in, comb pilot every 8th carrier with a 3-symbol tracking
window, 5 s frame budget, peak-normalised drive -- except the carrier
spacing. The FFT size at 12 kHz is round(12000 / spacing), so spacings that
don't divide 12000 land on the nearest realisable value (70 Hz -> 70.18 Hz);
the actual spacing is recorded. Carriers are the FFT bins inside the band.

Each trial sends one full LDPC-coded frame from a random payload and runs
vf12's own receiver front end (sync, timing search, header channel fit, pilot
tracking). Reported per trial:
  evm      -- RMS EVM of the pilot-tracked data carriers against the sent
              16-QAM points, relative to the constellation's mean power
  evm_hdr  -- the same with the header-only static channel (no pilot tracking)
  raw_ber  -- hard-decision bit errors on the coded stream, before LDPC
  crc_ok   -- whether vf12's full decoder recovered the payload

Run from the repository root:
    python scripts/measure_vf12_spacing.py --offline
    python scripts/measure_vf12_spacing.py --spacings 10:300:10 --trials 1
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path

import numpy as np
from scipy.signal import correlate, hilbert

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from whale import framing, rx_audio  # noqa: E402
from whale.dsp import bits, ldpc  # noqa: E402
from whale.modes.vf12 import Vf12Mode, _fit_channel  # noqa: E402

RX_RATE = 12000
ACQUIRE_WINDOW_S = 2.5


@dataclass(frozen=True)
class SpacedVf12(Vf12Mode):
    spacing_request_hz: float = 50.0

    def __post_init__(self):
        object.__setattr__(self, "fft_size", int(round(RX_RATE / self.spacing_request_hz)))
        object.__setattr__(self, "carrier_spacing_hz", RX_RATE / self.fft_size)
        if self.n_codewords is None:
            available = self.target_airtime_seconds - self.lead_in_seconds - 0.1
            symbols = int(np.floor(available * RX_RATE / self.symbol_samples)) - 8
            object.__setattr__(self, "n_codewords", symbols * self.bits_per_symbol // ldpc.N)
        if self.n_codewords < 1:
            raise ValueError("frame budget cannot hold one codeword")

    @cached_property
    def active_bins(self):
        d = self.carrier_spacing_hz
        return tuple(range(math.ceil(self.band_lo_hz / d - 1e-9),
                           math.floor(self.band_hi_hz / d + 1e-9) + 1))

    def coded_stream(self, payload):
        """The whitened, interleaved coded bits exactly as encode() maps them."""
        info = np.unpackbits(np.frombuffer(self._pack(bytes(payload)), np.uint8))
        k = ldpc.INFORMATION_BITS[self.fec_rate]
        info = np.pad(info, (0, self.n_codewords * k - len(info)))
        coded = np.concatenate([ldpc.encode(row, self.fec_rate)
                                for row in info.reshape(self.n_codewords, k)])
        stream = self.interleaver.spread(coded) ^ self.whitener
        # vf12 pads the last symbol with zero bits, which maps every padded
        # carrier to one 16-QAM point and adds them coherently: at 10 Hz
        # spacing that spike sets the frame's peak and cuts the drive by
        # ~11 dB. Pseudorandom fill keeps every spacing level-matched.
        fill = self.payload_symbols * self.bits_per_symbol - len(stream)
        return np.concatenate((stream, bits.pn_bits(fill, 0x3AD5) if fill else stream[:0]))

    def encode(self, payload):
        labels = self.coded_stream(payload).reshape(-1, self.bits_per_carrier)
        data_syms = self.points[labels @ (1 << np.arange(self.bits_per_carrier - 1, -1, -1))]
        values = np.empty((self.payload_symbols, self.n_carriers), dtype=complex)
        values[:, self.data_positions] = data_syms.reshape(self.payload_symbols, -1)
        values[:, self.pilot_positions] = self.pilot_values
        symbols = self._build(np.vstack((self.header, values)), factor=4)
        lead = np.resize(self._build(self._known(0x1EAD, 1), factor=4, guard=False),
                         round(self.lead_in_seconds * 48000)).copy()
        fade = min(240, len(lead))
        lead[:fade] *= np.linspace(0, 1, fade)
        audio = np.concatenate((lead, symbols, np.zeros(4800)))
        return (audio / np.max(np.abs(audio))).astype(np.float32)

    def analyse(self, audio, payload):
        audio = np.asarray(audio, dtype=float)
        reference = self._build(self.header[:4])
        if len(audio) < 2 * len(reference):
            return {"acquired": False, "reason": "capture too short"}
        corr = np.abs(correlate(hilbert(audio), hilbert(reference), mode="valid"))
        # The frame starts ~0.5 s into the capture. Short syncs at wide spacing
        # otherwise lock onto the receiver's squelch tail at unkey.
        coarse = int(np.argmax(corr[:int(ACQUIRE_WINDOW_S * RX_RATE)]))
        best = None
        for offset in range(-56, 9):
            start = coarse + offset
            observed = self._extract(audio, start + 4 * self.symbol_samples, 4)
            if observed is not None:
                _, evm = _fit_channel(observed, self.header[4:])
                if best is None or evm < best[1]:
                    best = (start, evm)
        if best is None:
            return {"acquired": False, "reason": "burst runs off the capture"}
        start, header_evm = best
        block = self._extract(audio, start + 4 * self.symbol_samples, 4 + self.payload_symbols)
        if block is None:
            return {"acquired": False, "reason": "burst runs off the capture"}
        channel, _ = _fit_channel(block[:4], self.header[4:])
        safe = np.where(np.abs(channel) > 1e-12, channel, 1e-12)
        rx = block[4:]
        pilot_idx, data_idx, pv = self.pilot_positions, self.data_positions, self.pilot_values
        carriers = np.arange(self.n_carriers)
        resid = rx[:, pilot_idx] / (pv * safe[pilot_idx])
        mag = np.abs(resid)
        phase = np.unwrap(np.angle(resid), axis=1)
        R = (np.array([np.interp(carriers, pilot_idx, row) for row in mag])
             * np.exp(1j * np.array([np.interp(carriers, pilot_idx, row) for row in phase])))
        if self.pilot_time_span > 1:
            kernel = np.ones(self.pilot_time_span)
            taps = np.convolve(np.ones(R.shape[0]), kernel, mode="same")[:, None]
            R = np.apply_along_axis(lambda col: np.convolve(col, kernel, mode="same"), 0, R) / taps
        tracked = rx[:, data_idx] / (safe[data_idx] * R[:, data_idx])
        static = rx[:, data_idx] / safe[data_idx]

        stream = self.coded_stream(payload)
        labels = stream.reshape(-1, self.bits_per_carrier) @ (1 << np.arange(self.bits_per_carrier - 1, -1, -1))
        sent = self.points[labels].reshape(self.payload_symbols, len(data_idx))
        power = float(np.mean(np.abs(self.points) ** 2))

        def evm(eq):
            return np.sqrt(np.mean(np.abs(eq - sent) ** 2, axis=0) / power)

        evm_carrier = evm(tracked)
        evm_total = float(np.sqrt(np.mean(evm_carrier ** 2)))
        evm_static = float(np.sqrt(np.mean(evm(static) ** 2)))
        hard = np.argmin(np.abs(tracked.reshape(-1)[:, None] - self.points[None, :]), axis=1)
        diff = hard ^ labels
        bit_errors = np.array([np.count_nonzero((diff >> b) & 1) for b in range(self.bits_per_carrier)])
        n_bits = diff.size * self.bits_per_carrier
        errs_per_carrier = np.zeros(len(data_idx), dtype=int)
        for b in range(self.bits_per_carrier):
            errs_per_carrier += ((diff >> b) & 1).reshape(self.payload_symbols, -1).sum(axis=0)
        freqs = np.array(self.active_bins)[data_idx] * self.carrier_spacing_hz
        return {
            "acquired": True,
            "header_evm": float(header_evm),
            "evm": evm_total,
            "evm_db": float(20 * np.log10(evm_total)),
            "evm_hdr": evm_static,
            "evm_hdr_db": float(20 * np.log10(evm_static)),
            "raw_ber": float(bit_errors.sum() / n_bits),
            "bit_errors": int(bit_errors.sum()),
            "bits": int(n_bits),
            "carrier_freq_hz": freqs.tolist(),
            "carrier_evm_db": (20 * np.log10(evm_carrier)).tolist(),
            "carrier_bit_errors": errs_per_carrier.tolist(),
        }

    def describe_geometry(self):
        return {"spacing_requested_hz": self.spacing_request_hz,
                "spacing_hz": self.carrier_spacing_hz, "fft_size": self.fft_size,
                "carriers": self.n_carriers, "data_carriers": len(self.data_positions),
                "pilots": len(self.pilot_positions),
                "band_hz": [self.active_bins[0] * self.carrier_spacing_hz,
                            self.active_bins[-1] * self.carrier_spacing_hz],
                "symbol_ms": 1000 * self.symbol_samples / RX_RATE,
                "payload_symbols": self.payload_symbols, "n_codewords": self.n_codewords,
                "airtime_s": self.airtime(0), "net_bps": self.bits_per_second}


def parse_spacings(text):
    if ":" in text:
        lo, hi, step = (float(x) for x in text.split(":"))
        return [lo + i * step for i in range(int(round((hi - lo) / step)) + 1)]
    return [float(x) for x in text.split(",") if x.strip()]


class _Offline:
    def __init__(self, snr_db=None):
        self.peer = None
        self.audio = np.zeros(0, np.float32)
        self.snr_db = snr_db

    def snapshot_rx(self):
        return self.audio.copy()

    def consume_rx(self, n):
        self.audio = self.audio[int(n):]

    def send(self, audio):
        rx = rx_audio.downsample(np.concatenate((np.zeros(4800, np.float32), audio)))
        if self.snr_db is not None:
            p = np.mean(rx[rx != 0] ** 2)
            rx = rx + np.random.default_rng().normal(0, np.sqrt(2 * p / 10 ** (self.snr_db / 10)), len(rx))
        self.peer.audio = np.concatenate((rx, np.zeros(2400))).astype(np.float32)
        return len(audio) / 48000


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spacings", default="10:300:10", help="lo:hi:step or comma list (Hz)")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--control", type=float, default=50.0,
                    help="repeat this spacing at the end as a drift control; 0 disables")
    ap.add_argument("--direction", choices=("both", "ic705->ht", "ht->ic705"), default="both")
    ap.add_argument("--capture-tail", type=float, default=1.5)
    ap.add_argument("--inter-trial", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=20260915)
    ap.add_argument("--offline", action="store_true", help="loopback through rx_audio; no radios")
    ap.add_argument("--offline-snr", type=float, default=None)
    ap.add_argument("--out-dir", type=Path, default=ROOT / "logs" / "vf12_spacing")
    args = ap.parse_args(argv)

    spacings = parse_spacings(args.spacings)
    if args.control:
        spacings.append(args.control)
    modes = [SpacedVf12(spacing_request_hz=s) for s in spacings]
    for m in modes:
        g = m.describe_geometry()
        print(f"{g['spacing_requested_hz']:5.0f} Hz -> {g['spacing_hz']:7.2f} Hz  N={g['fft_size']:4d} "
              f"{g['carriers']:3d} carriers ({g['data_carriers']} data)  symbol {g['symbol_ms']:.1f} ms  "
              f"{g['payload_symbols']} syms  {g['net_bps']:.0f} net bit/s")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out_dir / (stamp + ("-offline" if args.offline else ""))
    out.mkdir(parents=True, exist_ok=True)
    run = {"utc": stamp, "offline": args.offline, "trials": []}

    def pair():
        if args.offline:
            a, b = _Offline(args.offline_snr), _Offline(args.offline_snr)
            a.peer, b.peer = b, a
            from contextlib import nullcontext
            return nullcontext((a, b))
        import bench
        return bench.radio_pair("ic705", "ht", warmup=3.0)

    with pair() as (a, b):
        legs = []
        if args.direction in ("both", "ic705->ht"):
            legs.append((a, b, "ic705->ht"))
        if args.direction in ("both", "ht->ic705"):
            legs.append((b, a, "ht->ic705"))
        for i, mode in enumerate(modes):
            for trial in range(args.trials):
                for tx, rx, direction in legs:
                    rng = np.random.default_rng([args.seed, i, trial, int(direction.startswith("ht"))])
                    payload = rng.integers(0, 256, framing.AIR_HEADER_BYTES + mode.chunk_size,
                                           dtype=np.uint8).tobytes()
                    audio = mode.encode(payload)
                    rx.consume_rx(len(rx.snapshot_rx()))
                    keyed = tx.send(audio)
                    time.sleep(0 if args.offline else args.capture_tail)
                    captured = np.asarray(rx.snapshot_rx(), dtype=np.float32)
                    result = mode.analyse(captured, payload)
                    decoded = mode.decode(captured)
                    result["crc_ok"] = bool(decoded.get("payload") == payload)
                    result["codewords_ok"] = decoded.get("codewords_ok")
                    g = mode.describe_geometry()
                    label = f"{g['spacing_hz']:6.1f} Hz {direction}"
                    if result["acquired"]:
                        print(f"  {label}: EVM {result['evm_db']:6.1f} dB ({100 * result['evm']:5.1f}%)  "
                              f"hdr-only {result['evm_hdr_db']:6.1f} dB  raw BER {result['raw_ber']:.2e} "
                              f"({result['bit_errors']}/{result['bits']})  "
                              f"LDPC {result['codewords_ok']}/{mode.n_codewords} "
                              f"{'PASS' if result['crc_ok'] else 'FAIL'}")
                    else:
                        print(f"  {label}: NOT ACQUIRED ({result['reason']})")
                    name = f"{i:02d}_{g['spacing_requested_hz']:.0f}Hz_{direction.replace('->', '_')}_{trial}.npy"
                    np.save(out / name, captured)
                    run["trials"].append({"index": i, "control": args.control and i == len(modes) - 1,
                                          "direction": direction, "trial": trial, "geometry": g,
                                          "keyed_s": keyed, "capture": name, "result": result})
                    (out / "result.json").write_text(json.dumps(run, indent=1) + "\n")
                    if not args.offline:
                        time.sleep(args.inter_trial)
    print(f"wrote {out / 'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
