"""Software pre-screen for OFDM candidates: which impairment each one dies on.

Bench time is the scarce thing and ofdm.candidates() produces sixty profiles,
so something has to order them before sweep_ofdm.py starts keying radios. This
is that something -- but it is deliberately not the screen
experiments/mfsk/screen_mfsk.py was, because that screen has a post-mortem and
it is not a kind one.


The precedent this file exists to not repeat
--------------------------------------------

screen_mfsk.py walked every candidate down an AWGN waterfall and reported the
SNR it needed, referenced to PROFILE_1200 on the same yardstick.
experiments/mfsk/RESULTS.md records what that was worth:

    19 of 24 candidates cleared its bar. Exactly one worked on air.

Candidates it passed with 4 dB of apparent margin decoded 0/5 on the weak leg.
The post-mortem is explicit about why, and the reason is not that the model was
badly implemented -- it is that **noise is not the binding impairment on this
bench**. The mode that actually failed did so with 8 wrong symbols in 1596, all
of them the same adjacent-tone confusion, spread evenly through the frame: that
is leakage through a real audio chain, and AWGN does not contain it. An AWGN
screen measures the one thing that is not breaking anything.

So this file models the things that plausibly *are*, and treats noise as the
last column rather than the only one. Four impairments, in the order the signal
meets them:

  - **A hard limiter in the transmit path.** OFDM is the first
    non-constant-envelope mode in this repo, so this is the impairment with no
    precedent anywhere in it -- every previous mode could be clipped into a
    square wave and still decode. ofdm._clip_and_filter's own docstring says
    its table "does not model the radio's own limiter" and calls that "the one
    part of this module with no bench evidence behind it at all". This puts a
    limiter there.

  - **Dispersion.** The reason OFDM is being tried at all. A cyclic prefix
    longer than the channel's delay spread removes dispersion *exactly*; a
    shorter one does not remove it at all. That is a cliff, not a slope, and
    where a candidate sits relative to it is the single most decision-relevant
    thing about it.

  - **A receiver blackout at the start of the transmission.** whale/framing.py
    HEAD_PAD_SECONDS records an HT on this bench blacking out -- not
    attenuating, blacking out -- for ~110ms after its squelch opens, and that
    blackout killed PROFILE_1200 outright while 300 and 600 baud kept working.
    It is the only impairment here with a measured number attached.

  - **AWGN**, scored relatively, for screen_mfsk.py's reason (below).


How SNR is defined here, and why it differs from screen_mfsk.py
---------------------------------------------------------------

screen_mfsk.py measured SNR as the mode's own in-band power over in-band noise,
matching scripts/measure_snr.py's definition. That is the right yardstick for a
family of constant-envelope modes, where every candidate transmits the same
average power and the drive level is not a variable.

It is the wrong one here, and taking it over unexamined would have hidden the
single biggest thing about OFDM on an FM link. FM is **peak**-limited:
deviation follows the instantaneous audio level, so what the transmitter caps
is the peak while what sets received SNR is the average. A mode with 9 dB of
peak-to-average ratio therefore arrives ~6 dB weaker than a constant-envelope
mode driven to the same deviation -- for free, before any impairment.
Referencing SNR to each mode's own average power makes that 6 dB vanish from
the report, which is precisely the handicap ofdm.modulate's docstring says is
"the standing cost of a non-constant-envelope mode on a peak-limited
transmitter".

So SNR here is referenced to **full deviation**: the power of a full-scale sine
(0.5) over the noise power inside the mode's own occupied band plus
BAND_MARGIN either side. That is a property of the link rather than of the
mode, so PAPR shows up as a required-SNR penalty where it belongs, and a mode
occupying 1700 Hz is honestly charged more noise than one occupying 1000 Hz.

The absolute dB on this scale still mean nothing on air, for exactly the reason
screen_mfsk.py's docstring gives at length -- scripts/measure_snr.py estimates
in-band noise from side bands, and under FM a captured carrier quiets the noise
*inside* the occupied band far more than beside it, so the two 7.5 dBs are not
the same quantity. Every number in the last column is therefore reported as a
delta against afsk.PROFILE_1200 pushed through this identical model,
re-measured on every run. A mode already in service at 100% both directions is
the only absolute reference this bench has actually earned.


There is no pass/fail bar in this file, on purpose
--------------------------------------------------

The MFSK screen had one and it was worse than useless -- it cost bench time
rather than saving it, because a "PASS" reads as permission to skip a trial.
What did carry signal there was the *ordering*: its winner needed the least SNR
of anything screened, and the on-air outcome tracked required-SNR better than
it tracked throughput.

So this file ranks and refuses to grade. Two of the four impairments are swept
over a range precisely because nothing in this repo has ever measured where
the channel sits on them:

  - **delay spread** -- probe_channel.py is what will measure it, and it has
    not been run. Until it has, "tolerates 4.2 ms" is a fact about the
    candidate and "4.2 ms is enough" is a guess.
  - **limiter backoff** -- no bench evidence at all, per _clip_and_filter.

The one number that is measured is the 110ms blackout, and it is the one place
this file would say a candidate is eliminated rather than merely ranked last.
Even there, note that the blackout column turned out not to discriminate
between candidates at all (below), so it eliminates nothing in practice.


The model, impairment by impairment
-----------------------------------

**Limiter (limit()).** The frame is peak-normalised to the deviation limit --
so `overdrive_db` is measured from the point where the limiter first touches
the waveform, identically for every mode regardless of what amplitude was
passed to modulate -- then amplified by `overdrive_db` and hard-clipped at the
limit. Clipping *then* filtering to the radio's transmit audio passband
(RADIO_PASSBAND, 300-3000 Hz) is the physical order: mic amp and deviation
limiter first, splatter filter after. Peaks regrow through that filter and are
not re-clipped, which makes this model of the radio slightly kinder than a real
one.

The distortion this creates is entirely in-band and lands on every subcarrier
at once. Nothing removes it, which is the whole difference between this limiter
and the one inside ofdm._clip_and_filter: that one clips in software where the
splatter can be filtered off, this one clips inside the radio where it cannot.

Note what overdrive buys as well as costs, because a model that only counted
the cost would give the wrong answer. Clipping raises the average power against
a fixed deviation limit -- that is the entire reason _clip_and_filter exists --
so required SNR against a full-deviation reference *improves* with overdrive
until the distortion overtakes it. --drive-curve prints that curve, and it is
the software half of what sweep_ofdm.py --drive measures on air.

**Dispersion (_delay_ramp, default).** An all-pass filter whose group delay
rises linearly from 0 at DC to `spread` at 3 kHz. |H| is exactly flat, so this
is group delay and nothing else -- no notches, no tilt, nothing a candidate
could die on except the smearing itself. Its impulse response spans `spread`
seconds, which is what makes the parameter directly comparable with
OfdmProfile.cp_seconds.

That comparability is the check that makes the model worth anything, and it is
the first thing this file was run to confirm. Measured at 30 dB SNR, QPSK at
50 Hz spacing, ladder steps of 0.5 ms:

    ofdm4_50hz_cp4     cp = 5.00 ms     tolerates 5.0 ms
    ofdm4_50hz_cp8     cp = 2.50 ms     tolerates 2.5 ms
    ofdm4_50hz_cp16    cp = 1.25 ms     tolerates 1.0 ms

The tolerated spread tracks the cyclic prefix 1:1 to within a ladder step, and
the break is sharp -- one step either side of it is the difference between
every frame decoding and none. That is the cliff OFDM theory predicts,
reproduced without being built in anywhere: nothing in _delay_ramp knows what
cp is. If that relationship ever stops holding, the model has drifted from the
modem and every number below stops meaning anything.

**--dispersion multipath** is the alternative: a Rician tapped-delay line with
an exponential power-delay profile, drawn fresh per trial. It puts notches in
|H|, which is a different mechanism and the one probe_channel.py exists to
find -- with no FEC, one subcarrier in a notch fails every frame in the run
regardless of margin everywhere else. It is not the default because a wired-ish
FM audio chain has no scatterers in it; a smooth minimum-phase response is the
better model of what is there, and a random one conflates ISI with
frequency-selective fading so a candidate's death cannot be attributed.

**Blackout (receive()).** The first `blackout` seconds of the received
transmission are zeroed. Zeroed rather than attenuated or distorted, on
framing.py's own reading of the capture that measured it: "the signal is
absent, not distorted, so no receiver-side cleverness recovers it".

**Noise (receive()).** White by default, added across the whole buffer rather
than only across the frame, so the sync correlator has to reject noise-only
audio in the same call that finds the frame -- the failure mode
afsk._normalised_correlation and mfsk._centre were both rewritten for.
--fm-noise switches to a triangular noise spectrum (power rising as f^2), which
is what an FM discriminator produces before de-emphasis. It is off by default
because this repo has no evidence about how much de-emphasis is in either
chain, and 600-2300 Hz is 1.9 octaves, so the flag is worth ~11 dB of tilt
across the band -- far too large a thing to turn on by assumption. It is here
because it is the most plausible reason a candidate's low subcarriers would
beat its high ones on air.


One wrong turn, recorded because it produced a plausible-looking table
---------------------------------------------------------------------

The first version applied the dispersion filter with an FFT the same length as
the frame. That is a circular convolution, so the energy a group-delay ramp
pushes past the end of the frame wraps around onto the *head pad*, i.e. in
front of the preamble. Frames kept decoding at spreads well past their prefix,
because the ISI that should have landed on symbol N+1 was landing on dead air
instead. The table looked entirely reasonable -- every candidate tolerating 8+
ms -- and it was measuring nothing. _filtered() and _delay_ramp() both pad past
the signal now, and the cp relationship in the table above is what says the
fix worked.


What this still cannot see
--------------------------

Everything screen_mfsk.py could not, minus dispersion, plus:

  - **Amplitude response.** |H| is flat in the default model. A real chain
    rolls off, and with no FEC one subcarrier in a notch fails every frame.
    That is probe_channel.py's job and it is a measurement, not a model.
  - **The receiver's AGC.** A frame whose envelope moves 9 dB is a new thing
    for a chain whose AGC has only ever seen constant-envelope audio, and the
    AGC is what the blackout measurement says takes 110ms to settle.
  - **Whatever actually broke MFSK.** Its post-mortem could not fully explain
    why the *lowest* tone was always the one that failed. An unexplained
    failure mode cannot be modelled, and this channel has one.

Use the ordering. Do not use it to skip a rung. sweep_ofdm.py decides.

Run: python experiments/ofdm/screen_ofdm.py
     python experiments/ofdm/screen_ofdm.py --per-bits 5 --trials 8
     python experiments/ofdm/screen_ofdm.py --dispersion multipath
     python experiments/ofdm/screen_ofdm.py --drive-curve ofdm4_50hz_cp8
     python experiments/ofdm/screen_ofdm.py --out screen.json
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from scipy.fft import next_fast_len

import ofdm
from whale import afsk

SAMPLE_RATE = ofdm.SAMPLE_RATE

# The mode already in service, and the payload throughput it delivers. Every dB
# in the last column is a delta against this profile pushed through the same
# model, re-measured on every run rather than hardcoded, so a change to the
# model or to the modem moves the reference with it.
REFERENCE_PROFILE = afsk.PROFILE_1200
REFERENCE_BITRATE = REFERENCE_PROFILE.chunk_size * 8 / ofdm.MAX_KEYING_SECONDS

# Noise is counted over the mode's occupied band plus this either side, as
# scripts/measure_snr.py does. Kept identical so the *shape* of the definition
# matches the bench script even though the reference level deliberately does
# not -- see the module docstring.
BAND_MARGIN = 200.0

# The radio's transmit audio path, and the only thing that removes any of the
# limiter's splatter. Deliberately wider than any candidate's band: filtering
# to the profile's own band would delete distortion the receiver never sees
# anyway, and would make the channel model per-candidate, which it must not be.
RADIO_PASSBAND = (300.0, 3000.0)
_PASSBAND_TRANSITION = 150.0

# Full deviation, and the power of a full-scale sine at it. SNR is referenced
# to this rather than to the mode's own average power -- see the module
# docstring on why that distinction is the point on a peak-limited transmitter.
DEVIATION_LIMIT = 1.0
FULL_DEVIATION_POWER = 0.5

# whale/framing.py HEAD_PAD_SECONDS: an HT on this bench blacks out for this
# long after its squelch opens. The only number in this file that came off the
# bench rather than out of a range.
MEASURED_BLACKOUT_SECONDS = 0.110

# Dead air either side of the transmission in the receiver's buffer. Noise
# covers it too, so the sync correlator has to reject noise-only audio in the
# same call that finds the frame.
PAD_SECONDS = 0.5

# The ladders. The ascending ones report the largest value at which every trial
# still decodes; the SNR waterfall reports the smallest.
SPREAD_LADDER_MS = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 13.0)
OVERDRIVE_LADDER_DB = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0)
BLACKOUT_LADDER_MS = (0.0, 60.0, 110.0, 150.0, 200.0, 250.0, 300.0, 400.0, 500.0)
SNR_CEILING_DB = 34.0
SNR_FLOOR_DB = 0.0
SNR_COARSE_STEP_DB = 3.0

# The SNR at which the other three axes are measured. High enough that noise is
# not what breaks the frame -- the point of measuring the axes separately --
# but not infinite, because a model with no noise in it at all hides marginal
# decisions behind exact arithmetic.
CLEAN_SNR_DB = 30.0


# -- the channel -----------------------------------------------------------


def _passband_mask(n, lo, hi, transition=_PASSBAND_TRANSITION):
    """Raised-cosine skirted passband, as ofdm._band_mask -- a brick wall in
    frequency is a sinc in time, and its ringing would spill across exactly the
    prefix boundaries this whole mode rests on."""
    freqs = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
    mask = np.ones_like(freqs)
    mask[freqs <= lo - transition] = 0.0
    mask[freqs >= hi + transition] = 0.0
    rising = (freqs > lo - transition) & (freqs < lo)
    falling = (freqs > hi) & (freqs < hi + transition)
    mask[rising] = 0.5 * (1 - np.cos(np.pi * (freqs[rising] - (lo - transition)) / transition))
    mask[falling] = 0.5 * (1 + np.cos(np.pi * (freqs[falling] - hi) / transition))
    return mask


def _filtered(audio, mask_fn):
    """Apply a frequency-domain mask at a length the FFT likes.

    The transform is padded past the signal so this is a linear convolution and
    not a circular one. See the module docstring's wrong turn: wrapping a
    frame's tail onto its own head pad puts energy in front of the preamble
    that no radio ever put there, and it silently flatters every candidate.
    """
    n = len(audio)
    m = next_fast_len(n + SAMPLE_RATE // 100)
    return np.fft.irfft(np.fft.rfft(audio, n=m) * mask_fn(m), n=m)[:n]


def limit(audio, overdrive_db, passband=RADIO_PASSBAND):
    """The radio's deviation limiter, applied after ofdm.modulate has already
    done its own clipping.

    The frame is first peak-normalised to DEVIATION_LIMIT, so overdrive_db is
    measured from the exact onset of limiting whatever amplitude the caller
    modulated at -- and identically for a constant-envelope mode and an OFDM
    one, which is what makes the column comparable across the table. Then it is
    driven up by overdrive_db and hard-clipped.

    Clip first, filter second: that is the order inside the radio (mic amp and
    limiter, then the splatter filter), and it is why the splatter this makes
    cannot be removed the way ofdm._clip_and_filter removes its own. What
    survives the filter is in-band distortion sitting on every subcarrier at
    once.
    """
    audio = np.asarray(audio, dtype=np.float64)
    peak = float(np.max(np.abs(audio)))
    if peak <= 0:
        return audio
    audio = audio * (DEVIATION_LIMIT / peak) * 10 ** (overdrive_db / 20)
    if overdrive_db <= 0:
        return audio
    audio = np.clip(audio, -DEVIATION_LIMIT, DEVIATION_LIMIT)
    return _filtered(audio, lambda n: _passband_mask(n, *passband))


def _delay_ramp(audio, spread_seconds, knee_hz=3000.0):
    """All-pass dispersion: group delay rising linearly from 0 at DC to
    `spread_seconds` at knee_hz, flat above it.

    |H| is exactly 1 everywhere, so this is group delay and nothing else -- no
    notches, no tilt, nothing a candidate can die on except the smearing
    itself. The impulse response spans spread_seconds, which is what makes the
    parameter directly comparable with OfdmProfile.cp_seconds and the table in
    the module docstring meaningful.

    Integrating tau(f) gives the phase: phi(f) = -2*pi * int_0^f tau df, which
    is quadratic below the knee and linear above it.
    """
    audio = np.asarray(audio, dtype=np.float64)
    if spread_seconds <= 0:
        return audio
    tail = int(np.ceil(spread_seconds * SAMPLE_RATE)) + 1
    n = len(audio) + tail
    m = next_fast_len(n)
    freqs = np.fft.rfftfreq(m, 1 / SAMPLE_RATE)
    slope = spread_seconds / knee_hz
    below = freqs <= knee_hz
    phase = np.empty_like(freqs)
    phase[below] = -2 * np.pi * 0.5 * slope * freqs[below] ** 2
    phase[~below] = -2 * np.pi * (0.5 * slope * knee_hz ** 2
                                  + spread_seconds * (freqs[~below] - knee_hz))
    return np.fft.irfft(np.fft.rfft(audio, n=m) * np.exp(1j * phase), n=m)[:n]


def _multipath(audio, spread_seconds, rng, rice_db=6.0):
    """Rician tapped-delay line with an exponential power-delay profile whose
    RMS delay spread is `spread_seconds`, drawn fresh on every call.

    Unlike _delay_ramp this puts notches in |H|, which is a different failure
    mechanism and the one probe_channel.py exists to measure: with no FEC, one
    subcarrier in a notch fails every frame in the run regardless of the margin
    everywhere else. rice_db is how much of the energy stays in the direct
    path; at 6 dB the response ripples rather than nulls. It is a guess, as is
    the decision to model scatterers on an audio cable at all, which is why
    this is not the default.
    """
    audio = np.asarray(audio, dtype=np.float64)
    if spread_seconds <= 0:
        return audio
    n_taps = int(np.ceil(5 * spread_seconds * SAMPLE_RATE)) + 1
    t = np.arange(n_taps) / SAMPLE_RATE
    # Power decays as exp(-t/spread) so amplitude decays as exp(-t/(2*spread));
    # the RMS delay spread of that profile is spread_seconds.
    h = rng.normal(size=n_taps) * np.exp(-t / (2 * spread_seconds))
    h /= np.linalg.norm(h) or 1.0
    h[0] += np.sqrt(10 ** (rice_db / 10))
    h /= np.linalg.norm(h) or 1.0
    return np.convolve(audio, h)


def disperse(audio, spread_seconds, kind, rng):
    if kind == "multipath":
        return _multipath(audio, spread_seconds, rng)
    return _delay_ramp(audio, spread_seconds)


def _noise(n, band, snr_db, rng, fm_noise=False):
    """Noise whose power inside `band` (plus BAND_MARGIN either side) sits
    snr_db below the power of a full-scale sine.

    White noise of variance s^2 spreads flat over 0..fs/2, so the power landing
    in a band of width B is s^2 * B / (fs/2); inverting that gives s^2, exactly
    as screen_mfsk.add_awgn does. The triangular case cannot be inverted in
    closed form once the shaping is applied, so it is shaped first and then
    rescaled to the same in-band power numerically.
    """
    lo = max(band[0] - BAND_MARGIN, 0.0)
    hi = min(band[1] + BAND_MARGIN, SAMPLE_RATE / 2)
    target = FULL_DEVIATION_POWER / 10 ** (snr_db / 10)
    if not fm_noise:
        variance = target * (SAMPLE_RATE / 2) / (hi - lo)
        return rng.normal(0.0, np.sqrt(variance), n)

    m = next_fast_len(n)
    freqs = np.fft.rfftfreq(m, 1 / SAMPLE_RATE)
    spectrum = np.fft.rfft(rng.normal(0.0, 1.0, m)) * (freqs / max(freqs[-1], 1e-9))
    shaped = np.fft.irfft(spectrum, n=m)[:n]
    in_band = (freqs >= lo) & (freqs <= hi)
    total = float(np.sum(np.abs(spectrum) ** 2)) or 1.0
    share = float(np.sum(np.abs(spectrum[in_band]) ** 2)) / total
    power = float(np.mean(shaped ** 2)) * share
    return shaped * np.sqrt(target / power) if power > 0 else shaped


def receive(audio, band, snr_db, blackout_seconds, rng, fm_noise=False,
            pad_seconds=PAD_SECONDS):
    """The transmission as the receiver's buffer holds it: dead air either
    side, noise over the whole thing, and the opening `blackout_seconds` gone.

    Zeroed rather than attenuated or distorted, on framing.py's own reading of
    the capture that measured it -- "the signal is absent, not distorted, so no
    receiver-side cleverness recovers it". The noise goes with it: nothing is
    coming out of the discriminator during a blackout either.
    """
    pad = int(round(pad_seconds * SAMPLE_RATE))
    buf = np.zeros(len(audio) + 2 * pad)
    buf[pad:pad + len(audio)] = audio
    buf += _noise(len(buf), band, snr_db, rng, fm_noise)
    blackout = int(round(blackout_seconds * SAMPLE_RATE))
    if blackout > 0:
        buf[pad:pad + blackout] = 0.0
    return buf


# -- what a candidate looks like to the model ------------------------------


@dataclass
class Mode:
    """One thing that can be pushed through the channel.

    Exists so REFERENCE_PROFILE goes through byte-for-byte the same code path
    as every candidate rather than through a parallel one that might differ --
    the same reason screen_mfsk._BinaryShim existed, and the reason the
    reference figure is worth quoting at all.
    """

    name: str
    band: tuple
    payload_len: int
    bitrate: float
    modulate: Callable
    demodulate: Callable
    cp_ms: float = float("nan")


def ofdm_mode(profile: ofdm.OfdmProfile) -> Mode:
    return Mode(name=profile.name,
                band=(profile.tone_low, profile.tone_high),
                payload_len=profile.max_payload,
                bitrate=profile.payload_bitrate,
                modulate=lambda pl, p=profile: ofdm.modulate(pl, p),
                demodulate=lambda a, p=profile: ofdm.demodulate(a, p),
                cp_ms=profile.cp_seconds * 1000)


def reference_mode() -> Mode:
    p = REFERENCE_PROFILE
    return Mode(name=p.name,
                band=(min(p.freq0, p.freq1), max(p.freq0, p.freq1)),
                payload_len=p.chunk_size,
                bitrate=REFERENCE_BITRATE,
                modulate=lambda pl: afsk.modulate(pl, profile=p),
                demodulate=lambda a: afsk.demodulate(a, profile=p))


# -- measurement -----------------------------------------------------------


def decode_rate(mode: Mode, trials, rng, *, overdrive_db=0.0, spread_ms=0.0,
                blackout_ms=0.0, snr_db=CLEAN_SNR_DB, dispersion="delay-ramp",
                fm_noise=False):
    """Fraction of `trials` that decode, at the mode's full keying-budget
    payload.

    Full size deliberately, for screen_mfsk._decode_rate's reason: a short
    probe frame is the easiest thing this channel ever carries and says nothing
    about the mode you would actually run, and a frame is all-or-nothing under
    CRC so the error rate that matters is the one over thousands of bits.
    Random payloads for two reasons: a fixed one can pass on a decoder that
    reconstructs what it expects, and ofdm's whitener means the PAPR the
    limiter sees is payload-independent anyway.
    """
    ok = 0
    confidences = []
    for _ in range(trials):
        payload = rng.integers(0, 256, mode.payload_len, dtype=np.uint8).tobytes()
        audio = limit(mode.modulate(payload), overdrive_db)
        audio = disperse(audio, spread_ms / 1000.0, dispersion, rng)
        buf = receive(audio, mode.band, snr_db, blackout_ms / 1000.0, rng, fm_noise)
        result = mode.demodulate(buf)
        confidences.append(float(result.get("confidence", 0.0)))
        ok += int(result.get("payload") == payload)
    return ok / trials, float(np.mean(confidences))


def worst_tolerated(mode, axis, ladder, trials, rng, base):
    """Largest value on `ladder` at which every trial decodes, walking up and
    stopping at the first failure.

    None means the mode failed at the gentlest setting on the ladder, which for
    the ascending ladders here -- all of which start at zero -- means it failed
    under the base impairments alone.
    """
    best = None
    for value in ladder:
        rate, _ = decode_rate(mode, trials, rng, **{axis: value}, **base)
        if rate < 1.0:
            return best
        best = float(value)
    return best


def required_snr_db(mode, trials, rng, base, ceiling=SNR_CEILING_DB,
                    floor=SNR_FLOOR_DB, coarse=SNR_COARSE_STEP_DB):
    """Lowest SNR at which every trial decodes, to 1 dB.

    A coarse pass down in `coarse` dB steps finds the bracket and a 1 dB walk
    inside it finds the edge. Walked downward and stopped at the first failure,
    as screen_mfsk.threshold_db is, so a hopeless candidate costs two points
    rather than the whole waterfall -- and the coarse pass makes a *good*
    candidate cost about a third of one, which is what pays for the three extra
    axes this screen measures.
    """
    best = None
    best_conf = 0.0
    snr = ceiling
    while snr >= floor:
        rate, conf = decode_rate(mode, trials, rng, snr_db=snr, **base)
        if rate < 1.0:
            break
        best, best_conf = float(snr), conf
        snr -= coarse
    if best is None:
        return None, 0.0
    fine = best - 1.0
    while fine > best - coarse and fine >= floor:
        rate, conf = decode_rate(mode, trials, rng, snr_db=fine, **base)
        if rate < 1.0:
            break
        best, best_conf = float(fine), conf
        fine -= 1.0
    return best, best_conf


def screen(mode: Mode, trials, rng, args):
    """Every column for one mode.

    Each of the three non-noise axes is measured with the other two absent and
    the *measured* blackout present, because the blackout is on every real
    keying whether or not it is the axis under test. The last column puts all
    four together at the nominal operating point, and it is the only column
    that depends on the two numbers this file has to guess.
    """
    blackout = MEASURED_BLACKOUT_SECONDS * 1000
    common = dict(dispersion=args.dispersion, fm_noise=args.fm_noise)
    row = {"name": mode.name, "bitrate": mode.bitrate, "cp_ms": mode.cp_ms}

    row["spread_ms"] = worst_tolerated(
        mode, "spread_ms", SPREAD_LADDER_MS, trials, rng,
        dict(blackout_ms=blackout, snr_db=CLEAN_SNR_DB, **common))
    row["overdrive_db"] = worst_tolerated(
        mode, "overdrive_db", OVERDRIVE_LADDER_DB, trials, rng,
        dict(blackout_ms=blackout, snr_db=CLEAN_SNR_DB, **common))
    row["blackout_ms"] = worst_tolerated(
        mode, "blackout_ms", BLACKOUT_LADDER_MS, trials, rng,
        dict(snr_db=CLEAN_SNR_DB, **common))

    need, conf = required_snr_db(mode, trials, rng,
                                 dict(overdrive_db=args.nominal_overdrive,
                                      spread_ms=args.nominal_spread,
                                      blackout_ms=blackout, **common))
    row["snr_db"], row["confidence"] = need, conf
    return row


def cause_of_death(row, args):
    """Which impairment a candidate that never decodes at the combined
    operating point died on.

    Falls out of the per-axis columns rather than needing a separate
    measurement: a candidate tolerating less spread than the operating point
    asks of it died on dispersion, and so on. Checked blackout-first because
    that is the one threshold with a bench number behind it.
    """
    if row["blackout_ms"] is None or row["blackout_ms"] < MEASURED_BLACKOUT_SECONDS * 1000:
        return "blackout"
    if row["spread_ms"] is None or row["spread_ms"] < args.nominal_spread:
        return "dispersion"
    if row["overdrive_db"] is None or row["overdrive_db"] < args.nominal_overdrive:
        return "limiter"
    return "noise/combination"


# -- reporting -------------------------------------------------------------


def _fmt(value, spec, missing="  --"):
    return missing if value is None else format(value, spec)


def pool(args):
    """Candidates to screen: the top --per-bits of each constellation order,
    unioned and left in throughput order.

    Stratified rather than straight throughput order because throughput order
    is dominated by constellation -- the first fifteen candidates
    ofdm.candidates() returns are all 16-QAM -- and the question this screen
    exists to answer is precisely whether the constellation the throughput
    ordering prefers survives a limiter. --top overrides it with the sweep's
    own ordering, for checking where in that ladder to start.
    """
    everything = ofdm.candidates(band=tuple(args.band))
    if args.top:
        return everything[:args.top]
    chosen, seen = [], {}
    for profile in everything:
        seen.setdefault(profile.bits_per_carrier, [])
        if len(seen[profile.bits_per_carrier]) < args.per_bits:
            seen[profile.bits_per_carrier].append(profile)
            chosen.append(profile)
    return chosen


def drive_curve(profile_name, args, trials, rng):
    """Required SNR against overdrive, for one candidate and the reference.

    The software half of what sweep_ofdm.py --drive measures on air. Two
    effects pull against each other and this is where they cross: driving past
    the limiter raises the average power against a fixed deviation limit, which
    is a straight gain, and the distortion it makes lands in band on every
    subcarrier, which is a straight loss. The reference column is the control
    -- a constant-envelope mode should show the gain and almost none of the
    loss, and if it does not, the limiter model is wrong.
    """
    match = [p for p in ofdm.candidates(band=tuple(args.band)) if p.name == profile_name]
    if not match:
        print(f"no candidate named {profile_name}")
        return 1
    modes = [ofdm_mode(match[0]), reference_mode()]
    print(f"required SNR vs overdrive past the limiter, {trials} trials, "
          f"spread={args.nominal_spread:g}ms, "
          f"blackout={MEASURED_BLACKOUT_SECONDS * 1000:.0f}ms\n")
    print(f"{'overdrive':>9}  " + "  ".join(f"{m.name:>12}" for m in modes))
    for overdrive in OVERDRIVE_LADDER_DB:
        cells = []
        for mode in modes:
            need, _ = required_snr_db(mode, trials, rng,
                                      dict(overdrive_db=overdrive,
                                           spread_ms=args.nominal_spread,
                                           blackout_ms=MEASURED_BLACKOUT_SECONDS * 1000,
                                           dispersion=args.dispersion,
                                           fm_noise=args.fm_noise))
            cells.append(f"{need:>9.0f} dB" if need is not None else f"{'never':>12}")
        print(f"{overdrive:>7.0f} dB  " + "  ".join(cells))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=5,
                    help="trials that must all decode for a ladder rung to count")
    ap.add_argument("--per-bits", type=int, default=4,
                    help="candidates to take from each constellation order")
    ap.add_argument("--top", type=int, default=0,
                    help="instead, screen the top N in the sweep's own throughput order")
    ap.add_argument("--band", type=float, nargs=2,
                    default=[ofdm.BAND_LOW_HZ, ofdm.BAND_HIGH_HZ],
                    help="candidate band, as sweep_ofdm.py --band; run probe_channel.py first")
    ap.add_argument("--dispersion", choices=("delay-ramp", "multipath"), default="delay-ramp",
                    help="all-pass group delay (default) or a Rician tapped-delay line")
    ap.add_argument("--fm-noise", action="store_true",
                    help="triangular noise spectrum, as an FM discriminator makes it")
    ap.add_argument("--nominal-spread", type=float, default=2.0,
                    help="delay spread in ms at the combined operating point. NOT MEASURED -- "
                         "nothing in this repo knows the channel's delay spread; "
                         "probe_channel.py is what will")
    ap.add_argument("--nominal-overdrive", type=float, default=3.0,
                    help="dB past the onset of limiting at the combined operating point. "
                         "NOT MEASURED -- see ofdm._clip_and_filter")
    ap.add_argument("--drive-curve", metavar="NAME",
                    help="instead of the table, print required SNR vs overdrive for one candidate")
    ap.add_argument("--out", help="write the table to this file as JSON")
    ap.add_argument("--seed", type=int, default=20260818)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    if args.drive_curve:
        return drive_curve(args.drive_curve, args, args.trials, rng)

    print(f"model: limiter -> {args.dispersion} dispersion -> "
          f"{'triangular' if args.fm_noise else 'white'} noise -> "
          f"{MEASURED_BLACKOUT_SECONDS * 1000:.0f}ms blackout; "
          f"{args.trials} trials must all decode.\n"
          f"SNR is referenced to full deviation, not to the mode's own power, "
          f"so PAPR is priced in.\n"
          f"combined operating point: {args.nominal_spread:g}ms spread, "
          f"{args.nominal_overdrive:g}dB overdrive -- NEITHER IS MEASURED.\n")

    reference = screen(reference_mode(), args.trials, rng, args)
    ref_snr = reference["snr_db"]
    print(f"reference {reference['name']}: "
          f"tolerates {_fmt(reference['spread_ms'], '.1f')}ms spread, "
          f"{_fmt(reference['overdrive_db'], '.0f')}dB overdrive, "
          f"{_fmt(reference['blackout_ms'], '.0f')}ms blackout, "
          f"needs {_fmt(ref_snr, '.0f')}dB at {REFERENCE_BITRATE:.0f} bits/s\n")

    header = (f"{'candidate':<20}{'bits/s':>8}{'x ref':>7}{'cp ms':>7}"
              f"{'spread':>9}{'ovrdrv':>8}{'blkout':>8}{'SNR':>7}{'vs ref':>8}  note")
    print(header)
    print("-" * len(header))

    rows = []
    for profile in pool(args):
        row = screen(ofdm_mode(profile), args.trials, rng, args)
        row["note"] = ("" if row["snr_db"] is not None
                       else "dies on " + cause_of_death(row, args))
        row["delta_db"] = (None if row["snr_db"] is None or ref_snr is None
                           else row["snr_db"] - ref_snr)
        rows.append(row)
        print(f"{row['name']:<20}{row['bitrate']:>8.0f}"
              f"{row['bitrate'] / REFERENCE_BITRATE:>6.1f}x"
              f"{row['cp_ms']:>7.2f}"
              f"{_fmt(row['spread_ms'], '>7.1f')}ms"
              f"{_fmt(row['overdrive_db'], '>6.0f')}dB"
              f"{_fmt(row['blackout_ms'], '>6.0f')}ms"
              f"{_fmt(row['snr_db'], '>5.0f')}dB"
              f"{_fmt(row['delta_db'], '>+7.0f')}dB  {row['note']}")

    survivors = [r for r in rows if r["snr_db"] is not None]
    dead = [r for r in rows if r["snr_db"] is None]
    survivors.sort(key=lambda r: (r["snr_db"], -r["bitrate"]))

    print(f"\n{len(survivors)} of {len(rows)} candidates decode at the combined "
          f"operating point, ordered by required SNR:")
    for i, row in enumerate(survivors, 1):
        print(f"  {i:>2}. {row['name']:<20} {row['bitrate']:>7.0f} bits/s "
              f"({row['bitrate'] / REFERENCE_BITRATE:.1f}x) at "
              f"{row['snr_db']:.0f} dB ({row['delta_db']:+.0f} vs reference), "
              f"tolerates {_fmt(row['spread_ms'], '.1f')}ms / "
              f"{_fmt(row['overdrive_db'], '.0f')}dB")
    if dead:
        print("\neliminated at the combined operating point:")
        for row in dead:
            print(f"      {row['name']:<20} {row['note']}")

    print("\nOrdering only. This is not a pass/fail bar, and the two operating-point "
          "numbers above are guesses -- experiments/mfsk/RESULTS.md records an AWGN "
          "screen passing 19 of 24 candidates of which exactly one worked on air. "
          "Run probe_channel.py to replace --nominal-spread with a measurement, and "
          "let sweep_ofdm.py decide.")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "when": datetime.now(timezone.utc).isoformat(),
            "args": vars(args), "reference": reference, "candidates": rows,
        }, indent=2))
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
