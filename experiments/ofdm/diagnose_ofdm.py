"""Why did that OFDM frame fail? Per-subcarrier, per-symbol post-mortem.

sweep_ofdm.py answers "does it decode", which is the question that picks the
mode. This answers the follow-up. The payload is known here, so every
transmitted constellation point is known, and the received grid can be laid
against it point for point: n_symbols x n_carriers complex error vectors,
with a frequency axis and a time axis and -- new in this mode -- an amplitude
axis.

Four impairments produce four different shapes in that grid, and telling them
apart is the whole job:

  - **A notch, or band-edge rolloff.** Errors concentrate on particular
    subcarriers, and |H| is depressed there. This is the no-FEC killer and
    the reason probe_channel.py exists: with CRC-only framing one bad carrier
    produces errors in every symbol of the frame and fails every frame
    regardless of how good the other thirty-four are. Read the per-subcarrier
    EVM column and the |H| column next to it.

  - **Sample-clock drift.** Errors ramp with symbol index, because the
    channel estimate is taken from training symbols at the front of the frame
    and never refreshed. tau samples of accumulated drift rotate subcarrier k
    by 2*pi*k*tau/n_fft -- linear in k and linear in elapsed time -- so this
    tool does not merely note the ramp, it fits the closed form in ofdm.py's
    docstring and reports the offset in ppm. See "the trap" below.

  - **The radio's limiter clipping peaks, i.e. the drive is too hot.** Errors
    correlate with the symbol's own instantaneous amplitude. OFDM is the first
    non-constant-envelope mode in this repo, so this failure has no precedent
    here and nothing else in the repo will recognise it. The predictor is the
    peak of the *ideal, unclipped* symbol waveform rebuilt from the known
    payload -- not anything measured off the capture, for the reason in
    amplitude_axis().

  - **Plain noise.** Flat in all three axes.


The trap, and why the drift fit runs first
------------------------------------------

scripts/bench.py's docstring records the mistake this class of tool invites:
its near-miss diagnostic compared a measured span against the wrong reference
and reported "~1.2x expected" for frames that had in fact been read perfectly,
and that number was taken as evidence of a false sync lock that never
happened. A diagnostic that confidently accuses the wrong thing is worse than
no diagnostic, because it sends someone off to fix something that is not
broken.

The same mistake is available here, and it is not subtle. **Clock drift puts
its errors on the highest subcarriers**, because the rotation it causes is
proportional to k. A tool that reads the frequency axis first sees errors
piled up at the top of the band, announces "band-edge rolloff", and sends the
reader to probe_channel.py and a narrower band -- which will not help, and
which will make the frame shorter, which will *partly* help, which is the
worst possible outcome because it half-confirms the wrong story.

So the two are separated by mechanism rather than by marginal:

  - Drift's residual is a *phase* rotation, systematic in k and growing
    linearly with elapsed time. It has one free parameter across the whole
    grid, and removing it is a one-line derotation.
  - Rolloff's residual is amplified noise on specific carriers, with random
    phase and no time dependence.

drift_fit() estimates the one parameter, reports it as ppm alongside the
tolerance ofdm.py's closed form predicts, and derotates. Every other axis is
then measured on the derotated residual, and the report prints both so the
reader can see how much the fit took. If derotation removes most of the error
energy, it was the clock; if the top-carrier pile-up survives derotation, it
is the band edge. Neither number alone says which.


Why EVM, and not the error counts
---------------------------------

experiments/mfsk/diagnose_mfsk.py could build its whole case on a confusion
matrix, because 8 wrong symbols out of 1596 in a 4x4 matrix, all on one tone
pair, is already conclusive. That does not transfer.

A near-miss OFDM frame is a frame whose CRC failed, and CRC-only framing fails
on one bad bit. The most interesting near miss -- the one just past the edge,
which is exactly the one worth diagnosing -- may have **one** wrong symbol in
3605. One error has no distribution. Split it across 35 subcarriers and 10
deciles and every test in this file has zero power.

The error *vector* does not have that problem. Every one of the 3605 received
points carries a continuous measurement of how far off it was, whether or not
it crossed a decision boundary, so the grid holds 3605 measurements rather
than one. Per-carrier EVM averaged over 103 symbols has a standard deviation
of 4.343/sqrt(103) = 0.43 dB, which makes a 3 dB excess on one carrier a
seven-sigma event on a frame with no symbol errors at all.

So EVM is the evidence here and the error counts are corroboration. The
report prints both, and states the expected scatter next to each so a reader
can see which differences are real. When there are enough errors to have a
distribution, the counts and the EVM should agree, and it is worth noticing
when they do not.


Wrong turns, kept because they cost time
----------------------------------------

  - **A notch in a noiseless channel produces no errors at all.** The first
    synthetic notch this tool was tested against was 25 dB deep and the tool
    reported a perfect frame -- correctly. The equaliser divides by H, and a
    channel that only attenuates is removed exactly; what a notch does is
    amplify whatever noise sits under it by 1/|H|. So the synthetic notch
    injects noise as well, and the reason is written into the injector rather
    than left as a magic argument. This also says something about real
    captures: a subcarrier with a low |H| and a normal EVM is not a problem,
    and the tool must not flag it. Only |H| *and* EVM together mean anything.

  - **Measuring instantaneous amplitude off the capture does not work.**
    The obvious predictor for a limiter is the received peak level per symbol,
    and it is close to useless, because a limiter's defining behaviour is that
    it removes the peaks it acts on -- the symbols that were clipped are
    exactly the ones whose received peak now reads at the limit. Both numbers
    are printed side by side in the report and the received one is visibly
    the weaker. The predictor has to be the *ideal* symbol waveform rebuilt
    from the known payload. The received peak is still printed, as a
    compression curve, because the slope of received peak against ideal peak
    is a direct read on limiting that needs no errors at all -- but it is
    corroboration, not the test.

  - **The amplitude axis has to run after derotation.** Not because drift and
    PAPR are correlated -- whitening makes sure they are not -- but because
    drift raises the EVM floor across the second half of the frame, which
    dilutes any correlation into it. See verdict().


What it cannot tell apart
-------------------------

Stated here rather than buried, because a confident wrong answer is the
failure mode this file exists to avoid:

  - **Noise and clipping, when the clipping is mild.** Both are flat in
    frequency and flat in time. The only thing separating them is the
    amplitude correlation, and that correlation is weak until the limiter is
    biting hard. When it is below the flag the tool says "noise or mild
    limiting" and means it.

  - **A notch and a dead subcarrier.** If |H| at some carrier is at the noise
    floor, the tool cannot say whether the chain notched it or whether
    something else -- an interferer, a spur -- is sitting on it. Both need a
    band change; neither is distinguishable from one frame.

  - **Anything at all, on a frame with fewer than ~10 symbol errors, from the
    error counts alone.** The tool falls back on EVM and says so. If EVM is
    also flat and low, the honest answer is "this frame was marginal
    everywhere" and the tool gives it.

  - **Dispersion longer than the cyclic prefix.** Not in the list of four,
    and not separable by this tool: ISI that overruns the prefix appears as a
    frequency-selective error that looks exactly like a notch, because to
    first order it is one. probe_channel.py's delay-spread measurement is what
    answers that, and the report says so when it flags carriers.

Run: python experiments/ofdm/diagnose_ofdm.py --self-test
     python experiments/ofdm/diagnose_ofdm.py --inject drift --ppm 25
     python experiments/ofdm/diagnose_ofdm.py --inject notch --notch-hz 1500
     python experiments/ofdm/diagnose_ofdm.py --inject clip noise --limiter-db 4
     python experiments/ofdm/diagnose_ofdm.py --capture nearmiss_...npy --payload-file p.bin
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from scipy.signal import hilbert

import ofdm

# The candidate this tool defaults to when generating its own frame. The
# middle of the QPSK candidate space -- 35 subcarriers, 103 data symbols --
# and the first candidate sweep_ofdm.py's ladder reaches at a prefix worth
# having.
DEFAULT_PROFILE = "ofdm4_50hz_cp8"

# Where whale/link.py drops the audio behind a near-miss decode when
# WHALE_CAPTURE_DIR is set, and where the off-air corpus already lives.
CAPTURE_DIR = Path(__file__).resolve().parents[2] / "scratch_captures_600ack"

# A per-carrier EVM this far above the band's median is called out. Chosen
# against the measurement's own scatter rather than picked: per-carrier EVM
# is a mean of n_symbols squared magnitudes, so its standard deviation is
# 4.343/sqrt(n_symbols) dB -- 0.43 dB at the default profile. 3 dB is seven
# sigma there, and the floor is raised to 5 sigma on short frames so a
# 20-symbol capture does not produce a page of flags.
CARRIER_FLAG_DB = 3.0
CARRIER_FLAG_SIGMA = 5.0

# Below this, a fitted clock offset is not distinguishable from the fit
# picking up noise. --self-test measures the estimator's own floor on a clean
# frame and on a noisy one, which is where this number comes from.
DRIFT_FLOOR_PPM = 1.5

# Pearson r between per-symbol ideal peak and per-symbol EVM above which the
# limiter is called. Under the null the sample r has standard deviation
# 1/sqrt(n_symbols) = 0.099 at the default profile, so 0.30 is three sigma.
CLIP_R_THRESHOLD = 0.30


# -- getting the symbols back out ------------------------------------------


def locate(audio, profile):
    """(preamble start, confidence, cleared_threshold), or None.

    demodulate()'s own two-stage sync, reproduced rather than called because
    demodulate() returns a payload and this tool wants everything it threw
    away. Earliest above threshold wins, as there -- the RX buffer routinely
    holds a garbled self-echo alongside the real frame. When nothing clears
    the threshold the best candidate is returned anyway with the flag false,
    because a diagnostic that refuses to look at a weak lock is refusing
    exactly the case it was written for.
    """
    audio = np.asarray(audio, dtype=np.float64)
    proposals, _ = ofdm._propose(audio, profile)
    if not proposals:
        return None
    analytic = hilbert(audio)
    scored = [ofdm._refine(analytic, profile, p) for p in proposals]
    for start, confidence in scored:
        if confidence >= profile.confidence_threshold:
            return start, confidence, True
    start, confidence = max(scored, key=lambda s: s[1])
    return start, confidence, False


def reference_symbols(payload, profile):
    """The transmitted constellation grid, (n_symbols, n_carriers)."""
    return ofdm.data_symbol_values(payload, profile)


def decision_reference(equalised, profile):
    """Fallback reference when the payload is not known: each point's own
    nearest constellation point.

    Honest only about *scatter*. It cannot count symbol errors -- every point
    is by construction nearest to what it was decided as -- and it
    under-reports EVM, because any point that crossed a boundary is measured
    against the wrong reference and so looks closer than it is. Both effects
    grow with the error rate, which is to say the measurement degrades exactly
    where it matters. It is here because a real capture whose payload was lost
    is still worth looking at, not because it is equivalent.
    """
    points = ofdm.constellation(profile.bits_per_carrier)
    return points[ofdm._demap(equalised, profile.bits_per_carrier)].reshape(equalised.shape)


def evm_db(err):
    """Error vector magnitude in dB. The constellations are unit average
    power (see ofdm.constellation), so no reference normalisation is needed
    and -20 dB means the error is a tenth of the signal in amplitude."""
    return float(10 * np.log10(max(float(np.mean(np.abs(err) ** 2)), 1e-30)))


# -- the time axis: sample-clock drift -------------------------------------


def drift_fit(equalised, sent, profile):
    """Fit the one-parameter clock-offset model to the residual phase.

    The model is ofdm.py's: tau samples of accumulated drift rotate subcarrier
    k by 2*pi*k*tau/n_fft, with tau growing linearly from the training
    symbols, which are the only place the channel estimate is anchored. So the
    residual phase over the whole grid is

        phi(symbol, k) = 2*pi * k * eps * t(symbol) / n_fft

    for a single fractional offset eps, where t is elapsed samples measured
    from the centroid of the training symbols -- the centroid and not the
    first, because _equalise averages them.

    The per-symbol slope is taken from the product of adjacent carriers'
    residuals rather than by unwrapping and fitting, which matters: the
    residual at the top carrier reaches most of a radian before the frame
    dies, and passes pi on a frame that has already died, but the step from
    one carrier to the next is 2*pi*eps*t/n_fft -- 0.016 rad at 20 ppm on the
    default profile. Adjacent differences never wrap, so the estimate stays
    linear well past the point where the frame stops decoding. That is
    deliberate: the interesting question about a dead frame is how far past
    the edge it was.

    Returns the estimate, the per-symbol slopes, the derotated grid, and the
    per-symbol common phase -- which the model says is zero (the rotation
    vanishes at k=0) and which is printed because a ramp there would mean a
    carrier frequency offset, something ofdm.py's docstring argues cannot
    happen on an FM link.
    """
    z = equalised * np.conj(sent)
    mag = np.abs(z)
    z = np.divide(z, np.maximum(mag, 1e-12), out=np.zeros_like(z), where=mag > 1e-12)

    # rad per subcarrier index, per symbol. profile.carriers is a contiguous
    # arange, so neighbouring columns are one index apart.
    slope = np.angle(np.sum(z[:, 1:] * np.conj(z[:, :-1]), axis=1))

    j = np.arange(equalised.shape[0])
    t = (profile.n_train + j - (profile.n_train - 1) / 2.0) * profile.symbol_samples
    denom = float(np.sum(t * t))
    eps = float(np.sum(slope * t) / denom) * profile.n_fft / (2 * np.pi) if denom else 0.0

    predicted = 2 * np.pi * eps * np.outer(t, profile.carriers) / profile.n_fft
    return {
        "ppm": eps * 1e6,
        "slope": slope,
        "t_seconds": t / ofdm.SAMPLE_RATE,
        "corrected": equalised * np.exp(-1j * predicted),
        "common_phase": np.angle(np.sum(z, axis=1)),
    }


def predicted_tolerance_ppm(profile, frame_seconds):
    """ofdm.py's closed form, restated so a fitted offset can be read against
    something. Not a threshold this tool applies -- it is the number that says
    whether a fitted 8 ppm is a curiosity or the cause of death."""
    return 1e6 / (2 ** (profile.bits_per_carrier + 1) * profile.tone_high
                  * max(frame_seconds, 1e-9))


# -- the amplitude axis: the limiter ---------------------------------------


def amplitude_axis(sent, profile, audio, first_symbol_start):
    """Per-symbol instantaneous amplitude, from both ends of the link.

    The first return is the peak of the symbol waveform rebuilt from the known
    payload, relative to its own RMS -- what the transmitter *asked* the chain
    to reproduce, before ofdm._clip_and_filter touched it and before any
    limiter in the radio did. This is the predictor for a clipping verdict,
    and the reason is that a limiter destroys the evidence of its own action:
    the symbols it flattened are precisely the ones whose received peak now
    sits at the limit, so a correlation taken against the received peak is
    measured against a variable the impairment has already erased.

    The second is measured off the capture anyway, and reported against the
    ideal, because the *slope* of that relationship is a read on limiting that
    needs no errors and no payload at all. A linear chain gives slope 1; a
    chain that limits gives less. It is corroboration and it is labelled as
    such in the report.
    """
    guard = int(ofdm._WINDOW_GUARD_FRACTION * profile.cp)
    step = profile.symbol_samples
    ideal_peak, rx_peak = [], []
    positions = ofdm.data_physical_indices(profile, len(sent))
    for i, row in enumerate(sent):
        wave = ofdm._symbol_audio(profile, profile.carriers, row)
        rms = np.sqrt(np.mean(wave ** 2)) or 1.0
        ideal_peak.append(np.max(np.abs(wave)) / rms)
        start = first_symbol_start + positions[i] * step - guard
        window = np.asarray(audio[start:start + profile.n_fft], dtype=np.float64)
        if len(window) < profile.n_fft:
            rx_peak.append(np.nan)
            continue
        w_rms = np.sqrt(np.mean(window ** 2)) or 1.0
        rx_peak.append(np.max(np.abs(window)) / w_rms)
    ideal = 20 * np.log10(np.maximum(np.asarray(ideal_peak, dtype=float), 1e-12))
    rx = 20 * np.log10(np.maximum(np.asarray(rx_peak, dtype=float), 1e-12))
    return ideal, rx


def _pearson(a, b):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if len(a) < 3 or np.std(a) < 1e-12 or np.std(b) < 1e-12:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _compression_slope(ideal_db, rx_db):
    """Least-squares slope of received peak against ideal peak, both in dB.
    1.0 is a linear chain; less is compression."""
    ok = np.isfinite(ideal_db) & np.isfinite(rx_db)
    if ok.sum() < 3 or np.std(ideal_db[ok]) < 1e-9:
        return float("nan")
    x = ideal_db[ok] - ideal_db[ok].mean()
    y = rx_db[ok] - rx_db[ok].mean()
    return float(np.sum(x * y) / np.sum(x * x))


# -- putting the axes together ---------------------------------------------


def analyse(audio, profile, payload=None, verbose=True):
    """Everything measurable about one captured frame. Returns a dict, or
    None with a printed reason if there is nothing to measure."""
    audio = np.asarray(audio, dtype=np.float64)
    located = locate(audio, profile)
    if located is None:
        if verbose:
            print("  no sync proposal at all -- the repetition metric never cleared "
                  f"{ofdm.REPETITION_THRESHOLD}. Nothing to line up.")
        return None
    start, confidence, cleared = located
    first = start + profile.n_fft + profile.cp

    if payload is not None:
        sent = reference_symbols(payload, profile)
        want = len(sent)
    else:
        available = (len(audio) - first) // profile.symbol_samples - profile.n_train
        want = max(0, min(ofdm.max_credible_data_symbols(profile),
                          ofdm.data_symbols_from_physical(profile, available)))
        sent = None

    equalised, channel, train_noise = ofdm._equalise(profile, audio, first, want)
    if equalised is None or not len(equalised):
        if verbose:
            print("  synced, but not one whole symbol is in the buffer past the "
                  "training symbols. The capture is truncated.")
        return None

    if sent is None:
        sent = decision_reference(equalised, profile)
    n = min(len(equalised), len(sent))
    equalised, sent = equalised[:n], sent[:n]

    drift = drift_fit(equalised, sent, profile)
    raw_residual = equalised - sent
    residual = drift["corrected"] - sent

    got = ofdm._demap(equalised, profile.bits_per_carrier).reshape(n, -1)
    want_values = ofdm._demap(sent, profile.bits_per_carrier).reshape(n, -1)
    wrong = got != want_values
    corrected_wrong = (ofdm._demap(drift["corrected"], profile.bits_per_carrier)
                       .reshape(n, -1) != want_values)

    ideal_peak_db, rx_peak_db = amplitude_axis(sent, profile, audio, first)
    per_symbol_power = np.mean(np.abs(residual) ** 2, axis=1)
    per_symbol_power_raw = np.mean(np.abs(raw_residual) ** 2, axis=1)

    return {
        "profile": profile,
        "payload_known": payload is not None,
        "confidence": confidence,
        "cleared_threshold": cleared,
        "start": start,
        "n_symbols": n,
        "n_carriers": equalised.shape[1],
        "equalised": equalised,
        "sent": sent,
        "channel": channel,
        "train_noise": train_noise,
        "raw_residual": raw_residual,
        "residual": residual,
        "wrong": wrong,
        "corrected_wrong": corrected_wrong,
        "drift": drift,
        "ideal_peak_db": ideal_peak_db,
        "rx_peak_db": rx_peak_db,
        "per_symbol_power": per_symbol_power,
        "per_symbol_power_raw": per_symbol_power_raw,
        "clip_r": _pearson(ideal_peak_db, 10 * np.log10(np.maximum(per_symbol_power, 1e-30))),
        "clip_r_received": _pearson(rx_peak_db,
                                    10 * np.log10(np.maximum(per_symbol_power, 1e-30))),
        "compression": _compression_slope(ideal_peak_db, rx_peak_db),
        "frame_seconds": ((1 + profile.n_train + n) * profile.symbol_samples
                          / ofdm.SAMPLE_RATE),
    }


def _carrier_evm(a, key="residual"):
    return 10 * np.log10(np.maximum(np.mean(np.abs(a[key]) ** 2, axis=0), 1e-30))


def _carrier_flag(a):
    """Which carriers are worse than the band's own scatter can explain, and
    the level that decision was made at."""
    evm = _carrier_evm(a)
    sigma = 4.343 / np.sqrt(max(a["n_symbols"], 1))
    level = np.median(evm) + max(CARRIER_FLAG_DB, CARRIER_FLAG_SIGMA * sigma)
    return np.flatnonzero(evm > level), level, sigma, evm


def verdict(a):
    """A stated answer, plus the evidence behind it and what it cannot rule
    out. Returns (headline, [lines]).

    Order matters and it is the one thing in this file worth arguing about.
    The drift test runs first because drift *also* piles its errors onto the
    high subcarriers -- see the module docstring -- so reading the frequency
    axis first would misattribute it. Every subsequent axis is measured on the
    derotated residual, so "the top carriers are still bad after the clock is
    taken out" is a statement about the band and not about the clock.

    The limiter test runs before the noise fallback for the opposite reason:
    clipping is flat in both the frequency and the time axis, so a tool that
    only looks at those two calls it noise every time.
    """
    profile = a["profile"]
    n, n_car = a["n_symbols"], a["n_carriers"]
    lines = []

    total = int(a["wrong"].sum())
    after = int(a["corrected_wrong"].sum())
    raw_evm = evm_db(a["raw_residual"])
    res_evm = evm_db(a["residual"])
    flagged, flag_level, sigma_carrier, carrier_evm = _carrier_flag(a)
    excess = carrier_evm - np.median(carrier_evm)

    ppm = a["drift"]["ppm"]
    tolerance = predicted_tolerance_ppm(profile, a["frame_seconds"])
    drift_gain = raw_evm - res_evm
    clip_r = a["clip_r"]

    if abs(ppm) > DRIFT_FLOOR_PPM and drift_gain > 1.0 and abs(ppm) > 0.25 * tolerance:
        headline = (f"SAMPLE-CLOCK DRIFT: {ppm:+.1f} ppm, against a tolerance of "
                    f"{tolerance:.1f} ppm for this profile at this frame length")
        lines.append(f"  the fitted rotation accounts for {drift_gain:.1f} dB of error "
                     f"vector ({raw_evm:.1f} -> {res_evm:.1f} dB EVM)")
        if a["payload_known"]:
            lines.append(f"  symbol errors {total} -> {after} once it is removed")
        lines.append("  the errors sit on the top carriers and this is NOT a band "
                     "problem: the rotation is proportional to k. Derotating removes "
                     "them; narrowing the band would not.")
        lines.append("  fix, if the bench ever needs one, is ofdm.py's second training "
                     "symbol at the end of the frame -- two anchors bound a linear drift "
                     "and it costs no airtime.")
        if len(flagged):
            lines.append(f"  {len(flagged)} carrier(s) are still {excess[flagged].max():.1f} dB "
                         "hot after derotation, so there may be a band problem under it "
                         "as well")
    elif len(flagged):
        freqs = profile.carriers[flagged] * profile.spacing
        mag = 20 * np.log10(np.maximum(np.abs(a["channel"]), 1e-12))
        mag -= mag.max()
        headline = (f"FREQUENCY-SELECTIVE: {len(flagged)} of {n_car} subcarriers carry the "
                    "error -- " + ", ".join(f"{f:.0f}Hz" for f in freqs[:6])
                    + ("..." if len(freqs) > 6 else ""))
        lines.append(f"  their EVM is up to {excess[flagged].max():.1f} dB above the band "
                     f"median, against {sigma_carrier:.2f} dB of expected scatter")
        lines.append(f"  |H| there is {mag[flagged].min():.1f} to {mag[flagged].max():.1f} dB "
                     "below the band peak")
        if total:
            share = int(a["wrong"][:, flagged].sum()) / total
            lines.append(f"  those carriers hold {share * 100:.0f}% of the symbol errors "
                         f"on {len(flagged) / n_car * 100:.0f}% of the band")
        lines.append("  with no FEC this fails every frame regardless of the other "
                     f"{n_car - len(flagged)} carriers. Re-measure the band "
                     "(probe_channel.py --out) and pass it to sweep_ofdm.py --band.")
        lines.append("  CANNOT distinguish a notch in the chain from dispersion longer "
                     "than the prefix, nor from something parked on those carriers. "
                     "probe_channel.py's delay spread separates the first two.")
    elif clip_r > CLIP_R_THRESHOLD:
        headline = ("LIMITING / DRIVE TOO HOT: per-symbol EVM tracks the symbol's own "
                    f"peak amplitude at r = {clip_r:+.2f}")
        lines.append(f"  scatter under no relationship is {1 / np.sqrt(max(n, 2)):.2f}, so "
                     f"this is {abs(clip_r) * np.sqrt(max(n, 2)):.1f} sigma")
        hi = np.argsort(a["ideal_peak_db"])[-max(1, n // 10):]
        lo = np.argsort(a["ideal_peak_db"])[:max(1, n // 10)]
        lines.append(f"  the peakiest tenth of symbols runs "
                     f"{evm_db(a['residual'][hi]) - evm_db(a['residual'][lo]):.1f} dB worse "
                     "EVM than the flattest tenth")
        if np.isfinite(a["compression"]):
            lines.append(f"  received peak follows ideal peak at slope "
                         f"{a['compression']:.2f} (1.00 is a linear chain)")
        lines.append("  turn the drive down, or lower profile.papr_db so the clipping "
                     "happens in software where the splatter is filtered off "
                     "(sweep_ofdm.py --drive A/Bs exactly this).")
    else:
        headline = (f"NOISE-LIKE, or limiting too mild to see: flat in frequency, flat in "
                    f"time, EVM {res_evm:.1f} dB")
        lines.append(f"  implied SNR {-res_evm:.1f} dB on the equalised constellation")
        lines.append(f"  worst carrier {excess.max():.1f} dB over the median, flag at "
                     f"{flag_level - np.median(carrier_evm):.1f} dB")
        lines.append(f"  amplitude correlation r = {clip_r:+.2f}, flag at "
                     f"{CLIP_R_THRESHOLD:.2f}")
        lines.append(f"  fitted clock offset {ppm:+.2f} ppm against a {tolerance:.1f} ppm "
                     "tolerance -- not the cause")
        lines.append("  CANNOT separate plain noise from mild limiting. Both are flat in "
                     "both axes; the amplitude correlation is the only thing that "
                     "distinguishes them and it is weak until the limiter bites hard.")

    if a["payload_known"] and total < 10:
        lines.append(f"  note: {total} symbol error(s) in {n * n_car}. The error counts "
                     "have no power at that size; this verdict rests on EVM, which uses "
                     "every point whether or not it crossed a decision boundary.")
    if not a["payload_known"]:
        lines.append("  note: no payload given, so the reference is the receiver's own "
                     "decisions. Symbol errors are unmeasurable and EVM is understated. "
                     "Structure across the axes is still real.")
    if not a["cleared_threshold"]:
        lines.append(f"  note: sync confidence {a['confidence']:.3f} is below the profile "
                     f"threshold {profile.confidence_threshold:.2f}. The frame may not be "
                     "where this thinks it is, in which case every number above is "
                     "measuring the wrong samples.")
    return headline, lines


# -- report ----------------------------------------------------------------


def report(a, carrier_rows=64):
    profile = a["profile"]
    n, n_car = a["n_symbols"], a["n_carriers"]
    freqs = profile.carriers * profile.spacing
    total = int(a["wrong"].sum())

    print(f"\n  {ofdm.describe(profile)}")
    print(f"  sync at sample {a['start']}, confidence {a['confidence']:.3f}"
          f"{'' if a['cleared_threshold'] else '   <-- BELOW THRESHOLD'}")
    print(f"  compared {n} symbols x {n_car} carriers = {n * n_car} points"
          f"{'' if a['payload_known'] else '   (decision-directed -- no payload given)'}")
    print(f"  symbol errors {total} ({total / max(n * n_car, 1) * 100:.3f}%)   "
          f"EVM {evm_db(a['raw_residual']):.1f} dB   "
          f"after derotation {evm_db(a['residual']):.1f} dB")

    mag = 20 * np.log10(np.maximum(np.abs(a["channel"]), 1e-12))
    mag -= mag.max()
    phase = np.unwrap(np.angle(a["channel"]))
    evm = _carrier_evm(a)
    evm_raw = _carrier_evm(a, "raw_residual")
    carrier_err = a["wrong"].sum(axis=0)
    flagged, level, sigma, _ = _carrier_flag(a)
    base = np.median(evm)

    print(f"\n  -- per subcarrier (expected EVM scatter on a flat channel +/-{sigma:.2f} dB; "
          f"flagged above {level:.1f} dB) --")
    print("        Hz    |H| dB   arg H    EVM  (raw)   errors")
    step = max(1, -(-n_car // carrier_rows))
    for i in range(0, n_car, step):
        bar = "#" * int(max(0, min(30, evm[i] - base + 6)))
        flag = "<-- " if i in set(flagged.tolist()) else "    "
        print(f"    {freqs[i]:6.0f}   {mag[i]:6.1f}  {phase[i]:+6.2f}  {evm[i]:6.1f} "
              f"{evm_raw[i]:6.1f}   {carrier_err[i]:6d}   {flag}{bar}")

    print("\n  -- per symbol index, by decile of frame --")
    sym = 10 * np.log10(np.maximum(a["per_symbol_power"], 1e-30))
    sym_raw = 10 * np.log10(np.maximum(a["per_symbol_power_raw"], 1e-30))
    deciles = np.array_split(np.arange(n), 10)
    print("      decile     1     2     3     4     5     6     7     8     9    10")
    print("      EVM raw " + "".join(f"{sym_raw[d].mean():6.1f}" for d in deciles))
    print("      EVM der " + "".join(f"{sym[d].mean():6.1f}" for d in deciles))
    print("      errors  " + "".join(f"{int(a['wrong'][d].sum()):6d}" for d in deciles))
    if total:
        bad = a["wrong"].any(axis=1)
        first, last = int(np.argmax(bad)), int(n - 1 - np.argmax(bad[::-1]))
        print(f"      first bad symbol {first} ({first / n * 100:.0f}% in), "
              f"last {last} ({last / n * 100:.0f}% in)")

    d = a["drift"]
    tolerance = predicted_tolerance_ppm(profile, a["frame_seconds"])
    common = np.rad2deg(d["common_phase"])
    print("\n  -- sample-clock fit (ofdm.py's closed form) --")
    print(f"      fitted offset       {d['ppm']:+8.2f} ppm")
    print(f"      tolerance here      {tolerance:8.2f} ppm  "
          f"({profile.bits_per_carrier} bits, {profile.tone_high:.0f} Hz top carrier, "
          f"{a['frame_seconds']:.2f}s frame)")
    print(f"      EVM it accounts for {evm_db(a['raw_residual']) - evm_db(a['residual']):8.2f} dB")
    print(f"      symbol errors       {total} -> {int(a['corrected_wrong'].sum())}")
    print(f"      common phase        {common[-1] - common[0]:+8.2f} deg end-to-end "
          "(model says 0; a ramp here is a carrier offset, which FM should not produce)")

    order = np.argsort(a["ideal_peak_db"])
    lo, hi = order[:max(1, n // 10)], order[-max(1, n // 10):]
    print("\n  -- amplitude axis (the limiter) --")
    print(f"      ideal per-symbol peak spans {a['ideal_peak_db'].min():.1f} to "
          f"{a['ideal_peak_db'].max():.1f} dB over RMS")
    print(f"      EVM, peakiest 10% of symbols   {evm_db(a['residual'][hi]):6.1f} dB")
    print(f"      EVM, flattest 10% of symbols   {evm_db(a['residual'][lo]):6.1f} dB")
    print(f"      r(ideal peak, EVM)             {a['clip_r']:+6.2f}   "
          f"[scatter under no relationship {1 / np.sqrt(max(n, 2)):.2f}]")
    print(f"      r(received peak, EVM)          {a['clip_r_received']:+6.2f}   "
          "<- not the test: a limiter erases the peaks it acted on")
    print(f"      received peak vs ideal, slope  {a['compression']:6.2f}   "
          "(1.00 is a linear chain)")

    headline, lines = verdict(a)
    print("\n  === VERDICT ===")
    print(f"  {headline}")
    for line in lines:
        print(line)
    return headline


# -- synthetic impairments -------------------------------------------------
#
# Only for making this tool testable without keying a radio. Nothing here is a
# channel model and none of it should be quoted as one -- the repo already
# records, twice, that software screens are poor predictors of this bench
# (experiments/mfsk/RESULTS.md, "The software screen was a poor predictor").
# What they are for is showing that the *diagnostic* separates causes it is
# given in isolation, which is a claim about the tool and not about the radio.


def inject_noise(audio, profile, args, rng):
    """AWGN at args.snr relative to the frame's own RMS."""
    sigma = np.sqrt(np.mean(audio ** 2)) * 10 ** (-args.snr / 20)
    return audio + rng.normal(0.0, sigma, len(audio))


