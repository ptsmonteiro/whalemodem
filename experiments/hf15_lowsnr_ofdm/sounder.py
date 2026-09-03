"""HF15 path sounder: measures the IC-705 -> IC-7300 HF path per subcarrier.

This is deliberately not a modem. It transmits a *periodic* OFDM sounding
burst -- the same N-sample symbol repeated M times, so the signal is exactly
periodic and every aligned N-sample window is a valid, cyclic-prefix-free
symbol -- and estimates, per subcarrier across 300-2700 Hz:

  * the complex channel response H[k] (mean over repeats), and from it the
    amplitude response in dB;
  * the per-carrier effective noise+distortion power N[k] (variance over
    repeats), which folds in receiver noise, phase noise, and any
    within-burst channel variation -- i.e. exactly what a coherent demapper
    actually fights;
  * SNR[k] = |H[k]|^2 / N[k], the number every subsequent design decision
    (bits/carrier, FEC rate, diversity order) is derived from.

It also measures the carrier frequency offset between the two radios, which
sets a hard limit on how long a coherent OFDM symbol may be.

SAFETY: transmit side is selectable but the IC-705 -> IC-7300 direction
requires the explicit --allow-ic705-tx acknowledgement, matching
experiments/hf10_ofdm49_v6/hardware_test.py. The receiving station is always
opened structurally receive-only (no PTT backend is even constructed).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np

from experiments.hf5_8psk_4k import sc as _sc
from whale.transport import RadioTransport

DESIGN_RATE = 12000.0
BAND_LO_HZ = 300.0
BAND_HI_HZ = 2700.0


def bins_in_band(fft_size, lo=BAND_LO_HZ, hi=BAND_HI_HZ):
    spacing = DESIGN_RATE / fft_size
    return np.arange(int(np.ceil(lo / spacing)), int(np.floor(hi / spacing)) + 1)


def _newman_phases(n):
    k = np.arange(n)
    return np.pi * k * k / n


def sounding_symbol(fft_size):
    """One periodic sounding symbol at DESIGN_RATE.

    Returns (time_symbol, bins, bin_symbols). Newman phases keep the crest
    factor near 5 dB rather than the ~10 dB a random-phase multitone would
    give, so the transmitter's ALC is not the thing being measured.
    """
    bins = bins_in_band(fft_size)
    phases = _newman_phases(len(bins))
    bin_symbols = np.exp(1j * phases)
    spec = np.zeros(fft_size, dtype=np.complex128)
    for b, s in zip(bins, bin_symbols):
        spec[b] = s
        spec[fft_size - b] = np.conj(s)
    wave = np.real(np.fft.ifft(spec)) * fft_size
    wave = wave / (np.max(np.abs(wave)) + 1e-12)
    return wave, bins, bin_symbols


def crest_db(x):
    return float(20 * np.log10(np.max(np.abs(x)) / (np.sqrt(np.mean(x ** 2)) + 1e-15)))


def upsample_to_tx(x):
    up = 4
    stuffed = np.zeros(len(x) * up)
    stuffed[::up] = x
    lpf = _sc._design_interp_lpf(up)
    return (np.convolve(stuffed, lpf, mode="same") * up).astype(np.float32)


def build_burst(blocks, repeats, drive, lead_s, gap_s, tail_s):
    """[lead silence][block0 x repeats][gap][block1 x repeats]...[tail]."""
    pieces = [np.zeros(int(lead_s * DESIGN_RATE))]
    layout = []
    for i, fft_size in enumerate(blocks):
        wave, bins, _ = sounding_symbol(fft_size)
        rep = np.tile(wave, repeats)
        start = sum(len(p) for p in pieces)
        layout.append({"fft_size": int(fft_size), "start": int(start),
                       "n": int(len(rep)), "repeats": int(repeats),
                       "bins": bins.tolist(), "crest_db": crest_db(rep)})
        pieces.append(rep)
        if i != len(blocks) - 1:
            pieces.append(np.zeros(int(gap_s * DESIGN_RATE)))
    pieces.append(np.zeros(int(tail_s * DESIGN_RATE)))
    base = np.concatenate(pieces)
    base = base / (np.max(np.abs(base)) + 1e-12) * drive
    return base, layout


def _freq_shift(x, hz, rate=DESIGN_RATE):
    n = np.arange(len(x))
    return x * np.exp(2j * np.pi * hz * n / rate)


def _analytic(x):
    n = len(x)
    spec = np.fft.fft(x)
    h = np.zeros(n)
    h[0] = 1.0
    if n % 2 == 0:
        h[n // 2] = 1.0
        h[1:n // 2] = 2.0
    else:
        h[1:(n + 1) // 2] = 2.0
    return np.fft.ifft(spec * h)


def locate_block(capture, symbol, repeats, search_hz=30.0, step_hz=2.0,
                 template_repeats=2):
    """Matched-filter a short prefix of the repeated block over a coarse
    frequency grid. Returns (start_index, coarse_hz, peak_ratio).

    Only a couple of repeats are used as the template deliberately: a longer
    one has more processing gain but narrows the frequency response of the
    correlator itself, so the coarse grid would have to be far finer. The
    frequency estimate is refined afterwards from the block's own periodicity,
    which is exact and needs no search.
    """
    template = np.tile(symbol, min(repeats, template_repeats))
    z = _analytic(capture)
    tz = _analytic(template)
    m = len(tz)
    if len(z) < m + 4:
        return None
    energy = np.sqrt(np.sum(np.abs(tz) ** 2))
    # sliding local energy, so a loud transient elsewhere in the recording
    # cannot win the peak on raw amplitude alone
    power = np.abs(z) ** 2
    csum = np.concatenate([[0.0], np.cumsum(power)])
    local = np.sqrt(np.maximum(csum[m:] - csum[:-m], 0.0)) + 1e-12
    best = (-1.0, None, 0.0)
    for hz in np.arange(-search_hz, search_hz + 1e-9, step_hz):
        shifted = tz * np.exp(2j * np.pi * hz * np.arange(m) / DESIGN_RATE)
        # np.correlate conjugates its second argument, which is what a
        # matched filter wants -- do not pre-conjugate here.
        env = np.abs(np.correlate(z, shifted, mode="valid"))
        ratio_curve = env / (energy * local[:len(env)])
        top = float(np.max(ratio_curve))
        if top > best[0]:
            best = (top, ratio_curve, float(hz))
    top, curve, hz = best
    if curve is None:
        return None
    # The block is periodic, so the matched filter peaks near-equally at every
    # repeat boundary. argmax would pick an arbitrary one -- usually a late
    # one, whose remaining repeats run off the end of the block. Take the
    # earliest position that is essentially as good as the best.
    hits = np.flatnonzero(curve >= 0.85 * top)
    return int(hits[0]), hz, top


def _bin_matrix(capture, s0, fft_size, n_rep, bins, hz):
    z = _freq_shift(_analytic(capture[s0: s0 + n_rep * fft_size]), -hz)
    return np.array([np.fft.fft(z[r * fft_size:(r + 1) * fft_size])[bins] / fft_size
                     for r in range(n_rep)])


def estimate_block(capture, fft_size, repeats, bins, bin_syms, symbol,
                   seed_hz=None):
    """Per-carrier |H| and SNR from one periodic sounding block.

    Fine timing is deliberately not searched. The block is exactly periodic,
    so any window that lies wholly inside it is a valid symbol -- a timing
    error only adds a linear phase ramp across bins, which changes neither
    |H| nor the residual-variance noise estimate. One whole symbol is skipped
    at each end to absorb the coarse locator's error and the channel's
    delay spread.

    seed_hz, when given, is a frequency offset already measured on a
    wider-spaced block of the same burst. Both the correlator search and the
    alias cell are then centred on it. A narrow-spaced block cannot resolve
    its own offset modulo the spacing -- at 25 Hz spacing a one-spacing slip
    is inside the +/-30 Hz search range and shifts every bin index by one,
    which reads out as a plausible-looking but wholly wrong channel response.
    """
    spacing = DESIGN_RATE / fft_size
    if seed_hz is None:
        loc = locate_block(capture, symbol, repeats)
    else:
        loc = locate_block(capture, symbol, repeats,
                           search_hz=min(30.0, spacing / 2.0), step_hz=1.0)
    if loc is None:
        return {"located": False}
    start, coarse_hz, ratio = loc
    if seed_hz is not None:
        coarse_hz = float(seed_hz)
    n_rep = repeats - 2
    s0 = start + fft_size
    if n_rep < 4 or s0 < 0 or s0 + n_rep * fft_size > len(capture):
        return {"located": False}

    # Refine the frequency from the block's own periodicity: a residual
    # offset is exactly a constant phase step from one repeat to the next.
    # Unambiguous within +/- half the subcarrier spacing, which the coarse
    # estimate is assumed to already be inside.
    hz = coarse_hz
    for _ in range(3):
        xr = _bin_matrix(capture, s0, fft_size, n_rep, bins, hz)
        step = np.angle(np.sum(xr[1:] * np.conj(xr[:-1])))
        hz += step * DESIGN_RATE / (2 * np.pi * fft_size)
        # A whole-spacing slip shifts every bin index by one and scrambles the
        # measured response. Keep the estimate in the coarse one's own cell.
        hz -= spacing * round((hz - coarse_hz) / spacing)
    xr = _bin_matrix(capture, s0, fft_size, n_rep, bins, hz)

    h = xr.mean(axis=0)
    noise = np.sum(np.abs(xr - h) ** 2, axis=0) / (n_rep - 1)
    snr = (np.abs(h) ** 2) / (noise + 1e-30)
    coh = float(np.sum(np.abs(h) ** 2))
    inc = float(np.mean(np.sum(np.abs(xr - h) ** 2, axis=1)))
    hc = h / bin_syms  # remove the known transmit phases: this is the channel
    return {
        "located": True, "start": int(s0), "corr_ratio": float(ratio),
        "repeats_used": int(n_rep), "freq_offset_hz": float(hz),
        "coherence_score_db": float(10 * np.log10(coh / (inc + 1e-18) + 1e-30)),
        "snr_db": (10 * np.log10(snr + 1e-30)).tolist(),
        "mag_db": (20 * np.log10(np.abs(hc) + 1e-30)).tolist(),
        "phase_rad": np.angle(hc).tolist(),
        "bins": bins.tolist(),
        "freqs_hz": (bins * DESIGN_RATE / fft_size).tolist(),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tx", default="ic705")
    ap.add_argument("--rx", default="ic7300")
    ap.add_argument("--allow-ic705-tx", action="store_true")
    ap.add_argument("--blocks", type=int, nargs="+", default=[240, 480])
    ap.add_argument("--repeats", type=int, default=24)
    ap.add_argument("--drive", type=float, default=0.5)
    ap.add_argument("--drives", type=float, nargs="+",
                    help="sweep transmit drive levels in one radio session; "
                         "the received SNR this produces is what says whether "
                         "the link is receiver-noise-limited (SNR tracks drive) "
                         "or distortion-limited (SNR does not move)")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--noise-seconds", type=float, default=1.5)
    ap.add_argument("--capture-tail", type=float, default=1.0)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    if args.tx == "ic705" and not args.allow_ic705_tx:
        ap.error("transmitting on the IC-705 requires --allow-ic705-tx")

    drives = args.drives if args.drives else [args.drive]
    bursts = {}
    for d in drives:
        base, layout = build_burst(args.blocks, args.repeats, d,
                                   lead_s=0.30, gap_s=0.30, tail_s=0.30)
        bursts[d] = (base, upsample_to_tx(base))
    base, _ = bursts[drives[0]]
    print(f"sounder: tx={args.tx} rx={args.rx} blocks={args.blocks} "
          f"repeats={args.repeats} burst={len(base) / DESIGN_RATE:.2f}s "
          f"crest={crest_db(base):.1f}dB drives={drives}")
    for b in layout:
        print(f"  block fft={b['fft_size']} "
              f"spacing={DESIGN_RATE / b['fft_size']:.1f}Hz "
              f"carriers={len(b['bins'])} block_crest={b['crest_db']:.1f}dB")

    symbols = {f: sounding_symbol(f) for f in args.blocks}

    args.out.mkdir(parents=True, exist_ok=True)
    record = {"utc": datetime.now(timezone.utc).isoformat(),
              "tx": args.tx, "rx": args.rx, "drive": args.drive,
              "repeats": args.repeats, "blocks": args.blocks, "drives": drives,
              "burst_seconds": len(base) / DESIGN_RATE,
              "burst_crest_db": crest_db(base), "trials": []}

    txp = RadioTransport(args.tx)
    try:
        rxp = RadioTransport(args.rx, receive_only=True)
    except Exception:
        txp.close()
        raise
    try:
        rxp.start_receiving()
        time.sleep(3.0)
        rxp.consume_rx(len(rxp.snapshot_rx()))
        time.sleep(args.noise_seconds)
        noise = np.asarray(rxp.snapshot_rx(), dtype=np.float64)
        noise_rms = float(np.sqrt(np.mean(noise ** 2))) if noise.size else None
        noise_psd = None
        if noise.size >= 2400:
            trimmed = noise[:len(noise) // 240 * 240].reshape(-1, 240)
            spec = np.fft.rfft(trimmed, axis=1) / 240
            noise_psd = (10 * np.log10(np.mean(np.abs(spec) ** 2, axis=0) + 1e-30)).tolist()
        record["noise_rms"] = noise_rms
        record["noise_psd_db_fft240"] = noise_psd
        print(f"  idle receiver noise rms={noise_rms:.5f}")

        plan = [(d, t) for d in drives for t in range(1, args.trials + 1)]
        for idx, (drive, trial) in enumerate(plan, start=1):
            tx_audio = bursts[drive][1]
            rxp.consume_rx(len(rxp.snapshot_rx()))
            keyed = txp.send(tx_audio)
            time.sleep(args.capture_tail)
            cap = np.asarray(rxp.snapshot_rx(), dtype=np.float64)
            tag = f"drive{drive:g}_t{trial:02d}" if len(drives) > 1 else f"capture{trial:02d}"
            np.save(args.out / f"{tag}.npy", cap.astype(np.float32))
            entry = {"trial": idx, "drive": drive, "repeat": trial,
                     "capture_file": f"{tag}.npy",
                     "keyed_seconds": keyed,
                     "rx_samples": int(cap.size),
                     "rms": float(np.sqrt(np.mean(cap ** 2))) if cap.size else None,
                     "peak": float(np.max(np.abs(cap))) if cap.size else None,
                     "clipped": int(np.sum(np.abs(cap) >= 0.999)) if cap.size else 0,
                     "blocks": {}}
            # widest spacing first: its frequency estimate has the largest
            # unambiguous range and seeds every narrower block
            seed_hz = None
            for fft_size in sorted(args.blocks):
                wave, bins, bin_syms = symbols[fft_size]
                est = estimate_block(cap, fft_size, args.repeats, bins, bin_syms,
                                     wave, seed_hz=seed_hz)
                if est.get("located") and seed_hz is None:
                    seed_hz = est["freq_offset_hz"]
                entry["blocks"][str(fft_size)] = est
                if est.get("located"):
                    s = np.array(est["snr_db"])
                    mg = np.array(est["mag_db"])
                    print(f"    d={drive:g} t{trial} fft{fft_size}: "
                          f"foff={est['freq_offset_hz']:+.2f}Hz "
                          f"corr={est['corr_ratio']:.3f} snr min/p25/med/p75/max="
                          f"{s.min():.1f}/{np.percentile(s, 25):.1f}/{np.median(s):.1f}/"
                          f"{np.percentile(s, 75):.1f}/{s.max():.1f}dB "
                          f"resp_span={mg.max() - mg.min():.1f}dB")
                else:
                    print(f"    d={drive:g} t{trial} fft{fft_size}: NOT LOCATED")
            print(f"    d={drive:g} t{trial}: keyed={keyed:.2f}s rms={entry['rms']:.4f} "
                  f"peak={entry['peak']:.3f} clipped={entry['clipped']}")
            record["trials"].append(entry)
            time.sleep(0.5)
    finally:
        txp.close()
        rxp.close()

    (args.out / "sounding.json").write_text(json.dumps(record, indent=1))
    print(f"\nwrote {args.out / 'sounding.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
