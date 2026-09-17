# Triage Automation Evals

Deterministic evaluation of the ConnectWise triage agents, scored from
recorded traces.

Four Foundry agents propose and execute changes to support tickets. Before
pushing more autonomy to production we need to know whether they are behaving
well, and to be able to tell when a change makes them worse. The evaluation
that existed before this was an LLM reading the workflow output with no
reference and declaring it good. This replaces that with checks that read what
the tools actually returned.

`docs/HANDOFF.md` is the full engineering context: what is not built, why the
design is what it is, and where this sits against industry practice. Read it
before changing anything structural.

## Runs are not repeatable — this is the central constraint

The agents mutate the ticket they operate on, and `connectwise-operations-agent`
writes to a system of record. Re-running a triage produces different results
because the first run already changed the data.

**Never point an evaluator at a live agent against production ConnectWise.**

What that rules out is re-running agents against production. It does not rule
out replaying them against **recorded tool output**: `make_cassette.py` and
`replay_server.py` serve an agent the exact responses the recorded run got,
performing no writes and making no ConnectWise request. See `docs/REPLAY.md`.

Only one thing in this repo invokes agents at all — `staging-replay.yml`,
pinned to the test project and the dev ConnectWise instance. Everything else
touches nothing.

## Quickstart

Nothing to install for the frozen sets — the converter and the scorer are
stdlib-only.

```bash
make test        # unit tests + replay both frozen sets against their baselines
make evals       # score the known-good set
make evals-ops   # score the known-bad set
```

By hand:

```bash
python3 trace_to_eval.py traces/2026-09-03-full-triage.csv -o out \
    --tool-defs tool_manifests/
python3 run_evals.py out/eval_runs.jsonl \
    --expected expected.json \
    --baseline baselines/full-triage-2026-09-16.json \
    --json artifacts/run.json
```

`python` not `python3` on Windows.

Only `export_traces.py` needs dependencies: `pip install -r requirements.txt`.

## Layout

```
export_traces.py          Log Analytics -> raw spans, unattended (for CI)
trace_to_eval.py          raw spans -> eval_runs.jsonl, one row per agent run
run_evals.py              scoring, reporting, baseline diff
fetch_tool_manifest.py    tools/list against a Foundry toolbox -> a manifest
extract_tool_manifest.py  a manifest from a tools/list dump, or a trace skeleton
submit_to_foundry.py      the same dataset through the Foundry judged evaluators
make_cassette.py          a recorded trace -> a replay cassette
replay_server.py          an MCP server answering from a cassette; no writes

expected.json             ground-truth trajectories, keyed "<agent>|<intent>"
baselines/                frozen results — COMMIT THESE
traces/                   raw exports, dated, committed
replay/                   dev-instance tickets re-triaged in staging
tool_manifests/           MCP tool schemas (empty — see below)
tests/                    unit tests + frozen-set replay
docs/HANDOFF.md           full engineering context
docs/FOUNDRY.md           what Foundry does for us and what we do ourselves
docs/REPLAY.md            stubbing the tools with recorded output
docs/REPO-BOUNDARY.md     why the agents live in a different repo
```

The Foundry agents live in a **separate repo**. Anything that can change what
an agent does belongs there; anything that only measures belongs here. See
`docs/REPO-BOUNDARY.md`.

`expected.json` and `baselines/` are the real assets. The scripts are
replaceable; the curated expectations and the frozen results are not.

## The pipeline

```
Log Analytics                  export_traces.py    (lag mode, or explicit ids)
  |  raw dependency spans, customDimensions intact
  v
trace_to_eval.py               one JSONL row per AI Run
  |  + tool_manifests/, matched on the run's MCP toolbox version
  v
run_evals.py                   deterministic checks, baseline diff, exit code
```

An **AI Run** is one agent's execution. `operation_Id` is one orchestration;
each distinct `gen_ai.agent.name` inside it is a run.

## Checks

| Check | Gating | What it catches |
|---|---|---|
| `no_wasted_calls` | **yes** | Calls that could not have succeeded: `missing_script`, `invalid_reference_type`, `invalid_entity`, `invalid_projection_field`, `empty_failed` |
| `valid_tool_args` | **yes** | Arguments that violate the tool's own schema — required, type, enum, unexpected. Generated from `tool_manifests/`, skips cleanly without one. |
| `no_dead_ends` | **yes** | Succeeded but returned nothing. The hallucinated-entity signal. |
| `trajectory` | **yes** | In-order match vs ground truth, extras allowed. Reports precision / recall / F1. |
| `no_tool_errors` | info | Any error. Too broad to gate — a legitimately empty result is not a defect. |
| `no_search_cascade` | info | Four or more consecutive fruitless calls to one tool. Distinct from one bad call, and it burns the most time. |
| `no_truncation` | info | Results at exactly 8192 chars, cut mid-payload. Telemetry problem, not model. A truncated *skill* is called out separately — incomplete rules, not incomplete data. |
| `cost_latency` | info | Tokens and wall clock. **Tracked always, gated only if you set `--max-tokens` / `--max-duration-ms`.** |
| `evaluator_ready` | info | Would Foundry evaluators accept this run. Dataset readiness. |

