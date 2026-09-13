"""Stage-1 hardware probe for a 50 Hz-spaced OFDM mode on the FM bench.

Puts a real OFDM burst on the air and measures what a bit-loaded mode would
need to know: per-carrier SNR after equalisation, and raw uncoded QPSK bit
error rate, in BOTH directions, at several transmit clipping levels.

Geometry. 50 Hz carrier spacing, carriers on bins 6..60 -- 300..3000 Hz, the
band measure_ofdm_carrier_response.py found reliable in both directions. The
bin *indices* are the same at both sample rates, which is why the numbers
below look like a coincidence and are not: TX builds symbols at 48 kHz with a
960-point IFFT (48000/960 = 50 Hz) and RX analyses at 12 kHz with a 240-point
FFT (12000/240 = 50 Hz), so bin 6 is 300 Hz on both sides and no resampling
sits between the two.

Why this is written against plain numpy rather than whale/dsp: this is a
measurement harness, not the mode. It answers "does 50 Hz-spaced OFDM survive
this FM path, and how much can each carrier carry" with every unit visible at
the point of use. The mode that comes out of these numbers should be built on
the shared kernels (whale/dsp/ofdm.py, equalize.py, timing.py) instead.

What FM buys us, and why the receiver is this simple: an FM discriminator
outputs baseband audio, so there is no carrier frequency offset to estimate --
audio goes in at 300 Hz and comes out at 300 Hz. The two sound-card clocks
differ by only ~3.7 ppm (scripts/measure_clock_offset.py), which is well under
one sample of drift across this burst. So acquisition is a single correlation
against a known sync pattern, and one channel estimate covers the whole burst.
Neither would be safe on an HF/SSB path.

The clip sweep is the FM-specific measurement. Audio level into an FM
transmitter sets deviation, which is a hard PEAK limit, and OFDM's crest
factor is ~10-12 dB -- so for a fixed peak, clipping raises average power (and
every carrier's SNR with it) at the cost of clipping distortion. Somewhere in
between is the best drive for this radio pair, and nothing in this repo has
ever measured where. --clip is in dB above RMS; "none" means no clipping.

Run: python scripts/measure_ofdm_fm_burst.py
     python scripts/measure_ofdm_fm_burst.py --clip none,9,6 --direction ic705->ht
"""
import argparse
import csv
import sys
import time

import numpy as np
from scipy.signal import correlate, hilbert

import bench
from whale.transport import RX_SAMPLE_RATE, SAMPLE_RATE

CARRIER_SPACING_HZ = 50.0
BIN_LO, BIN_HI = 6, 60           # 300..3000 Hz inclusive -> 55 carriers
CARRIER_BINS = np.arange(BIN_LO, BIN_HI + 1)
N_CARRIERS = len(CARRIER_BINS)

NFFT_TX = int(SAMPLE_RATE / CARRIER_SPACING_HZ)      # 960 at 48 kHz
NFFT_RX = int(RX_SAMPLE_RATE / CARRIER_SPACING_HZ)   # 240 at 12 kHz
DECIMATION = SAMPLE_RATE // RX_SAMPLE_RATE           # 4

# Cyclic prefix, in samples at 12 kHz. The floor is set by our OWN receiver,
# before the radios get a say: rx_audio's anti-alias FIR is 129 taps at 48 kHz,
# i.e. its impulse response spans ~32 samples at 12 kHz, and a CP shorter than
# that leaks one symbol into the next no matter how clean the air path is. A
# CP of 24 (2 ms) was tried first and produced a symbol-dependent channel
# estimate -- textbook ISI -- on a noiseless offline loopback. 36 clears the
# decimator with a little room for the radios' own audio filters; --cp sweeps
# it, since only the hardware can say how much those filters add.
DEFAULT_CP_RX = 36
CP_RX = DEFAULT_CP_RX
CP_TX = CP_RX * DECIMATION
MIN_USEFUL_CP_RX = 33   # rx_audio's 129-tap FIR spans ~32 samples at 12 kHz

# Lead-in is OFDM symbols we never decode, not a tone: it has the payload's
# own spectrum and crest factor, so the receiver's squelch opens and its AGC
# settles on the same kind of signal it must then measure. framing.py sizes
# the equivalent CPFSK pad at 1.0 s against a radio that blacks out ~110 ms
# after squelch opens; this matches that budget.
LEADIN_SYMBOLS = 45
SYNC_SYMBOLS = 4                 # repeats of one known symbol, for acquisition
ESTIMATE_SYMBOLS = 4             # known symbols the channel estimate is fitted to
PAYLOAD_SYMBOLS = 40             # known pseudorandom QPSK, for BER

