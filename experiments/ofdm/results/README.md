# OFDM experiment artifacts

Generated bench evidence lives here so that running experiments does not
clutter the repository root:

- `sweeps/` — JSON summaries from `sweep_ofdm.py`
- `measurements/` — channel-probe and audio-bandwidth JSON
- `captures/` — paired received-audio (`.npy`) and payload (`.bin`) captures (ignored by Git)
- `failures/` — individual failed frames saved for diagnosis (ignored by Git)
- `logs/` — console output from longer sweep runs (ignored by Git)

The artifacts are grouped by run name beneath `captures/`. Paths passed to
`--out` and `--capture-dir` are interpreted relative to the current working
directory, so the commands in the parent README assume they are run from the
repository root.
