# Triage Automation Evals — Engineering Handoff

> **Kept as written, as the record of where this stood on 2026-09-16.**
> `README.md` is the current state. Since this was written:
>
> - §10 item 1 (unattended trace export) is built — `export_traces.py`.
> - §10 item 2 (MCP manifest) is *plumbed*, not filled: the converter takes
>   `--tool-defs`, `run_evals.py` generates argument validation from it, and
>   `extract_tool_manifest.py` builds the file. The schemas themselves are
>   still missing. See `tool_manifests/README.md`.
> - §10 item 3 (CI wiring) is built — `.github/workflows/evals.yml`.
> - §10 item 5 (cascade check) is built — `no_search_cascade`, informational.
> - §10 item 6: token data is now collected per run; no threshold is enforced.
> - The §2 numbers below were measured with two bugs live. `started` was empty
>   on every run (the portal names the column `timestamp [UTC]`), and intent
>   extraction never matched, because real hand-offs are `intent=Full Triage; …`
>   and the pattern required `intent:`. The trajectory check was therefore
>   skipping every run while the baseline diff read "no change". Both are
>   fixed; baselines re-frozen; the gating totals (5/7 and 0/2) are unchanged.
> - §11 says the toolbox is ConnectwiseMCP v5. It is v5 for
>   `triage-analysis-agent` and **v1** for `connectwise-operations-agent`.

Everything needed to pick this up. Written for someone who has the repo but
none of the history.

**Status:** working local eval pipeline, first baseline frozen, validated
against real production traces. Not yet automated, not yet in CI, not yet
connected to human ground truth.

---

## 1. What this is and why it exists

We run a multi-agent AI triage system against ConnectWise PSA. Four Foundry
agents propose and execute changes to support tickets. Before pushing more
autonomy to production we need to know whether the agents are behaving well,
and be able to tell when a change makes them worse.

The evaluation we had before this work was an LLM reading the workflow output
with no reference and declaring it good. That is ungrounded and told us
nothing. This project replaces it with checks that read what the tools
actually returned.

### The system under evaluation

| Agent | Role | Writes to ConnectWise |
|---|---|---|
| `triage-orchestrator` | Routes, preserves state, builds write plans, owns approval. Makes no business decisions. | No |
| `triage-analysis-agent` | Normalization + classification. Produces the proposed changes. | No |
| `triage-evaluation-agent` | Closure readiness, monitoring, lifecycle. | No |
| `connectwise-operations-agent` | Validates and executes approved writes. | **Yes** |

The orchestrator calls the others as A2A tools. All four share one MCP toolbox
(`ConnectwiseMCP`, currently version 5) and a set of skill files loaded at
runtime via `load_skill`.

### Critical constraint: runs are not repeatable

The agents mutate the ticket they operate on. Re-running the same triage
produces different results because the first run already changed the data, and
the ops agent writes to a system of record. **Evaluation is scoring of
recorded traces, permanently. Never point an evaluator at a live agent
against production ConnectWise.**

This is why there is no replay harness and no stub layer, and why that is the
correct design rather than a gap.

---

## 2. Current state

### Done

- Trace → eval dataset converter (`trace_to_eval.py`)
- Six deterministic checks + baseline diffing (`run_evals.py`)
- Baseline frozen: `baselines/baseline-2026-09-16.json`
- Every check validated by firing on real production failures
- Intent-aware trajectory keying

### Baseline numbers (2026-09-16, 2 orchestrations, 7 agent runs, 715 spans)

```
5/7 runs pass gating checks
20/115 tool calls errored (17%)
 5 avoidable calls: 2 empty_failed, 1 invalid_reference_type,
                    1 invalid_entity, 1 missing_script
 7 truncated tool results (all on triage-analysis-agent)
 trajectory 4/4 matched
 no_dead_ends 7/7 clean (fires correctly on the known-bad set)
```