def inject_notch(audio, profile, args, rng):
    """A narrow dip in the amplitude response, plus noise.

    The noise is not optional and that is the point. A channel that only
    attenuates is removed *exactly* by the equaliser -- it divides by H -- so a
    25 dB notch with no noise under it produces a perfect frame and this tool
    correctly reports one. What a notch actually does is amplify whatever noise
    is already there by 1/|H| on that carrier. Injecting the notch alone was
    the first wrong turn in writing this file.
    """
    spectrum = np.fft.rfft(audio)
    freqs = np.fft.rfftfreq(len(audio), 1 / ofdm.SAMPLE_RATE)
    dip = 10 ** (args.notch_db / 20)
    shape = np.exp(-0.5 * ((freqs - args.notch_hz) / max(args.notch_width, 1e-6)) ** 2)
    spectrum = spectrum * (1 + (dip - 1) * shape)
    return inject_noise(np.fft.irfft(spectrum, n=len(audio)), profile, args, rng)


def inject_rolloff(audio, profile, args, rng):
    """Band-edge rolloff above a knee: the failure measure_band_edges.py went
    looking for, applied as a smooth slope rather than a wall."""
    spectrum = np.fft.rfft(audio)
    freqs = np.fft.rfftfreq(len(audio), 1 / ofdm.SAMPLE_RATE)
    excess = np.maximum(freqs - args.rolloff_hz, 0.0) / 1000.0
    spectrum = spectrum * 10 ** (args.rolloff_db_per_khz * excess / 20)
    return inject_noise(np.fft.irfft(spectrum, n=len(audio)), profile, args, rng)