SYNC_SEED = 0x5104
ESTIMATE_SEED = 0xE57A
PAYLOAD_SEED = 0x9A17
LEADIN_SEED = 0x1EAD

PEAK_TARGET = 0.9
FADE_SECONDS = 0.005             # ramp in/out so the burst edges are not steps

# How far either side of the correlation peak refine_start() looks. Back far
# enough to undo the path's impulse-response delay (decimator ~33 samples at
# 12 kHz, plus whatever the radios' audio filters add); a little forward in
# case the peak lands early on a noisy capture.
SEARCH_BACK = 56
SEARCH_FORWARD = 8
SETTLE_AFTER_PTT = 0.3
CAPTURE_TAIL = 0.6


def qpsk(bits):
    """Gray QPSK, unit magnitude. bits is a flat 0/1 array, taken in pairs.

    The float cast is load-bearing: bits arrive as uint8, and `1 - 2*b`
    on an unsigned dtype wraps 1 to 255 instead of -1, which puts the
    constellation at {0.707, 180.3} rather than {+0.707, -0.707}. That stays
    self-consistent all the way through modulation and channel estimation --
    the fit is perfect, the EVM reads zero -- and only shows up as a ~0.5 BER,
    because the demapper's sign test can never fire when both points are
    positive.
    """
    b = bits.reshape(-1, 2).astype(np.float64)
    return ((1.0 - 2.0 * b[:, 0]) + 1j * (1.0 - 2.0 * b[:, 1])) / np.sqrt(2.0)


def random_symbol_values(seed, symbols):
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 2, size=(symbols * N_CARRIERS * 2), dtype=np.uint8)
    return qpsk(bits).reshape(symbols, N_CARRIERS), bits


