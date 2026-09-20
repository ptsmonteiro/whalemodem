"""Complete waveform implementations -- the PHY layer.

Three layers sit between the link and the sound card:

  `whale/dsp/`    waveform-independent kernels: PN sequences and symbol
                  mapping, OFDM frame geometry, acquisition, timing,
                  frequency offset, equalization, interleaving, MFSK,
                  convolutional and LDPC coding, the length/CRC payload
                  codec.  Parameterized on geometry, not on any one mode.
  `whale/phy/`    this package: whole waveforms.  Each module here wires
                  those kernels into one on-air signal and exposes a
                  `modulate`/`demodulate` pair (or a frozen dataclass with
                  that pair) in the waveform's own terms -- payload bytes in,
                  audio out.  Nothing here knows about the link.
  `whale/modes/`  the link-facing adapters.  Each `*_mode.py` picks one
                  configuration of one PHY and presents it as the
                  `WaveformMode` (see `whale/waveform.py`) that the link
                  negotiates. Exception: `vf12.py`, `vf13.py`, `vf14.py`
                  and `vf16.py` are self-contained -- one parametric
                  dataclass that is simultaneously the geometry, the
                  modulation and the `WaveformMode` surface, with a
                  `mode_for(**kwargs)` factory for variants -- so for
                  those four there is no separate PHY to pick a
                  configuration of.

  `hr0`           the 32-tone noncoherent FSK short/full control waveform
  `hc0`           16-tone noncoherent FSK
  `hc1w`          23-carrier differential-QPSK OFDM
  `hf2`           pilot-assisted coherent 16-QAM OFDM with carrier grouping
  `sc`            the parametric single-carrier PSK/QAM PHY
  `sc_fast`       `sc` with the fused-FFT acquisition search
  `sc_resilient`  `sc_fast` plus convolutional coding and interleaving
  `ofdm49`        the parametric 49-carrier OFDM PHY (HF6, HF7, HF8, HF9)

Every module here was developed and qualified under `experiments/`; each
module docstring names the experiment directory it came from and the
RESULTS.md that measured it.  Those directories keep the benches and the
evidence; the shipped code lives here.

Dependencies point downward only: `whale/phy/` may use `whale/dsp/`, and
`whale/modes/` may use both.  `whale/phy/` must not import `whale/modes/`
-- `tests/test_layering.py` enforces that, with the one exception noted
there for the shared on-air lead-in.
"""
