# qpsk29 experiment artifacts

- `sweeps/` — tracked JSON summaries from `run_qpsk29.py`
- `captures/` — paired received-audio (`.npy`) and payload (`.bin`) captures (ignored by Git)
- `failures/` — individual failed frames saved for diagnosis (ignored by Git)
- `logs/` — console output from longer runs (ignored by Git)

Paths passed to `--out` and `--capture-dir` are interpreted relative to the
current working directory, so the commands in the parent README assume they are
run from the repository root.
