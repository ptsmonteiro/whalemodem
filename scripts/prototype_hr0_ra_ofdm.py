"""Known-boundary screen for an exploratory very-low-SNR HF waveform.

This is deliberately not a registered production mode.  It combines coherent
pilot-assisted OFDM with a systematic repeat-accumulate (RA) code.  The RA
decoder exchanges soft information between a repetition constraint and an
exact log-MAP two-state accumulator, i.e. a small turbo decoder.
"""
from __future__ import annotations

import argparse
import numpy as np
from scipy.signal import resample_poly

from whale.channel import AwgnChannel, ChannelChain, SnrSpec, WattersonChannel

RATE = 48_000
BASE = 8_000
NFFT = 256
CP = 64                         # 8 ms, greater than the 2 ms target delay
BINS = np.arange(20, 84, 4)     # 625..2500 Hz; 1875 Hz bin span
SYMBOLS = 200                   # exactly 8.000 seconds
TRAINING = 10
Q = 6
K = 217
N = K * (Q + 1)                # 1519 coded bits
DATA_ROWS = np.arange(TRAINING + 1, SYMBOLS, 2)
PILOT_ROWS = np.arange(TRAINING, SYMBOLS, 2)


def ra_encode(bits: np.ndarray, permutation: np.ndarray) -> np.ndarray:
    repeated = np.repeat(bits.astype(np.uint8), Q)[permutation]
    parity = np.bitwise_xor.accumulate(repeated)
    return np.concatenate((bits, parity))


def _accumulator_siso(channel: np.ndarray, prior: np.ndarray) -> np.ndarray:
    """Max-log extrinsic LLR for x where p[i]=p[i-1] xor x[i]."""
    m = len(prior)
    alpha = np.full((m + 1, 2), -np.inf); alpha[0, 0] = 0.0
    beta = np.zeros((m + 1, 2))
    for i in range(m):
        a0, a1 = alpha[i]; lp, lc = prior[i]/2, channel[i]/2
        alpha[i+1, 0] = max(a0+lp+lc, a1-lp+lc)
        alpha[i+1, 1] = max(a0-lp-lc, a1+lp-lc)
        alpha[i+1] -= np.max(alpha[i+1])
    for i in range(m-1, -1, -1):
        b0, b1 = beta[i+1]; lp, lc = prior[i]/2, channel[i]/2
        beta[i, 0] = max(b0+lp+lc, b1-lp-lc)
        beta[i, 1] = max(b0-lp+lc, b1+lp-lc)
        beta[i] -= np.max(beta[i])
    out = np.empty(m)
    for i in range(m):
        a0, a1 = alpha[i]; b0, b1 = beta[i+1]; lc = channel[i]/2
        zero = max(a0+b0+lc, a1+b1-lc)
        one = max(a0+b1-lc, a1+b0+lc)
        out[i] = zero - one
    return np.clip(out, -40, 40)


def ra_decode(llr: np.ndarray, permutation: np.ndarray, iterations: int = 8):
    sys, parity = llr[:K], llr[K:]
    inv = np.argsort(permutation)
    acc_ext = np.zeros(K * Q)
    posterior = sys.copy()
    for _ in range(iterations):
        deperm = acc_ext[inv].reshape(K, Q)
        posterior = sys + deperm.sum(axis=1)
        repeat_prior = np.repeat(posterior, Q) - deperm.ravel()
        acc_ext = _accumulator_siso(parity, repeat_prior[permutation])
    return posterior < 0