def inject_drift(audio, profile, args, rng):
    """Resample by (1 + ppm), as test_ofdm.py does."""
    n = len(audio)
    return np.interp(np.arange(n) * (1 + args.ppm * 1e-6), np.arange(n), audio)


def inject_clip(audio, profile, args, rng):
    """A hard limiter args.limiter_db above the frame's RMS, standing in for
    the radio's deviation limiter.

    Not band-limited afterwards, deliberately: ofdm._clip_and_filter filters
    its own splatter because it can, and a radio cannot. The in-band part
    lands on the subcarriers either way, and that is the part this is for.
    """
    limit = np.sqrt(np.mean(audio ** 2)) * 10 ** (args.limiter_db / 20)
    return np.clip(audio, -limit, limit)


INJECTORS = {
    "clean": lambda audio, profile, args, rng: audio,
    "noise": inject_noise,
    "notch": inject_notch,
    "rolloff": inject_rolloff,
    "drift": inject_drift,
    "clip": inject_clip,
}


def synth_frame(profile, args, rng, injectors):
    payload = rng.integers(0, 256, profile.max_payload, dtype=np.uint8).tobytes()
    audio = np.asarray(ofdm.modulate(payload, profile, amplitude=args.amplitude),
                       dtype=np.float64)
    for name in injectors:
        audio = INJECTORS[name](audio, profile, args, rng)
    # A real capture has room either side of the frame; give the sync search
    # the same job it will have on the bench rather than an easier one.
    lead = np.zeros(int(0.4 * ofdm.SAMPLE_RATE))
    return payload, np.concatenate([lead, audio, lead])