A second frozen set exists for the worst-case ops runs — `no_wasted_calls`
0/2 there. Keep both: a regression suite needs a known-good and a known-bad
baseline. If a change makes the bad case start passing, suspect the check
before celebrating.

### Not done

| Gap | Blocks | Notes |
|---|---|---|
| Unattended trace export | CI, scheduled runs | Portal blade only today. Needs Log Analytics API. ~20 lines. |
| MCP tool manifest | 3 Foundry evaluators, generated arg validation | Highest leverage remaining item. See §6. |
| Dataverse loading on new pipeline | All outcome evaluation | Has lead time — reviewers need runs queued before they can review. |
| Foundry submission | Portal visibility, LLM-judged evaluators | Needs manifest first. |
| Cost/latency gates | Regression on spend | Token data already in traces, unused. |
| Statistical treatment | Distinguishing flaky from broken | Single runs only; no pass@k. |

---

## 3. Repository layout

```
triage-automation-evals/
  trace_to_eval.py          # trace export -> eval_runs.jsonl (one row per agent run)
  run_evals.py              # scoring, reporting, baseline diff
  expected.json             # ground-truth trajectories, keyed "<agent>|<intent>"
  traces/                   # raw exports, dated
  out/                      # converter output (gitignore)
  baselines/                # frozen results (COMMIT THESE)
```

`expected.json` and `baselines/` are the real assets. The scripts are
replaceable; the curated expectations and frozen results are not.

### Should the Foundry agents live in this repo?

**No. Keep them separate.** Reasons, in order of weight:

1. **Different release cadence and blast radius.** An agent change can write
   bad data to ConnectWise. An eval change cannot. They should not share a
   pipeline, approval path, or rollback story.
2. **Circular gating.** If the evals gate the agents and live beside them, a
   broken eval blocks agent hotfixes and someone will disable the gate under
   pressure. Separation keeps the gate credible.
3. **Different consumers.** Evals should eventually cover agents this team
   does not own. A shared repo makes that awkward.

**What crosses the boundary:** the evals repo needs to know agent names, skill
versions, and the MCP tool manifest. Get those by version-pinned artifact
(published manifest, tagged skill bundle), not by reaching into the agents
repo. Skill content should be referenced by hash so "which rules were in
force" is answerable for any historical run.

**Exception worth considering:** a thin contract package the agents repo
publishes and the evals repo consumes — agent names, intent enum, tool
manifest, skill hashes. One artifact, versioned, no source coupling.

---

## 4. How the pipeline works

### Converter (`trace_to_eval.py`)

Input is a raw span export from App Insights. Output is one JSONL row per
**AI Run**, where an AI Run is one agent's execution.

```powershell
python trace_to_eval.py .\traces\query_data.csv -o .\out
python run_evals.py .\out\eval_runs.jsonl --expected .\expected.json `
                   --baseline .\baselines\baseline-2026-09-16.json
```

Export query — keep `customDimensions` intact, a flattening projection strips
every `gen_ai.*` attribute:

```kusto
let ids = dynamic(["<operation_id>", "..."]);
dependencies
| where timestamp > ago(30d)
| where operation_Id in (ids)
| project timestamp, name, id, operation_Id, operation_ParentId,
          duration, success, customDimensions
