# experiments/

`experiments/` is scratch work and evidence. Nothing here is production code.

## The rules

**`whale/` must never import from `experiments/`.** This is enforced by
`test_whale_never_imports_experiments` in `tests/test_layering.py`, which walks
every module under `whale/` and fails on any `experiments` import. Tests and
`scripts/` may import an experiment; the modem itself may not.

**When an experiment's code ships, it moves.** A reusable, waveform-independent
kernel becomes a module in `whale/dsp/`. A complete waveform - modulate,
demodulate, frame geometry - becomes a module in `whale/phy/`, with its
registry-facing wrapper in `whale/modes/`. The experiment directory stays behind
as the evidence record: `RESULTS.md` keeps the measurements that justified the
promotion, and the promoted module's docstring names the experiment it came
from. Copies of shipped DSP are not kept here.

**A retired experiment keeps its markdown and loses its data.** When an
experiment is done - adopted, superseded or abandoned - its `.py`, `.npy`,
`.npz`, `.bin`, `.json`, `.txt` and `.log` files are removed with `git rm`, so
history still holds them, and every `.md` file stays. Prose across `whale/` and
the root documents cites these directories as qualification evidence, and those
citations must keep resolving. The single exception is a data file cited by path
from surviving code or docs - for example
`experiments/ofdm/results/measurements/bandwidth.json`, which `whale/fm_channel.py`
reads at runtime.

See [`RETIRED.md`](RETIRED.md) for the index of retired experiments, what each
one was, why it was not adopted, and how to recover any deleted file from git.

## Run output is not committed

Raw captures and per-trial dumps under `results/`, `captures/` and `hardware/`,
and any `.npy`/`.npz`/`.bin` anywhere under `experiments/`, are ignored by the
repository-root `.gitignore`. If a particular artifact is genuinely part of the
evidence - something a test or a document reads by path - force-add it with
`git add -f` and say in the experiment's markdown why it is tracked.

## What is still live

An experiment directory that still has code in it is one the test suite or
`scripts/` imports. As of this writing that is `hf3`,
`hf14_ofdm_bpsk_watterson`, `hf15_resilient`, `hf16_mfsk_lowsnr` and
`hr0_fast_control`. Everything else is markdown only.
