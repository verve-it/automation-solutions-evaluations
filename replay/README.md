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

## Which agent do you name?

`agent-ids` is `agent-name:version`, and it must match the **input contract of
the queries in the data file**.

`full-triage.json` holds **orchestrator-shaped** queries:

```
entityType=ticket
entityId=805392
context=Automated flow: triage ticket and automatically approve writeplan
```

So `agent-ids` is `triage-orchestrator:<version>`. The orchestrator invokes
`triage-analysis-agent`, `triage-evaluation-agent` and
`connectwise-operations-agent` itself — you do not list them, and listing them
would send each of them an input contract it does not accept.

To evaluate a child agent on its own, give it its own data file with its own
input shape:

| Agent | Query shape | Data file |
|---|---|---|
| `triage-orchestrator` | `entityType=ticket\nentityId=…\ncontext=…` | `full-triage.json` |
| `triage-analysis-agent` | `intent=Full Triage; ticketId=…; mode=Automation; context=…` | not written yet |
| `triage-evaluation-agent` | `intent=Write Request; ticketId=…; …` | not written yet |
| `connectwise-operations-agent` | a JSON write plan | not written yet |

Those shapes come straight from the recorded hand-offs in
`traces/2026-09-03-full-triage.csv`; copy one and change the ticket id.

Where the version comes from: the Foundry project's agent list. The recorded
traces carry it as `gen_ai.agent.version` — orchestrator 45, analysis 82,
evaluation 20, ops 16 at the time of the September traces.

`baseline-agent-id` is the version you are comparing against, normally the one
currently deployed. With it, the action reports whether a difference is
statistically significant rather than noise. Without it you get scores but no
significance test.

Set `DEFAULT_AGENT_IDS` (and optionally `DEFAULT_BASELINE_AGENT_ID`) on the
`staging` GitHub environment so a push to the `staging` branch has something to
run against. A dispatch or a `repository_dispatch` from the agents repo
overrides it.

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
