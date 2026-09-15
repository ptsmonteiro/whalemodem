"""Single-carrier QAM MER probe for the FM bench.

Answers one question: what is the highest QAM order a simple single-carrier
waveform could carry between the two FM radios? Rather than building a mode per
order and watching which ones fail, it sends one dense constellation (1024-QAM
by default) and measures modulation error ratio (MER) after equalisation. The
received error vectors then give a projected symbol error rate for every
square order, using the path's real error distribution rather than a Gaussian
assumption.

Waveform: root-raised-cosine single carrier centred on --center Hz, at
--rate baud. Burst = ~1 s random QPSK lead-in (opens squelch, settles AGC on a
signal with the payload's spectrum), known QPSK training, known random M-QAM
data, a short QPSK tail. All sections have the same average power.

Receiver (12 kHz, the production RX rate): analytic correlation on the training
block, downconversion, matched filter, fine timing on the training, then a
T/2-spaced linear equaliser. Three equalisers are reported:
  static  -- least squares fitted on the training only, then frozen.
  tracked -- the static taps, then data-aided NLMS plus a second-order phase
             loop across the whole burst. This is what a decision-directed
             receiver achieves once its decisions are mostly right, so it is
             the number that sets the usable order.
  genie   -- least squares fitted on the data block itself. No receiver can
             do this; it is the floor for a static linear equaliser.

Reading the result. Noise-limited paths gain MER as --rate drops (the matched
filter's noise bandwidth is the symbol rate). Distortion-limited paths don't.
The inner/outer ring figures separate the two: compression shows as lower gain
and worse MER on the outer ring. Sweep --peak for the intermodulation test,
as in measure_ofdm_fm_burst.py.

Run from the repository root:
    python scripts/measure_fm_qam_mer.py --simulate 20           # no radios
    python scripts/measure_fm_qam_mer.py --rate 300,600,1200 --peak 0.04,0.077,0.15
    python scripts/measure_fm_qam_mer.py --reanalyse logs/fm_qam_mer/<run>.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.interpolate import CubicSpline
from scipy.optimize import brentq
from scipy.signal import fftconvolve, upfirdn
from scipy.special import erfc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from whale import rx_audio  # noqa: E402

TX_SAMPLE_RATE = 48_000
RX_SAMPLE_RATE = 12_000

ROLLOFF = 0.25
RRC_SPAN_SYMBOLS = 12
LEADIN_SECONDS = 1.0
TAIL_SECONDS = 0.3
TRAINING_SYMBOLS = 256
EQ_HALF_TAPS = 16                 # 33 taps at T/2: +/-8 symbols
NLMS_MU = 0.005
PLL_ALPHA = 0.02
PLL_BETA = PLL_ALPHA ** 2 / 4
SKIP_DATA_SYMBOLS = 20            # tracking settle at the training->data edge
FADE_SECONDS = 0.005
MER_WINDOW_SECONDS = 0.5
TARGET_SER = 1e-3
REPORT_ORDERS = (4, 16, 64, 256, 1024)

LEADIN_SEED = 0x1EAD
TRAINING_SEED = 0x7A11
TAIL_SEED = 0x7A1
SETTLE_AFTER_OPEN = 0.3
CAPTURE_TAIL = 1.0
INTER_TRIAL = 0.5


# -- waveform ---------------------------------------------------------------

def rrc_taps(sps: int) -> np.ndarray:
    """Unit-energy root-raised-cosine, RRC_SPAN_SYMBOLS long, odd length."""
    t = np.arange(-RRC_SPAN_SYMBOLS * sps // 2, RRC_SPAN_SYMBOLS * sps // 2 + 1) / sps
    b = ROLLOFF
    h = np.empty_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-12:
            h[i] = 1.0 - b + 4 * b / np.pi
        elif abs(abs(ti) - 1 / (4 * b)) < 1e-9:
            h[i] = (b / np.sqrt(2)) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * b))
                                       + (1 - 2 / np.pi) * np.cos(np.pi / (4 * b)))
        else:
            h[i] = ((np.sin(np.pi * ti * (1 - b)) + 4 * b * ti * np.cos(np.pi * ti * (1 + b)))
                    / (np.pi * ti * (1 - (4 * b * ti) ** 2)))
    return h / np.sqrt(np.sum(h ** 2))


def qam_points(order: int) -> np.ndarray:
    """Square M-QAM, unit average power, index = i_I * L + i_Q."""
    levels = int(round(np.sqrt(order)))
    if levels * levels != order or levels < 2:
        raise ValueError("order must be a square number >= 4")
    axis = 2.0 * np.arange(levels) - (levels - 1)
    grid = (axis[:, None] + 1j * axis[None, :]).reshape(-1)
    return grid / np.sqrt(2.0 * (order - 1) / 3.0)


def random_symbols(seed: int, count: int, order: int):
    idx = np.random.default_rng(seed).integers(0, order, size=count)
    return qam_points(order)[idx], idx


def check_rate(rate: int, center: float) -> None:
    for fs in (TX_SAMPLE_RATE, RX_SAMPLE_RATE):
        if fs % rate or (fs // rate) % 2:
            raise ValueError(f"{rate} baud needs an even integer samples/symbol at {fs} Hz")
    lo = center - rate * (1 + ROLLOFF) / 2
    hi = center + rate * (1 + ROLLOFF) / 2
    if lo < 250 or hi > 3050:
        raise ValueError(f"{rate} baud at {center:g} Hz occupies {lo:.0f}-{hi:.0f} Hz, "
                         "outside the FM audio passband")


def burst_symbols(rate: int, order: int, seconds: float, seed: int) -> dict:
    lead, _ = random_symbols(LEADIN_SEED, int(LEADIN_SECONDS * rate), 4)
    train, _ = random_symbols(TRAINING_SEED, TRAINING_SYMBOLS, 4)
    data, data_idx = random_symbols(seed, int(seconds * rate), order)
    tail, _ = random_symbols(TAIL_SEED, int(TAIL_SECONDS * rate), 4)
    return {
        "all": np.concatenate([lead, train, data, tail]),
        "train_first": len(lead),
        "data_first": len(lead) + len(train),
        "data": data,
        "data_idx": data_idx,
    }


def modulate(symbols: np.ndarray, rate: int, center: float, fs: int) -> np.ndarray:
    sps = fs // rate
    baseband = upfirdn(rrc_taps(sps), symbols, up=sps)
    n = np.arange(len(baseband))
    return np.real(baseband * np.exp(2j * np.pi * center * n / fs)) * np.sqrt(2.0)


def build_burst(rate: int, center: float, order: int, seconds: float,
                seed: int, peak: float) -> dict:
    sym = burst_symbols(rate, order, seconds, seed)
    audio = modulate(sym["all"], rate, center, TX_SAMPLE_RATE)
    papr_db = 20 * np.log10(np.max(np.abs(audio)) / np.sqrt(np.mean(audio ** 2)))
    audio = audio * (peak / np.max(np.abs(audio)))
    ramp = np.linspace(0.0, 1.0, int(FADE_SECONDS * TX_SAMPLE_RATE))
    audio[:len(ramp)] *= ramp
    audio[-len(ramp):] *= ramp[::-1]
    return {"audio": audio.astype(np.float32), "symbols": sym, "papr_db": float(papr_db)}


# -- receiver ---------------------------------------------------------------

def ser_awgn(order: int, mer_db: float) -> float:
    q = 0.5 * erfc(np.sqrt(3 * 10 ** (mer_db / 10) / (order - 1)) / np.sqrt(2))
    p_axis = 2 * (1 - 1 / np.sqrt(order)) * q
    return 1 - (1 - p_axis) ** 2


def required_mer_db(order: int, ser: float = TARGET_SER) -> float:
    return float(brentq(lambda m: ser_awgn(order, m) - ser, -10, 60))


def projected_ser(errors: np.ndarray, order: int) -> float:
    """Fraction of error vectors that would cross a decision boundary of
    square M-QAM at unit power. Treats every point as interior, so it is
    slightly pessimistic (edge points can't err outward)."""
    half = np.sqrt(6.0 / (order - 1)) / 2
    return float(np.mean((np.abs(errors.real) > half) | (np.abs(errors.imag) > half)))


def mer_db(received: np.ndarray, sent: np.ndarray) -> float:
    err = np.mean(np.abs(received - sent) ** 2)
    return float(10 * np.log10(np.mean(np.abs(sent) ** 2) / max(err, 1e-15)))


def gain_normalise(received: np.ndarray, sent: np.ndarray) -> np.ndarray:
    g = np.vdot(sent, received) / np.vdot(sent, sent)
    return received / g


def analyse(captured: np.ndarray, rate: int, center: float, order: int,
            seconds: float, seed: int) -> dict:
    captured = np.asarray(captured, dtype=np.float64)
    sym = burst_symbols(rate, order, seconds, seed)
    sps = RX_SAMPLE_RATE // rate
    h = rrc_taps(sps)
    delay = (len(h) - 1) // 2

    # Acquisition: analytic correlation against the training block's passband.
    reference = modulate(sym["all"], rate, center, RX_SAMPLE_RATE)
    a = sym["train_first"] * sps
    ref = reference[a:a + TRAINING_SYMBOLS * sps]
    needed = len(reference)
    if len(captured) < needed:
        return {"acquired": False, "reason": f"capture too short ({len(captured)} samples)"}

    def analytic(x):
        spec = np.fft.fft(x)
        spec[len(x) // 2 + 1:] = 0
        spec[1:(len(x) + 1) // 2] *= 2
        return np.fft.ifft(spec)

    corr = np.abs(fftconvolve(analytic(captured), np.conj(analytic(ref)[::-1]), mode="valid"))
    peak = int(np.argmax(corr))
    confidence = float(corr[peak] / max(np.median(corr), 1e-15))
    start = peak - a
    if start < 0 or start + needed > len(captured):
        return {"acquired": False, "reason": "burst runs off the capture",
                "confidence": confidence}

    n = np.arange(len(captured))
    baseband = 2.0 * captured * np.exp(-2j * np.pi * center * n / RX_SAMPLE_RATE)
    y = fftconvolve(baseband, h, mode="full")

    # Fine timing on the training symbols.
    k_train = sym["train_first"] + np.arange(TRAINING_SYMBOLS)
    train = sym["all"][k_train]
    base = start + 2 * delay
    best = None
    for off in range(-sps, sps + 1):
        v = y[base + off + k_train * sps]
        score = abs(np.vdot(train, v)) ** 2 / (np.vdot(v, v).real + 1e-15)
        if best is None or score > best[1]:
            best = (off, score)
    base += best[0]

    # Clock offset. The sound cards differ by a few ppm, which over a burst
    # is a fraction of a sample -- enough to cap MER near 30 dB if the
    # equaliser has to chase it. A delay of tau samples rotates the
    # downconverted signal by -2*pi*fc*tau/fs, so the phase of a known-symbol
    # correlation at the start versus the end of the burst measures the drift
    # unambiguously for |drift| < fs/(2 fc) samples.
    first = sym["train_first"]
    last = sym["data_first"] + len(sym["data"])
    k_end = np.arange(last - TRAINING_SYMBOLS, last)
    phase_start = np.angle(np.vdot(train, y[base + k_train * sps]))
    phase_end = np.angle(np.vdot(sym["all"][k_end], y[base + k_end * sps]))
    drift = -np.angle(np.exp(1j * (phase_end - phase_start))) * RX_SAMPLE_RATE / (2 * np.pi * center)
    clock = drift / ((k_end.mean() - k_train.mean()) * sps)

    ks = np.arange(first, last)
    tap_offsets = np.arange(-EQ_HALF_TAPS, EQ_HALF_TAPS + 1) * (sps // 2)
    positions = base + (ks[:, None] * sps + tap_offsets[None, :]) * (1 + clock)
    grid = np.arange(len(y))
    X = (CubicSpline(grid, y.real)(positions) + 1j * CubicSpline(grid, y.imag)(positions))
    # The same drift also scales the carrier: undo its phase ramp so the
    # static and genie equalisers aren't charged for a moving phase.
    X *= np.exp(2j * np.pi * center * clock * (positions - base) / RX_SAMPLE_RATE)
    d = sym["all"][ks]
    n_train = TRAINING_SYMBOLS
    data_slice = slice(n_train + SKIP_DATA_SYMBOLS, len(ks))
    sent = d[data_slice]

    w_static, *_ = np.linalg.lstsq(X[EQ_HALF_TAPS:n_train], d[EQ_HALF_TAPS:n_train], rcond=None)
    static = gain_normalise(X[data_slice] @ w_static, sent)
    w_genie, *_ = np.linalg.lstsq(X[data_slice], sent, rcond=None)
    genie = X[data_slice] @ w_genie

    w = w_static.copy()
    theta = 0.0
    nu = 0.0
    tracked = np.empty(len(ks), dtype=complex)
    for i in range(len(ks)):
        x = X[i]
        rot = np.exp(-1j * theta)
        out = (x @ w) * rot
        tracked[i] = out
        di = d[i]
        phase_err = np.imag(out * np.conj(di)) / (abs(di) ** 2 + 1e-12)
        nu += PLL_BETA * phase_err
        theta += PLL_ALPHA * phase_err + nu
        e = (di - out) / rot
        w += NLMS_MU * e * np.conj(x) / (np.vdot(x, x).real + 1e-15)
    tracked = gain_normalise(tracked[data_slice], sent)
    errors = tracked - sent

    radius = np.abs(sent)
    inner = radius <= np.quantile(radius, 0.4)
    outer = radius >= np.quantile(radius, 0.9)

    def ring(mask):
        # MER here is error power against the WHOLE constellation's unit
        # average power, so additive noise reads equal on both rings and
        # only amplitude-dependent distortion separates them.
        g = np.real(np.vdot(sent[mask], tracked[mask])) / np.vdot(sent[mask], sent[mask]).real
        err = np.mean(np.abs(errors[mask]) ** 2)
        return float(20 * np.log10(max(g, 1e-9))), float(-10 * np.log10(max(err, 1e-15)))

    inner_gain, inner_mer = ring(inner)
    outer_gain, outer_mer = ring(outer)

    window = max(1, int(MER_WINDOW_SECONDS * rate))
    windows = [mer_db(tracked[j:j + window], sent[j:j + window])
               for j in range(0, len(sent) - window + 1, window)] or [mer_db(tracked, sent)]

    points = qam_points(order)
    sliced = np.argmin(np.abs(tracked[:, None] - points[None, :]), axis=1)
    hard_ser = float(np.mean(sliced != sym["data_idx"][SKIP_DATA_SYMBOLS:]))

    return {
        "acquired": True,
        "confidence": confidence,
        "start_sample": int(start),
        "timing_offset": int(best[0]),
        "clock_ppm": float(clock * 1e6),
        "mer_static_db": mer_db(static, sent),
        "mer_tracked_db": mer_db(tracked, sent),
        "mer_genie_db": mer_db(genie, sent),
        "mer_window_min_db": float(min(windows)),
        "mer_window_max_db": float(max(windows)),
        "inner_ring": {"gain_db": inner_gain, "mer_db": inner_mer},
        "outer_ring": {"gain_db": outer_gain, "mer_db": outer_mer},
        "phase_drift_deg": float(np.degrees(theta)),
        "hard_ser_sent_order": hard_ser,
        "projected_ser": {str(m): projected_ser(errors, m) for m in REPORT_ORDERS},
        "symbols_measured": int(len(sent)),
    }


def usable_order(result: dict) -> int | None:
    best = None
    for m in REPORT_ORDERS:
        if result["projected_ser"][str(m)] <= TARGET_SER:
            best = m
    return best


def report(label: str, result: dict) -> None:
    if not result["acquired"]:
        print(f"  [{label}] NOT ACQUIRED: {result['reason']}")
        return
    print(f"  [{label}] conf {result['confidence']:.0f}, timing {result['timing_offset']:+d}, "
          f"clock {result['clock_ppm']:+.1f} ppm; "
          f"MER static/tracked/genie = {result['mer_static_db']:.1f}/"
          f"{result['mer_tracked_db']:.1f}/{result['mer_genie_db']:.1f} dB "
          f"(0.5 s windows {result['mer_window_min_db']:.1f}..{result['mer_window_max_db']:.1f})")
    print(f"      inner ring gain {result['inner_ring']['gain_db']:+.2f} dB MER "
          f"{result['inner_ring']['mer_db']:.1f}; outer ring gain "
          f"{result['outer_ring']['gain_db']:+.2f} dB MER {result['outer_ring']['mer_db']:.1f}; "
          f"phase drift {result['phase_drift_deg']:+.1f} deg")
    ser = "  ".join(f"{m}:{result['projected_ser'][str(m)]:.1e}" for m in REPORT_ORDERS)
    order = usable_order(result)
    print(f"      projected SER {ser}  -> highest order at SER<={TARGET_SER:g}: "
          f"{order if order else 'none'}")


# -- channels ---------------------------------------------------------------

def simulate(audio: np.ndarray, snr_db: float, ppm: float, clip_db: float | None,
             rng: np.random.Generator) -> np.ndarray:
    """48 kHz TX audio -> 12 kHz capture with clock offset, optional hard clip,
    and white noise at snr_db in a 3 kHz reference bandwidth."""
    x = np.asarray(audio, dtype=np.float64)
    if clip_db is not None:
        limit = np.sqrt(np.mean(x ** 2)) * 10 ** (clip_db / 20)
        x = np.clip(x, -limit, limit)
    n = np.arange(len(x))
    x = np.interp(n * (1 + ppm * 1e-6), n, x, right=0.0)
    x = np.concatenate([np.zeros(int(0.7 * TX_SAMPLE_RATE)), x,
                        np.zeros(int(1.0 * TX_SAMPLE_RATE))])
    rx = rx_audio.downsample(x).astype(np.float64)
    active = rx[np.abs(rx) > 1e-6]
    signal_power = np.mean(active ** 2)
    # White noise of variance s2 at 12 kHz puts s2/2 into 3 kHz.
    sigma2 = 2 * signal_power / 10 ** (snr_db / 10)
    return rx + rng.normal(0, np.sqrt(sigma2), len(rx))


def radio_capture(tx, rx, audio: np.ndarray) -> np.ndarray:
    rx.consume_rx(len(rx.snapshot_rx()))
    tx.send(audio)
    time.sleep(CAPTURE_TAIL)
    return rx.snapshot_rx()


# -- main -------------------------------------------------------------------

def parse_list(text: str, kind):
    return [kind(part) for part in text.split(",") if part.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rate", default="300,600,1200", help="baud list (default: %(default)s)")
    ap.add_argument("--center", type=float, default=1500.0, help="carrier Hz")
    ap.add_argument("--order", type=int, default=1024, help="sent square QAM order")
    ap.add_argument("--seconds", type=float, default=4.0, help="data block length")
    ap.add_argument("--peak", default="0.04,0.077,0.15",
                    help="peak DAC amplitude list (default: %(default)s)")
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--direction", choices=["both", "ic705->ht", "ht->ic705"], default="both")
    ap.add_argument("--simulate", type=float, default=None, metavar="SNR_DB",
                    help="no radios: AWGN at this SNR in 3 kHz")
    ap.add_argument("--sim-ppm", type=float, default=4.0)
    ap.add_argument("--sim-clip-db", type=float, default=None,
                    help="hard clip this many dB above RMS in simulation")
    ap.add_argument("--reanalyse", type=Path, default=None,
                    help="re-run analysis on a saved run JSON and its captures")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "logs" / "fm_qam_mer")
    args = ap.parse_args(argv)

    if args.reanalyse:
        saved = json.loads(args.reanalyse.read_text())
        for trial in saved["trials"]:
            captured = np.load(args.reanalyse.parent / trial["capture"])
            p = trial["params"]
            result = analyse(captured, p["rate"], p["center"], p["order"], p["seconds"], p["seed"])
            report(trial["label"], result)
        return 0

    rates = parse_list(args.rate, int)
    peaks = parse_list(args.peak, float)
    for rate in rates:
        check_rate(rate, args.center)
    qam_points(args.order)

    print(f"single-carrier RRC a={ROLLOFF}, centre {args.center:g} Hz, sent {args.order}-QAM, "
          f"{args.seconds:g} s data")
    print("AWGN MER needed for SER " f"{TARGET_SER:g}: " + ", ".join(
        f"{m}-QAM {required_mer_db(m):.1f} dB" for m in REPORT_ORDERS))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = {"date": stamp, "simulate": args.simulate, "trials": []}
    rng = np.random.default_rng()

    def settings():
        for rate in rates:
            for peak in peaks:
                for trial in range(args.trials):
                    yield rate, peak, trial

    def one(tx_name, rx_name, capture_fn):
        for rate, peak, trial in settings():
            seed = 0xDA7A + trial
            burst = build_burst(rate, args.center, args.order, args.seconds, seed, peak)
            label = f"{tx_name}->{rx_name} {rate}bd peak{peak:g} #{trial + 1}"
            captured = capture_fn(burst["audio"])
            result = analyse(captured, rate, args.center, args.order, args.seconds, seed)
            report(label, result)
            params = {"rate": rate, "center": args.center, "order": args.order,
                      "seconds": args.seconds, "seed": seed, "peak": peak,
                      "papr_db": burst["papr_db"]}
            entry = {"label": label, "direction": f"{tx_name}->{rx_name}",
                     "params": params, "result": result}
            run["trials"].append(entry)
            if args.simulate is None:
                # Saved per trial so an interrupted bench run keeps its data.
                args.out_dir.mkdir(parents=True, exist_ok=True)
                name = f"{stamp}-{len(run['trials']) - 1:03d}.npy"
                np.save(args.out_dir / name, captured)
                entry["capture"] = name
                (args.out_dir / f"{stamp}.json").write_text(json.dumps(run, indent=2) + "\n")

    if args.simulate is not None:
        print(f"simulation: SNR {args.simulate:g} dB/3 kHz, {args.sim_ppm:g} ppm, "
              f"clip {args.sim_clip_db if args.sim_clip_db is not None else 'none'}")
        for rate in rates:
            gain = 10 * np.log10(3000 / rate)
            print(f"  expected MER at {rate} baud ~ {args.simulate + gain:.1f} dB")
        one("sim", "sim", lambda audio: simulate(audio, args.simulate, args.sim_ppm,
                                                 args.sim_clip_db, rng))
        return 0

    import bench  # noqa: E402  (opens hardware; not needed for simulation)

    with bench.radio_pair() as (t_ic705, t_ht):
        time.sleep(SETTLE_AFTER_OPEN)
        legs = []
        if args.direction in ("both", "ic705->ht"):
            legs.append((t_ic705, t_ht, "ic705", "ht"))
        if args.direction in ("both", "ht->ic705"):
            legs.append((t_ht, t_ic705, "ht", "ic705"))
        for tx, rx, tx_name, rx_name in legs:
            def capture(audio, tx=tx, rx=rx):
                captured = radio_capture(tx, rx, audio)
                time.sleep(INTER_TRIAL)
                return captured
            one(tx_name, rx_name, capture)

    print(f"\nwrote {args.out_dir / f'{stamp}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