# -- self test -------------------------------------------------------------

SELF_TEST = [
    ("clean, nothing injected", ["clean"], {}),
    ("noise at 20 dB SNR", ["noise"], {"snr": 20.0}),
    ("noise at 12 dB SNR", ["noise"], {"snr": 12.0}),
    ("notch 1500 Hz -25 dB, 20 dB SNR", ["notch"],
     {"notch_hz": 1500.0, "notch_db": -25.0, "snr": 20.0}),
    ("notch 1500 Hz -25 dB, NO noise", ["notch"],
     {"notch_hz": 1500.0, "notch_db": -25.0, "snr": 200.0}),
    ("rolloff -12 dB/kHz above 1800 Hz, 20 dB SNR", ["rolloff"],
     {"rolloff_hz": 1800.0, "rolloff_db_per_khz": -12.0, "snr": 20.0}),
    ("clock drift 25 ppm", ["drift"], {"ppm": 25.0}),
    ("clock drift 60 ppm", ["drift"], {"ppm": 60.0}),
    ("limiter 3 dB over RMS", ["clip"], {"limiter_db": 3.0}),
    ("limiter 2 dB over RMS", ["clip"], {"limiter_db": 2.0}),
    ("drift 25 ppm AND notch 1500 Hz", ["drift", "notch"],
     {"ppm": 25.0, "notch_hz": 1500.0, "notch_db": -25.0, "snr": 20.0}),
    ("limiter 3 dB plus 20 dB SNR noise", ["clip", "noise"],
     {"limiter_db": 3.0, "snr": 20.0}),
]