| order by timestamp asc
```

#### Five non-obvious things the converter handles

Each of these was a real bug found against real data. Do not "simplify" them
away without reproducing the case first.

1. **Duplicate tool spans.** Every MCP call emits both `execute_tool <x>`
   (parent, carries args/result) and `tools/call <x>` (child, empty). Both set
   `gen_ai.operation.name = execute_tool`. Filtering on operation name double
   counts the entire trajectory. **Filter on span name:
   `name startswith "execute_tool"`.**

2. **Grouping is by `gen_ai.agent.name`, not the span tree.** A child agent's
   spans hang off a parent id outside the caller's subtree. Tree-walking loses
   them. Agent name is correct on every span.

3. **`call_tool` is a dispatcher.** The ops agent wraps real calls as
   `{"name": "<real tool>", "arguments": {...}}`. Unwrapped, its whole
   trajectory reads as `call_tool` and no evaluator can see which ConnectWise
   operation ran.

4. **A2A calls are run boundaries.** A tool whose name matches a known agent
   (`AGENT_NAMES` at the top of the file) stays as a step in the *caller's*
   trajectory — routing is the orchestrator's job and should be scored — and
   the callee becomes its own AI Run. Adding an agent without updating
   `AGENT_NAMES` silently collapses it into its caller.

5. **Empty result + failed span is a real failure.** `cw_resolve` returns
   *nothing at all* for unsupported reference types. Span status is the only
   signal. Classified as `empty_failed`.

#### Intent extraction

Trajectories are keyed `"<agent>|<intent>"` because the same agent has
different correct paths per intent — the orchestrator routes to a child for
Full Triage but goes straight to MCP for an Information Request.

- `declared` — the run's own input carries `intent: <x>`
- `delegated` — inferred from an A2A call's arguments (the orchestrator's case;
  its own input is free text)
- `unknown` — neither; intent stays `None` and is never guessed

A bare `"<agent>"` key still works and applies to all intents.

### Checks (`run_evals.py`)

| Check | Gating | What it catches |
|---|---|---|
| `no_wasted_calls` | **yes** | Calls that could not have succeeded: `missing_script`, `invalid_reference_type`, `invalid_entity`, `invalid_projection_field`, `empty_failed` |
| `no_dead_ends` | **yes** | Succeeded but returned nothing. The hallucinated-entity signal. |
| `trajectory` | **yes** | In-order match vs ground truth, extras allowed. Reports precision/recall/F1. |
| `no_tool_errors` | info | Any error. Too broad to gate — a legitimately empty result is not a defect. |
| `no_truncation` | info | Results at exactly 8192 chars, cut mid-payload. Telemetry problem, not model. |
| `evaluator_ready` | info | Would Foundry evaluators accept this run. Dataset readiness. |

Adding a check is one function returning `_pass()`, `_fail(reason)`, or
`_skip(reason)`, registered in the `CHECKS` dict. `_skip` keeps a run out of
the denominator so a missing expectation does not read as failure.

Move checks between gating and informational as you learn what is actionable.

### Exit codes

- **Without `--baseline`:** any gating failure → 1
- **With `--baseline`:** only *regressions* → 1

The second is the CI mode. Known-failing runs do not block unrelated work, but
anything that used to pass and now fails goes red.

---

## 5. CI gating — recommended design

Not built yet. This is the intended shape.

### Two triggers

**On change (merge gate).** Agent prompt, skill file, or MCP version changes →
export recent traces → convert → diff against baseline → block on regression.

**Scheduled (drift detection).** Nightly over the last 24h. Catches what no
change of ours caused: ConnectWise schema changes, MCP version bumps, model
drift. The `cw_resolve` failures described below would have been caught the
day they started.

### The trace-availability problem

A skill change does not produce traces by itself. Something must run the
agents. Two options:

- **Replay set** — a fixed list of ticket IDs re-triaged in a non-production
  environment on each change. Clean signal, requires a dev ConnectWise
  instance so writes are safe.
- **Lag** — evaluate whatever production produced since the last run. No
  infrastructure, but the gate runs after the change is already live.

Given the mutation constraint, start with lag and add a replay set once a dev
instance exists.

### Gate thresholds

Do not gate on absolute pass rates while known issues are open — the suite
will be red permanently and people will route around it. Gate on **delta**:
regressions fail, existing failures do not. That is what `--baseline` already
implements.

Once the known issues are fixed, tighten to absolutes.

### Suggested pipeline

```
1. az login (workload identity)
2. Query Log Analytics for traces since last run       <- NEEDS BUILDING
3. python trace_to_eval.py <export> -o ./out
4. python run_evals.py ./out/eval_runs.jsonl \
       --expected expected.json \
       --baseline baselines/<current>.json \
       --json artifacts/run-$(date).json