Adding one is a function returning `_pass()`, `_fail(reason)` or
`_skip(reason)`, registered in the `CHECKS` dict in `run_evals.py`. `_skip`
keeps a run out of the denominator so a missing expectation does not read as a
failure. Move checks between gating and informational as you learn what is
actionable.

Keep check *logic* as code and check *expectations* as data. Do not build a
generic "evaluate any agent" framework; that ends as abstraction fitting
nothing with a config language nobody reads.

## Exit codes

- **Without `--baseline`** — any gating failure exits 1.
- **With `--baseline`** — only a *regression* exits 1, plus **lost coverage**:
  a check that produced a verdict in the baseline and now skips. That case
  looks like silence rather than a failure, and it is how the intent-keying
  break went unnoticed.

The second is the CI mode. Gate on delta while known issues are open, or the
suite is red permanently and people route around it.

## Branch to Foundry project

| Branch | Environment | Project | Invokes agents? |
|---|---|---|---|
| any | — | none | no — frozen sets only |
| `staging` | `staging` | `automation-solutions-test` | **yes**, via `staging-replay.yml` |
| `main` | `prod` | `automation-solutions` | **never** |

Cassette replay (`docs/REPLAY.md`) invokes no agents from this repo and touches
no ConnectWise at all, so it is safe to build from production traces on any
branch.

There is no production replay and there must not be: invoking agents re-triages
real tickets and the ops agent writes to the system of record. Production is
evaluated from recorded traces only. Full detail, plus judge model choice, in
`docs/FOUNDRY.md`.

## What is not built

| Gap | Blocks | Where |
|---|---|---|
| **MCP tool manifest** | 3 Foundry evaluators, generated arg validation | `tool_manifests/README.md`. Plumbing and two extraction paths are done; the schemas are not. Try `AIAgentConverter` first — it may close this outright. |
| **Dataverse loading on the new pipeline** | All outcome evaluation | Handoff §8. Has lead time; nothing about outcome quality is answerable until it has been running a while. |
| `replay/` ticket ids | The staging agent-change gate | `replay/README.md` — the workflow is written, the dev ticket ids are placeholders. |
| Cost / latency budgets | Gating on spend | Tracked now; set `--max-tokens` / `--max-duration-ms` once you know what normal looks like. |
| Skill versions | "Which rules were in force" across versions | `load_skill` returns no version. Hashing is the workaround; the fix is agent-side. |
| Intent on the ops hand-off | Intent-keyed expectations for the ops agent | The orchestrator hands it a JSON write plan with no `intent=`, so those runs key on the bare agent name. |

## Gotchas

- **The export must keep `customDimensions` intact.** A flattening projection
  strips every `gen_ai.*` attribute.
- **JSON export is safer than CSV.** CSV escaping of `customDimensions` has
  caused parse failures.
- **Adding an agent means updating `AGENT_NAMES` in `trace_to_eval.py`**, or
  its runs silently collapse into the caller's trajectory.
- **Every MCP call emits two spans** — `execute_tool <x>` carries the payload,
  `tools/call <x>` is empty, and both set
  `gen_ai.operation.name = execute_tool`. Filter on the span *name*.
- **Grouping is by `gen_ai.agent.name`, not the span tree.** A child agent's
  spans hang off a parent id outside the caller's subtree.
- **`invoke_agent` spans carry a roll-up of token usage.** Counting them and
  the chat spans doubles every figure.
- **App Insights property cap is 8192 chars.** `gen_ai.*` attributes are
  largely exempt (59,270 observed) but some paths still truncate at exactly
  8192 — treat `res_len == 8192` as suspect.
- **Foundry Traces retains 90 days**; Dataverse rows are permanent.

- **`load_skill` returns no version** — only `name` and `description` in the
  frontmatter. Content hash is the only identity a historical run has, and a
  truncated body hashes to the truncation rather than the skill.
- **`ToolCallAccuracyEvaluator` returns _pass_ for tool types it does not
  support.** Never let it be the gate. See `docs/FOUNDRY.md`.

`tests/` locks in every one of these. If you are about to simplify one away,
reproduce the case in a trace first.

## Environment

- App Insights: `automation-solutions-resource-appinsights` (rg `Verve-CopilotCapacity`)
- Foundry project: `automation-solutions`
- MCP toolbox: `ConnectwiseMCP`. The number in the toolbox URL is a **binding
  revision**, not a schema version — the ops agent shows v1 and the analysis
  agent v5 for the same toolbox, with byte-identical tool descriptions. A
  manifest normally declares `"versions": ["*"]`.
- Content recording is **ON**