def self_test(profile, args):
    """Inject each failure in isolation and print what the tool says about it.

    This is the evidence that the tool discriminates, and it is the only
    regression test it has -- if a change to the verdict order or to a
    threshold breaks one of these, the run says so. The clean case is the
    important one: it measures the estimators' own floors, and a diagnostic
    that finds a cause in a frame with nothing wrong is worthless.
    """
    print(f"=== self test on {profile.name} "
          f"({profile.max_payload}B payload, {profile.n_carriers} carriers) ===")
    outcomes = []
    for label, injectors, overrides in SELF_TEST:
        local = argparse.Namespace(**vars(args))
        for key, value in overrides.items():
            setattr(local, key, value)
        rng = np.random.default_rng(args.seed)
        payload, audio = synth_frame(profile, local, rng, injectors)
        decoded = ofdm.demodulate(audio, profile).get("payload") == payload
        a = analyse(audio, profile, payload, verbose=False)
        print(f"\n-- {label}   (decodes: {decoded})")
        if a is None:
            print("   no sync -- nothing to diagnose")
            outcomes.append((label, "NO SYNC"))
            continue
        if args.full:
            headline = report(a)
        else:
            headline, lines = verdict(a)
            print(f"   errors {int(a['wrong'].sum()):5d}/{a['n_symbols'] * a['n_carriers']}"
                  f"   EVM {evm_db(a['raw_residual']):6.1f} dB"
                  f"   ppm {a['drift']['ppm']:+7.2f}"
                  f"   r(peak) {a['clip_r']:+.2f}")
            print(f"   -> {headline}")
            for line in lines:
                print("  " + line)
        outcomes.append((label, headline.split(":")[0]))
    print("\n=== self test summary ===")
    for label, headline in outcomes:
        print(f"  {label:<45} {headline}")
    return 0