5. Publish artifacts/ and the printed report
6. Non-zero exit fails the build
```

Step 2 is the only missing piece. `azure-monitor-query` Python SDK or
`az monitor log-analytics query`. Needs a workspace-reader identity.

### Promoting a baseline

When a change improves things, the new results become the baseline. Commit it
with the change that caused it so the history explains itself. Keep old
baselines — they are the record of how the system behaved over time.

---

## 6. The MCP manifest — highest-value remaining work

`gen_ai.tool.definitions` **only covers A2A agent registrations.** On the
orchestrator it contains exactly three entries (the child agents). No
`load_skill`, no ConnectWise tools. Child agents have no A2A children so the
attribute is absent entirely — which is why `evaluator_ready` fails on
5 of 7 runs.

Consequence: **no schema exists in telemetry for any ConnectWise call.** Tool
Input Accuracy and Tool Output Utilization cannot run anywhere.

### Why it matters more than it sounds

With the manifest in hand, argument validation becomes **generated rather than
written** — required params, types, enums, unexpected fields, per tool,
automatically. That is the same six criteria Tool Input Accuracy checks, but
deterministic and free.

Every `cw_resolve` failure in the baseline would have been caught by a
generated check, and so would every future one, across every agent and every
flow, with nobody writing a check. It is the difference between an eval suite
that grows linearly with the agent count and one that does not.

### How to get it

The toolbox is versioned (`ConnectwiseMCP/versions/5`), so this is a one-time
extraction per version, not per-run capture.

1. Check whether the `tools/list` span already carries it:
   ```kusto
   dependencies
   | where name has "tools/list"
   | extend d = customDimensions
   | project timestamp, name, keys = bag_keys(d), dims = d
   | take 5
   ```
2. If not, export the toolbox definition from the Foundry portal, or capture
   the raw `tools/list` JSON-RPC response from the MCP endpoint.
3. Store as `tool_manifests/connectwise-v5.json`. Add a `--tool-defs` flag to
   the converter to inject it as `tool_definitions` at convert time, keyed by
   the version in the span URL.

Version-key it. Scoring old behaviour against new schemas silently corrupts
results.

---

## 7. Findings the baseline already produced

These are real defects found by the eval before it was finished. Useful both
as a demonstration of value and as work items.

1. **`cw_resolve` does not support `type`, `subtype`, `item`, `site`,
   `impact`, `urgency`.** 8 wasted calls across two ops runs. Two failure
   shapes for the same root cause: some return a text error
   (`Unknown reference type 'impact'`), others return empty with a failed
   span. The agent guesses at the vocabulary because it has no schema — a
   symptom of the manifest gap. *Fix: document supported types in the ops
   agent skill, or have the tool return a structured "supported types are…"
   instead of an empty failure.*

2. **`run_skill_script` probing.** The analysis agent calls it with
   `script_name: "?"` and `"none"` against skills whose manifest shows
   `<available_scripts />` empty. Three guaranteed failures, ~6s per triage.
   *Fix: one prompt line — if the skill has no scripts, do not call it.*

3. **Cascading dead-end search.** The ops agent resolved
   `"A.S. Economou Development"` → empty, `"4597"` → empty, `"A."` → empty,
   then queried companies directly → empty. An upstream agent proposed a
   company that does not exist in ConnectWise. The write correctly refused,
   but burned ~34 calls discovering it. *Worth its own check: repeated calls
   to one tool with degrading arguments is a distinct signature from one bad
   call, and it burns the most time.*

4. **Truncation at 8192 chars.** 7 results cut mid-payload, all on the
   analysis agent, meaning it reasoned over incomplete data. `load_skill` is
   the largest payload (25,662 chars max). *Fix: store skills by reference —
   replace the response body with `{skill_name, version, sha256}` and rehydrate
   at eval time. Also enables comparing runs across skill versions and makes
   Task Adherence answerable, since skill files are the operative rules.*

5. **`connectwise-operations-agent` is the least reliable and highest risk.**
   Across four observed runs: invalid reference types, invalid entities,
   invalid projection fields, 29% dead-end rate. It is the only agent that
   writes to a system of record. *Priority for both fixes and eval coverage.*

---

## 8. What is NOT evaluated, and why it matters

**No outcome evaluation.** Every current check is process quality — did the
agent behave tidily. None measure whether it was *right*. A run can pass all
six checks having proposed entirely the wrong company.

The blocker is that trace data and ground truth are currently disjoint
populations:

| | tool trajectory | human verdict |
|---|---|---|
| New agents (28 linked traces) | yes | **no** |
| Old pipeline (bulk volume) | **no** | yes |

The new architecture is not writing to Dataverse yet. All human-reviewed
decisions sit on the old pipeline, which made no tool calls at all — data was
pushed in rather than fetched.

**Turning on Dataverse loading for the new pipeline is the single highest-value
non-code action.** It has lead time: reviewers need runs queued before they can
review, and reviews lag runs by days. Nothing about outcome quality is
answerable until that pipeline has been running for a while.

### The Dataverse schema (already built, in production)

```
AI Orchestration   one workflow run against one entity (one ticket)
  └─ AI Run        one agent execution        (4 per orchestration typically)
       └─ AI Decision   one proposed field change
            └─ AI Review   human disposition + downstream execution (1:1, optional)
