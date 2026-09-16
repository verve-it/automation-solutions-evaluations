# Replay set

A fixed list of dev-instance tickets re-triaged on each agent change, scored by
the [`microsoft/ai-agent-evals`](https://github.com/microsoft/ai-agent-evals)
action against **`automation-solutions-test`**, from the `staging` branch.

## Why this is separate from everything else here

Every other evaluation in this repo scores **recorded** traces. This one
**invokes the agents**. That is only safe because `automation-solutions-test`
is wired to the dev ConnectWise instance.

**There is no `main` counterpart and there must not be.** Against
`automation-solutions` this would re-triage real tickets, and
`connectwise-operations-agent` would write the results into the system of
record. `staging-replay.yml` hard-codes the required project name rather than
reading it from a variable, so a mis-set environment variable cannot redirect
it. That constraint is what makes the rest of this repo trace-scoring rather
than a replay harness.

## Why the action rather than our own harness

It invokes the agents from this data file, runs any evaluator in the Foundry
catalog, and reports **confidence intervals and statistical significance**
against a baseline agent version. That is handoff §10 item 9 — distinguishing
a flaky agent from a broken one — for no code. Set `baseline-agent-id` to the
currently deployed version and the report says whether a change is real or
noise.

## Tickets are single-use

A ticket is only useful once: after the first run it has been triaged, so its
state no longer matches what the case was meant to exercise. Either re-seed
the dev tickets before each run, or keep a pool large enough to rotate.

## Populating

Replace each `TICKET_ID_*` with a real dev ticket chosen to exercise that
intent. Aim for coverage of the orchestrator's intent enum first, then add a
regression case for each finding in §7 of `docs/HANDOFF.md` as it is fixed —
that is how this set earns its keep over time.

Fields prefixed `_` are notes for us; the action ignores them.

The workflow fails the run if any `TICKET_ID_` placeholder is still in the
file, so a half-populated set cannot look like a passing gate.