# -- entry point -----------------------------------------------------------


def resolve_profile(args):
    if args.n_fft:
        return ofdm.OfdmProfile(name="custom", n_fft=args.n_fft,
                                cp=args.cp or args.n_fft // 8,
                                bits_per_carrier=args.bits,
                                pilot_interval=args.pilot_interval,
                                band_low=args.band[0] if args.band else ofdm.BAND_LOW_HZ,
                                band_high=args.band[1] if args.band else ofdm.BAND_HIGH_HZ)
    band = tuple(args.band) if args.band else (ofdm.BAND_LOW_HZ, ofdm.BAND_HIGH_HZ)
    pool = ofdm.candidates(band=band, pilot_interval=args.pilot_interval)
    for p in pool:
        if p.name == args.profile:
            return p
    raise SystemExit(f"unknown profile {args.profile!r}\nknown: "
                     + ", ".join(sorted(p.name for p in pool)))


def profile_from_capture_name(path):
    """whale/link.py names its near-miss captures
    nearmiss_{call}_{time}_c{conf}_rx{profile}.npy, so the profile is usually
    in the filename. A hint only -- an AFSK profile name will simply not match
    any OFDM candidate, which is itself worth saying out loud."""
    stem = Path(path).stem
    return stem.split("_rx")[-1] if "_rx" in stem else None