def build_symbols(values, nfft, cp):
    """values is (symbols, carriers) -> real audio with a cyclic prefix."""
    out = []
    for row in values:
        spectrum = np.zeros(nfft // 2 + 1, dtype=complex)
        spectrum[CARRIER_BINS] = row
        body = np.fft.irfft(spectrum, nfft)
        out.append(np.concatenate([body[-cp:], body]))
    return np.concatenate(out)


def fade(audio, sample_rate):
    n = int(FADE_SECONDS * sample_rate)
    if n < 2 or len(audio) < 2 * n:
        return audio
    ramp = np.linspace(0.0, 1.0, n)
    audio = audio.copy()
    audio[:n] *= ramp
    audio[-n:] *= ramp[::-1]
    return audio


def build_burst(clip_db, peak=None):
    """The whole transmission at 48 kHz, plus the references RX needs.

    clip_db is the clipping threshold in dB above the unclipped RMS, or None
    for no clipping. Peak is normalised to PEAK_TARGET afterwards either way,
    so a tighter clip genuinely delivers more average power to the radio
    rather than just a quieter signal.
    """
    leadin, _ = random_symbol_values(LEADIN_SEED, LEADIN_SYMBOLS)
    sync_one, _ = random_symbol_values(SYNC_SEED, 1)
    sync = np.repeat(sync_one, SYNC_SYMBOLS, axis=0)
    estimate, _ = random_symbol_values(ESTIMATE_SEED, ESTIMATE_SYMBOLS)
    payload, payload_bits = random_symbol_values(PAYLOAD_SEED, PAYLOAD_SYMBOLS)

    values = np.vstack([leadin, sync, estimate, payload])
    audio = build_symbols(values, NFFT_TX, CP_TX)

    rms = float(np.sqrt(np.mean(audio ** 2)))
    raw_papr_db = 20 * np.log10(np.max(np.abs(audio)) / rms)
    if clip_db is not None:
        limit = rms * 10 ** (clip_db / 20.0)
        audio = np.clip(audio, -limit, limit)
    audio = audio * ((PEAK_TARGET if peak is None else peak)
                     / np.max(np.abs(audio)))
    audio = fade(audio, SAMPLE_RATE)

    sent_papr_db = 20 * np.log10(np.max(np.abs(audio))
                                 / np.sqrt(np.mean(audio ** 2)))
    return {
        "audio": audio.astype(np.float32),
        "sync_values": sync_one[0],
        "estimate_values": estimate,
        "payload_values": payload,
        "payload_bits": payload_bits,
        "raw_papr_db": raw_papr_db,
        "sent_papr_db": sent_papr_db,
    }


def sync_reference(sync_values):
    """The sync block as the receiver expects to see it, at 12 kHz."""
    return build_symbols(np.repeat(sync_values[None, :], SYNC_SYMBOLS, axis=0),
                         NFFT_RX, CP_RX)


def acquire(captured, reference):
    """Start index of the sync block in `captured`, and a confidence score.

    Correlates on the analytic signal so the peak survives whatever fixed
    phase the audio path applies -- the channel estimate deals with phase
    afterwards, but acquisition has to find the block first.
    """
    if len(captured) < len(reference) * 2:
        return None, 0.0
    corr = np.abs(correlate(hilbert(captured.astype(np.float64)),
                            hilbert(reference), mode="valid"))
    peak = int(np.argmax(corr))
    median = float(np.median(corr))
    confidence = float(corr[peak] / median) if median > 0 else 0.0
    return peak, confidence


def extract(captured, start, symbols):
    """(symbols, carriers) complex, taking each symbol's body after its CP."""
    stride = NFFT_RX + CP_RX
    need = start + symbols * stride
    if need > len(captured):
        return None
    out = np.empty((symbols, N_CARRIERS), dtype=complex)
    for i in range(symbols):
        base = start + i * stride + CP_RX
        body = captured[base:base + NFFT_RX]
        out[i] = np.fft.rfft(body, NFFT_RX)[CARRIER_BINS]
    return out


def fit_channel(observed, reference):
    """Least-squares per-carrier complex gain, and the residual EVM it leaves.

    One gain per carrier for the whole burst: the FM path is static and the
    clocks differ by ~3.7 ppm, so there is nothing here to track.
    """
    channel = (np.sum(observed * np.conj(reference), axis=0)
               / np.sum(np.abs(reference) ** 2, axis=0))
    modelled = channel * reference
    residual = float(np.mean(np.abs(observed - modelled) ** 2))
    signal = float(np.mean(np.abs(modelled) ** 2))
    return channel, (residual / signal if signal > 0 else np.inf)


def refine_start(captured, coarse, burst):
    """Best symbol-window start near the correlation peak, by EVM.

    The correlation peak sits LATE by however long the path's impulse
    response is -- the receive decimator's anti-alias FIR alone is ~33
    samples at 12 kHz, and the radios' audio filters add their own. That
    delay is part of the channel and gets absorbed as a per-carrier phase
    ramp, so the window must NOT be advanced to compensate for it: pushing
    the window forward by the group delay runs its tail into the next
    symbol and manufactures ISI. (It did exactly that here, and cost a
    debugging session -- a noiseless offline loopback still read BER 0.55.)

    Rather than assume any particular delay, score candidate starts against
    the known estimate symbols and keep the best. That also absorbs whatever
    the radios contribute, which we cannot know in advance.
    """
    best = None
    reference = burst["estimate_values"]
    for offset in range(-SEARCH_BACK, SEARCH_FORWARD + 1):
        start = coarse + offset
        if start < 0:
            continue
        observed = extract(captured, start + SYNC_SYMBOLS * (NFFT_RX + CP_RX),
                           ESTIMATE_SYMBOLS)
        if observed is None:
            continue
        _, evm = fit_channel(observed, reference)
        if best is None or evm < best[1]:
            best = (start, evm)
    return best


def analyse(captured, burst):
    """Per-carrier SNR and BER for one received burst."""
    coarse, confidence = acquire(captured, sync_reference(burst["sync_values"]))
    if coarse is None:
        return {"acquired": False, "reason": "capture too short"}

    refined = refine_start(captured, coarse, burst)
    if refined is None:
        return {"acquired": False, "reason": "burst ran past the capture",
                "confidence": confidence}
    start, timing_evm = refined

    after_sync = start + SYNC_SYMBOLS * (NFFT_RX + CP_RX)
    block = extract(captured, after_sync, ESTIMATE_SYMBOLS + PAYLOAD_SYMBOLS)
    if block is None:
        return {"acquired": False, "reason": "burst ran past the capture",
                "confidence": confidence}

    observed_est = block[:ESTIMATE_SYMBOLS]
    observed_payload = block[ESTIMATE_SYMBOLS:]

    channel, _ = fit_channel(observed_est, burst["estimate_values"])
    safe = np.where(np.abs(channel) > 1e-12, channel, 1e-12)

    equalised = observed_payload / safe
    reference = burst["payload_values"]
    error = equalised - reference
    error_power = np.mean(np.abs(error) ** 2, axis=0)
    snr_db = 10 * np.log10(1.0 / np.maximum(error_power, 1e-12))

    # Is the 'static channel' assumption actually holding? Three estimators,
    # each allowed to track more than the last, separate a fixed distortion
    # floor from a channel that moves during the burst:
    #   static  -- one gain per carrier for the whole burst (what we ship)
    #   cpe     -- plus one complex scalar per SYMBOL, i.e. common phase/gain
    #              error. A handful of pilot carriers can estimate this, so
    #              any gain here is cheaply available.
    #   genie   -- per symbol AND per carrier, using known data. Not
    #              achievable; it is the floor that no equaliser can beat, so
    #              it says how much of the loss is plain distortion/noise.
    # These use known payload, which a receiver would not have -- they are
    # diagnostics for choosing the pilot scheme, not a decoder.
    common = (np.sum(equalised * np.conj(reference), axis=1)
              / np.sum(np.abs(reference) ** 2, axis=1))
    eq_cpe = equalised / np.where(np.abs(common) > 1e-12, common, 1e-12)[:, None]
    snr_cpe_db = 10 * np.log10(
        1.0 / np.maximum(np.mean(np.abs(eq_cpe - reference) ** 2, axis=0), 1e-12))

    per_symbol_evm_db = 10 * np.log10(
        np.maximum(np.mean(np.abs(error) ** 2, axis=1), 1e-12))

    hard = np.empty((PAYLOAD_SYMBOLS, N_CARRIERS, 2), dtype=np.uint8)
    hard[..., 0] = (equalised.real < 0).astype(np.uint8)
    hard[..., 1] = (equalised.imag < 0).astype(np.uint8)
    sent = burst["payload_bits"].reshape(PAYLOAD_SYMBOLS, N_CARRIERS, 2)
    errors_per_carrier = np.sum(hard != sent, axis=(0, 2))
    bits_per_carrier = PAYLOAD_SYMBOLS * 2

    return {
        "acquired": True,
        "confidence": confidence,
        "start": start,
        "timing_offset": start - coarse,
        "timing_evm": timing_evm,
        "channel_db": 20 * np.log10(np.abs(channel) + 1e-12),
        "snr_db": snr_db,
        "snr_cpe_db": snr_cpe_db,
        "per_symbol_evm_db": per_symbol_evm_db,
        "common_mag": np.abs(common),
        "common_phase": np.unwrap(np.angle(common)),
        "errors_per_carrier": errors_per_carrier,
        "bits_per_carrier": bits_per_carrier,
        "ber": float(np.sum(errors_per_carrier)
                     / (bits_per_carrier * N_CARRIERS)),
    }


def run_once(tx, rx, tx_name, rx_name, burst, label):
    rx.snapshot_rx()
    tx.send(burst["audio"])
    time.sleep(CAPTURE_TAIL)
    captured = rx.snapshot_rx()

    result = analyse(captured, burst)
    head = f"  [{tx_name}->{rx_name} {label}]"
    if not result["acquired"]:
        print(f"{head} NOT ACQUIRED ({result['reason']}), "
              f"captured {len(captured)} samples")
        return result

    snr = result["snr_db"]
    cpe = result["snr_cpe_db"]
    mag = result["common_mag"]
    phase = result["common_phase"]
    print(f"{head} acquired (conf {result['confidence']:.1f}), "
          f"BER={result['ber']:.2e}, "
          f"per-carrier SNR min/median/max = "
          f"{snr.min():.1f}/{np.median(snr):.1f}/{snr.max():.1f} dB")
    print(f"       static->cpe median SNR {np.median(snr):.1f} -> {np.median(cpe):.1f} dB "
          f"(+{np.median(cpe) - np.median(snr):.1f}); "
          f"per-symbol gain {mag.min():.3f}..{mag.max():.3f}, "
          f"phase drift {np.degrees(phase[-1] - phase[0]):+.1f} deg over the burst")
    return result


def save_csv(path, rows):
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["direction", "clip_db", "freq_hz", "channel_db",
                         "snr_db", "bit_errors", "bits"])
        writer.writerows(rows)