```

Ground truth for outcome evaluation comes from AI Review: the original AI
suggestion is snapshotted, the human's final value recorded alongside, with a
reason code for *why* it changed. Those reason codes (Human Corrected
Classification, Source Data Incorrect, Business Rule Exception, …) are already
a failure taxonomy the reviewers have been using for months. **Operationalise
that taxonomy rather than inventing new metrics.**

Note the three-way label is more useful than it looks:
- approved, no override → gold trajectory *and* gold outcome
- **modified, small changes → gold trajectory, corrected outcome**
- rejected → negative example

The middle bucket is diagnostic. Clean trajectories there mean tools and
skills are fine and synthesis is the gap. Messy trajectories mean the reverse.

---

## 9. Where this sits against industry practice

**Ahead of most:** evaluates trajectory and tool arguments, not just final
output. Deterministic, so no judge-reliability problem. Real per-tool failure
taxonomy. Most agent "evals" are an LLM judging a response string — which is
exactly what we replaced.

**Behind:** no ground truth (see §8). No adversarial testing — Foundry ships an
AI red teaming agent (PyRIT-based) that matters more once autonomous writes are
live. No cost/latency gate despite having token data in the traces. No
statistical treatment: single runs, no pass@k, so a flaky agent and a broken
one look identical.

The honest summary: we have the half most teams skip and lack the half most
teams claim to have. Pre-production, that is the right order — process quality
is fixable now, outcome quality needs the review queue running regardless.

### Foundry migration — when and how

Foundry evaluation is a **second surface over the same dataset**, not a
replacement.

Gains: portal run history and comparison, LLM-judged evaluators we cannot write
ourselves (Task Adherence, Intent Resolution, Task Completion), continuous
evaluation sampling live traffic, a place non-engineers can look.

Costs: judge model deployment, per-evaluation inference spend,
`azure-ai-projects` in the loop, and the manifest gap fixed first.

Intended split once both exist: **deterministic checks locally as the merge
gate** (fast, free, no judge variance); **judged evaluators in Foundry on a
sample**, nightly or weekly. Do not run LLM judges per commit — slow, and the
scores wobble.

`eval_runs.jsonl` is already the right shape for the Foundry data source, and
`check_trajectory` deliberately reports precision/recall/F1 the way Task
Navigation Efficiency does, so numbers stay comparable across the move. The
migration is a runner swap, not a rewrite.

Prerequisites for continuous evaluation specifically: App Insights connected to
the Foundry project, and the project managed identity holding the Foundry User
role.

---

## 10. Suggested order of work

1. **Unattended trace export** — unblocks CI and scheduled runs. Smallest
   piece, largest unlock.
2. **MCP tool manifest** — turns argument validation from written to
   generated. See §6.
3. **CI wiring** — regression-only gating per §5.
4. **Skill-by-reference** — fixes truncation structurally, enables skill
   version comparison and Task Adherence.
5. **Cascade check** — repeated calls with degrading arguments (§7 item 3).
6. **Cost/latency gate** — token data is already in the traces.
7. **Foundry submission** — after 2. Sampled, not per-commit.
8. **Outcome evaluation** — after Dataverse loading has accumulated reviews.

Items 1–3 are a coherent first sprint.

---

## 11. Reference

### Layering — how to think about what is reusable

| Layer | Reuse | Examples |
|---|---|---|
| **1. Hygiene** | Every agent, forever | errors, dead ends, truncation, wasted calls |
| **2. Tool contract** | Every agent sharing a tool surface | supported reference types, valid entities, valid projection fields |
| **3. Trajectory** | Per workflow + intent | `expected.json` |
| **4. Outcome** | Per workflow, needs labels | vs human final value |

Layers 1–2 are ~80% of check code and written once. Layers 3–4 are ~20% and
written per flow. The unit of evaluation is **the agent, not the workflow** —
which is why a new workflow composing existing agents inherits layers 1–2 free.

Keep check *logic* as code and check *expectations* as data. Do not build a
generic "evaluate any agent" framework; that ends as abstraction fitting
nothing with a config language nobody reads.

### Useful KQL

Find evaluable traces, ranked:

```kusto
let newAgents = dynamic(["triage-orchestrator","triage-analysis-agent",
                         "connectwise-operations-agent","triage-evaluation-agent"]);
