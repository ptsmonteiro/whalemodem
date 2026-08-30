# Logs: retention and tracking policy

`logs/` has two tiers. Everything under it is one or the other; nothing lives
loose at `logs/` root.

## `logs/mode_qualification/` -- tracked, permanent

The only evidence a qualification claim in `MODE_QUALIFICATION.md` may cite
by path. Tracked in git via the `.gitignore` allow-list
(`logs/*` then `!logs/mode_qualification/` and `!logs/mode_qualification/**`),
so `git add` picks these files up normally -- no `git add -f` required, and
none should be needed. If a new file under this tree isn't showing up in
`git status`, the allow-list is wrong; fix `.gitignore`, don't force-add
around it.

Layout: `logs/mode_qualification/<channel policy>/<mode group>/<date>/`,
for example `logs/mode_qualification/vhf-fm/cpfsk/2026-08-29/`. `<mode
group>` is a single mode name (`vf3`) or a hyphenated group sharing one
campaign (`hc0-hc1`). `<date>` is the run date; append a short disambiguating
suffix (`2026-08-28-hardware`) when a date needs more than one campaign
directory.

Every campaign directory has exactly one `INDEX.md` alongside its raw
artifacts (`.json` result files, and `.bin`/`.npy` capture pairs for hardware
runs). `INDEX.md` states, at minimum:

- the exact command run (or, for a hardware session, how the capture was
  taken and with what radios/config). For hardware runs this must include the
  RF path on **each** station -- antenna or dummy load, power setting, and
  audio levels -- because a one-directional path deficit is otherwise
  indistinguishable from a waveform failure once the session is over. The
  2026-08-28 HC0/HC1 run is the cautionary case: its weak leg was a dummy
  load, but nothing in the artifact recorded that;
- the git commit and whether the tree was clean or dirty;
- per-point or per-trial results, in enough detail to recompute the verdict;
- which qualification gate(s) this evidence does or does not clear, and why.

A result is retained evidence -- eligible to back a `passed`/`failed` cell in
`MODE_QUALIFICATION.md`'s assessment table -- only once it has an `INDEX.md`
making that claim explicit. A JSON artifact with no `INDEX.md` entry is not
citable; either write the entry or treat the file as scratch and move it out.
Diagnostic or exploratory runs that don't themselves constitute promotion
evidence (e.g. the mode-2 boundary-diagnosis replays) still live here if
they're the record a doc's narrative depends on, but their `INDEX.md` must
say plainly that they are diagnostic-only, not promotion evidence.

Nothing here is ever deleted or overwritten silently. A superseded result
stays on disk with its original date; the doc narrative says which artifact
is current.

## `logs/scratch/` -- gitignored, disposable

Everything else: ad hoc link-harness sessions, one-off debugging runs,
anything written by a tool's default output path. Never cited by path from a
doc -- if a result matters enough to reference later, promote its directory
into `logs/mode_qualification/` with an `INDEX.md`, following the layout
above, rather than pointing at scratch. Safe to delete at any time; nothing
outside the current working session should depend on a `logs/scratch/` path
still existing.

## Adding a new campaign

1. Pick (or create) `logs/mode_qualification/<policy>/<mode group>/<date>/`.
2. Point the run's `--out` (or equivalent) directly at that directory so the
   artifact lands tracked from the start.
3. Write `INDEX.md` in the same change, before or alongside updating
   `MODE_QUALIFICATION.md`'s assessment table -- the table cell should point
   a reader at the `INDEX.md`, not restate its content.
4. Everything else that run produced (console logs, intermediate captures
   not worth keeping) goes to `logs/scratch/` or is discarded.