def modulate(codeword: np.ndarray) -> np.ndarray:
    grid = np.ones((SYMBOLS, len(BINS)), dtype=np.complex128)
    padded = np.pad(codeword, (0, len(DATA_ROWS)*len(BINS)-len(codeword)))
    grid[DATA_ROWS] = (1 - 2 * padded.astype(float)).reshape(len(DATA_ROWS), -1)
    blocks = []
    for row in grid:
        spectrum = np.zeros(NFFT // 2 + 1, complex)
        spectrum[BINS] = row
        body = np.fft.irfft(spectrum, NFFT) * np.sqrt(NFFT / (2*len(BINS)))
        blocks.append(np.concatenate((body[-CP:], body)))
    base = np.concatenate(blocks)
    return resample_poly(base, RATE // BASE, 1).astype(np.float32)


def demodulate(audio: np.ndarray) -> np.ndarray:
    base = resample_poly(audio, 1, RATE // BASE)
    base = base[:SYMBOLS*(NFFT+CP)].reshape(SYMBOLS, NFFT+CP)[:, CP:]
    y = np.fft.rfft(base, axis=1)[:, BINS]
    # Even rows are known pilots. Average adjacent pilots: an 80 ms update is
    # fast relative to the target's 1 Hz maximum Doppler spread.
    pilots = y[PILOT_ROWS]
    # The worst differential delay is 2 ms (about 500 Hz coherence BW), so a
    # three-carrier/250 Hz frequency smoother buys pilot SNR without averaging over
    # an entire selective fade.
    padded = np.pad(pilots, ((0, 0), (1, 1)), mode="edge")
    pilots = sum(padded[:, j:j+len(BINS)] for j in range(3)) / 3
    tp = np.pad(pilots, ((2, 2), (0, 0)), mode="edge")
    pilots = sum(tp[j:j+len(pilots)] for j in range(5)) / 5
    hd = (pilots[:-1] + pilots[1:]) / 2
    # The final data row has only its preceding pilot.
    hd = np.vstack((hd, pilots[-1]))
    soft = np.real(y[DATA_ROWS] * np.conj(hd))
    return soft.ravel()[:N]


def _fft_grid(audio: np.ndarray) -> np.ndarray:
    base = resample_poly(audio, 1, RATE // BASE)
    base = base[:SYMBOLS*(NFFT+CP)].reshape(SYMBOLS, NFFT+CP)[:, CP:]
    return np.fft.rfft(base, axis=1)[:, BINS]


def trial(preset: str, seed: int, snr_db: float, oracle: bool = False) -> tuple[bool, int]:
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 2, K, dtype=np.uint8)
    permutation = np.random.default_rng(0x485230).permutation(K*Q)
    tx = modulate(ra_encode(bits, permutation))
    channel = ChannelChain((
        WattersonChannel.from_preset(RATE, preset, seed=seed+10_000),
        AwgnChannel(RATE, SnrSpec(snr_db, reference_start=0,
                                  reference_stop=len(tx)), seed=seed+20_000)))
    rx = channel.process(tx).audio[:len(tx)]
    if oracle:
        clean = WattersonChannel.from_preset(
            RATE, preset, seed=seed+10_000).process(tx).audio[:len(tx)]
        yt, yc, yr = _fft_grid(tx), _fft_grid(clean), _fft_grid(rx)
        h = yc[DATA_ROWS] / (yt[DATA_ROWS] + 1e-12)
        soft = np.real(yr[DATA_ROWS] * np.conj(h)).ravel()[:N]
    else:
        soft = demodulate(rx)
    # Robust scale estimate; absolute noise variance only scales all LLRs.
    scale = np.median(np.abs(soft)) + 1e-9
    decoded = ra_decode(np.clip(2*soft/scale, -20, 20), permutation)
    errors = int(np.count_nonzero(decoded != bits))
    return errors == 0, errors


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--snr", type=float, default=-24.0309,
                    help="repository waveform SNR; -24.0309 = -15 dB/3 kHz")
    ap.add_argument("--oracle", action="store_true",
                    help="diagnostic exact per-symbol/per-carrier channel response")
    args = ap.parse_args()
    span = (BINS[-1] - BINS[0]) * BASE / NFFT
    print(f"RA-OFDM K={K} N={N} q={Q}, {len(BINS)} carriers, 25 sym/s, 8.000 s")
    print(f"payload={K/8:.2f} B, nominal={K/8:.3f} bit/s, occupied-bin-span={span:.2f} Hz")
    for label in ("mid_latitude_quiet", "mid_latitude_moderate",
                  "mid_latitude_disturbed"):
        rows = [trial(label, 1000*i+7, args.snr, args.oracle)
                for i in range(args.trials)]
        print(label, f"{sum(ok for ok,_ in rows)}/{len(rows)}", [e for _,e in rows])


if __name__ == "__main__":
    main()