def load_payload(args):
    if args.payload_file:
        return Path(args.payload_file).read_bytes()
    if args.payload_hex:
        return bytes.fromhex(args.payload_hex)
    if args.payload_counting is not None:
        return bytes(i % 256 for i in range(args.payload_counting))
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--n-fft", type=int, default=None,
                    help="build a profile directly instead of naming a candidate")
    ap.add_argument("--cp", type=int, default=None)
    ap.add_argument("--bits", type=int, default=2)
    ap.add_argument("--band", nargs=2, type=float, metavar=("LOW", "HIGH"))
    ap.add_argument("--pilot-interval", type=int, default=0,
                    help="tracking-pilot interval used by the captured frame")

    ap.add_argument("--capture", help=".npy capture to diagnose (see WHALE_CAPTURE_DIR)")
    ap.add_argument("--payload-file", help="the bytes that were sent")
    ap.add_argument("--payload-hex")
    ap.add_argument("--payload-counting", type=int,
                    help="the sweeps' 0,1,2,... payload, this many bytes")

    ap.add_argument("--inject", nargs="+", default=None, choices=sorted(INJECTORS),
                    help="generate a frame and impair it, instead of reading a capture")
    ap.add_argument("--self-test", action="store_true",
                    help="inject every failure in turn and print the verdicts")
    ap.add_argument("--full", action="store_true",
                    help="full report per self-test case, not just the verdict")

    ap.add_argument("--snr", type=float, default=20.0)
    ap.add_argument("--ppm", type=float, default=25.0)
    ap.add_argument("--notch-hz", type=float, default=1500.0)
    ap.add_argument("--notch-db", type=float, default=-25.0)
    ap.add_argument("--notch-width", type=float, default=40.0)
    ap.add_argument("--rolloff-hz", type=float, default=1800.0)
    ap.add_argument("--rolloff-db-per-khz", type=float, default=-12.0)
    ap.add_argument("--limiter-db", type=float, default=3.0)
    ap.add_argument("--amplitude", type=float, default=0.9)
    ap.add_argument("--seed", type=int, default=20260818)
    args = ap.parse_args()

    profile = resolve_profile(args)

    if args.self_test:
        return self_test(profile, args)

    if args.capture:
        path = Path(args.capture)
        if not path.exists() and (CAPTURE_DIR / args.capture).exists():
            path = CAPTURE_DIR / args.capture
        audio = np.load(path)
        payload = load_payload(args)
        hint = profile_from_capture_name(path)
        print(f"capture {path.name}: {len(audio)} samples "
              f"({len(audio) / ofdm.SAMPLE_RATE:.2f}s)")
        if hint and hint != profile.name:
            print(f"  filename says profile {hint!r}; diagnosing as {profile.name!r}. "
                  "If those disagree, every number below is measuring the wrong grid.")
        if payload is None:
            print("  no payload given -- falling back to a decision-directed reference. "
                  "See decision_reference() for what that costs.")
        a = analyse(audio, profile, payload)
        if a is None:
            return 1
        report(a)
        return 0

    injectors = args.inject or ["clean"]
    rng = np.random.default_rng(args.seed)
    payload, audio = synth_frame(profile, args, rng, injectors)
    decoded = ofdm.demodulate(audio, profile).get("payload") == payload
    print(f"synthetic frame: {profile.name}, {len(payload)}B, "
          f"injected {'+'.join(injectors)}, decodes: {decoded}")
    a = analyse(audio, profile, payload)
    if a is None:
        return 1
    report(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
