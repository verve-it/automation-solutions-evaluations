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
| `full-triage-2026-09-18.json` | `traces/2026-09-03-full-triage.json` | 5/7 runs pass | Known-good. Two full orchestrations, seven agent runs, 715 spans. |
| `ops-worst-case-2026-09-18.json` | `traces/2026-09-15-ops-worst-case.json` | 0/2 runs pass | Known-bad. The two worst observed ops runs. |

Re-frozen 2026-09-18 when the traces moved to their scrubbed JSON forms. Two
things changed and both are improvements, not drift:

- `no_truncation` goes 5/7 → 7/7 on the known-good set. The JSON export
  carries the `AppGenAIContent` join, so tool results are no longer cut at
  8192 chars. The truncation was a telemetry artifact, never agent behaviour.
- `valid_tool_args` goes 6/6 → 5/6, catching `reference_type='severity'`
  from the tool schema. See `tool_manifests/README.md`.

Gating verdicts are otherwise unchanged, and the ops scrub changed no verdict
at all — `--verify` confirmed 18 identical before the file was written.

## Superseded

Four earlier baselines were removed once nothing referenced them:
`baseline-2026-09-16-pre-intent-fix.json` (before the converter recognised the
portal's `timestamp [UTC]` column or the `intent=<x>` hand-off format, so
every row had an empty `started` and trajectory verdicts came from bare-agent
keys), `full-triage-2026-09-16.json`, `full-triage-2026-09-17.json` and
`ops-worst-case-2026-09-16.json`.

They are in git history if a verdict ever needs tracing back. Keeping them in
the tree invited diffing against one by accident, which is a regression report
that means nothing.

The current pair is `full-triage-2026-09-18.json` and
`ops-worst-case-2026-09-18.json` — what the Makefile, `tasks.ps1` and
`evals.yml` all name.