def parse_clips(text):
    clips = []
    for part in text.split(","):
        part = part.strip()
        clips.append(None if part.lower() == "none" else float(part))
    return clips


def main():
    global CP_RX, CP_TX
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clip", default="none,9,6",
                    help="comma-separated clip thresholds in dB above RMS, "
                         "or 'none' (default: %(default)s)")
    ap.add_argument("--direction", choices=["both", "ic705->ht", "ht->ic705"],
                    default="both")
    ap.add_argument("--cp", type=int, default=DEFAULT_CP_RX,
                    help="cyclic prefix in samples at 12 kHz; must exceed the "
                         "~32-sample receive anti-alias FIR (default: %(default)s)")
    ap.add_argument("--peak", default=None,
                    help="comma-separated peak amplitudes to sweep with no "
                         "clipping, e.g. 0.9,0.6,0.4,0.25 -- the "
                         "intermodulation test; overrides --clip")
    ap.add_argument("--out", default="scratch_ofdm_fm_burst.csv")
    args = ap.parse_args()

    CP_RX = args.cp
    CP_TX = CP_RX * DECIMATION
    if CP_RX < MIN_USEFUL_CP_RX:
        print(f"WARNING: CP {CP_RX} is at or under the receive decimator's own "
              f"~32-sample impulse response -- expect ISI regardless of the air path")

    # Each setting is one (clip, peak) pair, labelled for the output. Sweeping
    # peak with no clipping is the IMD test: if backing the drive off RAISES
    # per-carrier SNR, the path is intermodulation-limited rather than
    # noise-limited, and the operating point belongs at the peak of that curve.
    if args.peak:
        settings = [(None, p, f"peak{p:g}") for p in
                    [float(x) for x in args.peak.split(",")]]
    else:
        settings = [(c, None, "none" if c is None else f"{c:g}dB")
                    for c in parse_clips(args.clip)]
    bursts = {label: build_burst(clip, peak) for clip, peak, label in settings}

    payload_bits_total = PAYLOAD_SYMBOLS * N_CARRIERS * 2
    symbol_seconds = (NFFT_RX + CP_RX) / RX_SAMPLE_RATE
    print(f"{N_CARRIERS} carriers, bins {BIN_LO}..{BIN_HI} "
          f"({BIN_LO * CARRIER_SPACING_HZ:.0f}-{BIN_HI * CARRIER_SPACING_HZ:.0f} Hz), "
          f"CP {CP_RX}/{NFFT_RX} at 12 kHz")
    print(f"symbol {symbol_seconds * 1000:.1f} ms -> {1 / symbol_seconds:.1f} sym/s; "
          f"QPSK raw rate {N_CARRIERS * 2 / symbol_seconds:.0f} bps; "
          f"{payload_bits_total} payload bits/burst")
    for label, burst in bursts.items():
        audio = burst["audio"].astype(np.float64)
        print(f"  {label}: PAPR {burst['raw_papr_db']:.1f} -> "
              f"{burst['sent_papr_db']:.1f} dB, peak {np.max(np.abs(audio)):.3f}, "
              f"rms {np.sqrt(np.mean(audio ** 2)):.4f}, "
              f"burst {len(audio) / SAMPLE_RATE:.2f}s")

    do_ab = args.direction in ("both", "ic705->ht")
    do_ba = args.direction in ("both", "ht->ic705")
    rows = []

    with bench.radio_pair() as (t_ic705, t_ht):
        time.sleep(SETTLE_AFTER_PTT)
        for _clip, _peak, label in settings:
            burst = bursts[label]
            legs = []
            if do_ab:
                legs.append((t_ic705, t_ht, "ic705", "ht"))
            if do_ba:
                legs.append((t_ht, t_ic705, "ht", "ic705"))
            for tx, rx, tx_name, rx_name in legs:
                result = run_once(tx, rx, tx_name, rx_name, burst, label)
                if result.get("acquired"):
                    for i, b in enumerate(CARRIER_BINS):
                        rows.append([f"{tx_name}->{rx_name}", label,
                                     b * CARRIER_SPACING_HZ,
                                     f"{result['channel_db'][i]:.2f}",
                                     f"{result['snr_db'][i]:.2f}",
                                     int(result["errors_per_carrier"][i]),
                                     result["bits_per_carrier"]])
                time.sleep(0.5)

    if rows:
        save_csv(args.out, rows)
        print(f"\nWrote {args.out}")
        return 0
    print("\nNo burst was acquired -- nothing written")
    return 1


if __name__ == "__main__":
    sys.exit(main())