dependencies
| where timestamp > ago(14d)
| extend d = customDimensions
| extend agent = tostring(d["gen_ai.agent.name"])
| where agent in (newAgents)
| summarize started = min(timestamp), agents = make_set(agent),
            agent_count = dcount(agent), spans = count(),
            tool_calls = countif(name startswith "execute_tool"),
            has_tool_defs = countif(isnotempty(tostring(d["gen_ai.tool.definitions"])))
  by operation_Id
| extend usable = case(agent_count > 1 and tool_calls > 0 and has_tool_defs > 0, "1-full",
                       tool_calls > 0 and has_tool_defs > 0, "2-single agent",
                       tool_calls > 0, "3-no tool defs", "4-thin")
| order by usable asc, started desc
```

### Environment facts

- App Insights: `automation-solutions-resource-appinsights`
  (rg `Verve-CopilotCapacity`)
- Foundry project: `automation-solutions`
- Models seen: `gpt-5.6-luna`
- MCP toolbox: `ConnectwiseMCP` version 5
- Content recording is **ON** (312/443 tool spans carry arguments)
- Trace propagation works — 28 linked multi-agent traces available
- App Insights property cap is 8192 chars; `gen_ai.*` attributes are largely
  exempt (values to 59,270 observed) but some paths still truncate at exactly
  8192 — treat `res_len == 8192` as suspect
- Foundry Traces view retains **90 days**; Dataverse rows are permanent.
  Materialise eval rows on a rolling basis or old traces age out before their
  reviews land.

### Gotchas

- Export must keep `customDimensions` intact — a flattening projection strips
  every `gen_ai.*` attribute.
- JSON export is safer than CSV for nested data; CSV escaping of
  `customDimensions` has caused parse failures.
- `python` not `python3` on Windows.
- Adding an agent means updating `AGENT_NAMES` in `trace_to_eval.py`, or its
  runs silently collapse into the caller's trajectory.
