# Baselines

Frozen scoring results. **Commit these.** With `expected.json` they are the
real assets in this repo — the scripts are replaceable, the curated
expectations and the record of how the system behaved are not.

```
<trace-set>-<YYYY-MM-DD>.json
```

The trace set is the file in `traces/` the baseline was produced from. One
baseline per trace set: a regression suite needs a known-good and a known-bad
case, and they move independently.

## Current

| Baseline | Trace set | Gating | What it is for |
|---|---|---|---|
| `full-triage-2026-09-16.json` | `traces/2026-09-03-full-triage.csv` | 5/7 runs pass | Known-good. Two full orchestrations, seven agent runs, 715 spans. |
| `ops-worst-case-2026-09-16.json` | `traces/2026-09-15-ops-worst-case.csv` | 0/2 runs pass | Known-bad. The two worst observed ops runs. |

Keep both. **If a change makes the known-bad set start passing, suspect the
check before celebrating.**

## Regenerating

```bash
make baselines        # rewrites both from the committed traces
```

## Promoting

When a change improves things, the new results become the baseline. Commit the
new baseline **in the same commit as the change that caused it**, so the
history explains itself. Never rewrite a baseline in place under its old date —
add a new dated file and retire the old one to `archive/`.

`run_evals.py --baseline` gates on **delta**: regressions fail the build,
existing failures do not, and a check that used to produce a verdict and now
skips fails as lost coverage. Gating on absolute pass rates while known issues
are open makes the suite permanently red and people route around it. Tighten to
absolutes once §7 of `docs/HANDOFF.md` is cleared.

## archive/

Superseded baselines, kept because they are the record of how the system
behaved at the time.

- `baseline-2026-09-16-pre-intent-fix.json` — the first frozen set. Produced
  before the converter recognised the portal's `timestamp [UTC]` column or the
  `intent=<x>` hand-off format, so every row has an empty `started` and no
  `intent`/`traj_key`, and its trajectory verdicts came from bare-agent keys.
  Superseded by `full-triage-2026-09-16.json`, which covers the same traces.
