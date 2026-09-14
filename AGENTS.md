# Agent instructions

- Keep documentation short and current.
- Document only behavior that is shipped in `whale/`.
- Do not add mode contracts, qualification frameworks, experiment histories,
  design diaries, or speculative future documentation.
- When a shipped mode changes, update `docs/MODES.md` with only its name,
  channel, net application bit/s per full-capacity DATA frame, simulated pure-
  SNR pass points, simulated Watterson pass points when applicable, and
  calibrated radio-test SNR pass points when available.
- Use `not measured` when evidence is absent or is not an SNR measurement.
- Cross-check code and `docs/MODES.md` before changing a documented value.
  Update only the affected entry.
- Every FM mode module's docstring keeps one line stating its simulated flat
  FM C/N floor (`not measured` if absent), matching the value in
  `docs/MODES.md`. Update it whenever the simulated or radio result changes.
