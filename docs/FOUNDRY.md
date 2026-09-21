# What Foundry does for us, and what we do ourselves

Written to answer one question: *are we hand-rolling anything Foundry offers
for little effort?*

## Three surfaces, three jobs

| Surface | What it scores | Data source | Where it runs | Judge cost |
|---|---|---|---|---|
| **Deterministic checks** (`run_evals.py`) | Process quality — wasted calls, dead ends, cascades, argument validity, trajectory | Recorded production traces | CI merge gate, nightly drift | none |
| **Cassette replay** (`docs/REPLAY.md`) | Whether a changed agent still takes the recorded path, and where it diverges | Agents invoked against **recorded tool output** | Agent change | none |
| **`microsoft/ai-agent-evals`** | Foundry evaluator catalog, with confidence intervals and significance vs a baseline agent version | Agents **invoked live** with a query set | Before release, **test project only** | per run |
| **Foundry continuous evaluation** | Judged metrics on live traffic at a sampling rate | Production traces, sampled | Production, always on | per sampled run |

They are not alternatives. The first scores what production actually did; the
second scores what a changed agent *would* do, deterministically and for free;
the third does the same live, at the cost of drifting data and real writes; the
fourth watches for drift without anyone asking.

Cassette replay is the per-change gate. The live staging replay drops to a
weekly smoke test — it is the slowest, the most expensive, the least
repeatable, and the only one that leaves state behind.

## What we handed to Foundry

- **Statistical treatment** (handoff §10.9). The action reports confidence
  intervals and tests for significance against `baseline-agent-id`. We are not
  writing pass@k.
- **The replay harness** (§5). The action invokes agents from `replay/`. With
  staging wired to dev ConnectWise there is no harness to write — this was the
  most expensive open item in the handoff and it costs nothing now.
- **Judged evaluators** (§10.7). Task Adherence, Intent Resolution and
  Relevance are LLM-judged and we could not write them ourselves.
- **Portal run history**, via `foundry/submit_to_foundry.py` or the action's summary.
  Somewhere non-engineers can look.
- **Possibly the tool manifest.** `AIAgentConverter` takes an Agent Service
  thread + run id and returns `tool_definitions` read from the Agent Service
  rather than from telemetry. **Test this before extracting anything** — it may
  close §6 outright. Your traces carry the thread id as
  `gen_ai.conversation.id` (`conv_...`).

## What we keep, and why

- **`no_wasted_calls`, `no_dead_ends`, `no_search_cascade`.** A ConnectWise
  failure taxonomy. Foundry has no equivalent and never will.
- **`valid_tool_args` as the gate.** `ToolCallAccuracyEvaluator` supports a
  fixed list of tool types — File Search, Azure AI Search, Bing, SharePoint,
  Code Interpreter, Fabric, OpenAPI, Function Tool. **For anything else it
  returns _pass_**, with a reason string saying evaluation is not supported.
  If our MCP toolbox tools do not register as Function Tools, it reports green
  without evaluating. A pass that means "did not evaluate" is worse than no
  check, so the deterministic one stays the gate and the judged one is a
  second opinion.
- **Scoring recorded traces at all.** Foundry's offline evaluation invokes
  agents. Against production that is the one thing we cannot do — the agents
  mutate the ticket they operate on.
- **Trajectory expectations.** `expected.json` is our ground truth, and
  `check_trajectory` deliberately reports precision / recall / F1 the way Task
  Navigation Efficiency does, so numbers stay comparable if we ever move it.

## Native audit: what we use, what we skip, and why

Checked against Foundry as of 2026-09. Split three ways, because "are we
native" has three different answers depending on the piece.

### Native, and we use it

| Concern | Native mechanism | Where |
|---|---|---|
| Evaluator definitions | evaluator catalog, versioned | `foundry/register_evaluators.py` |
| Evaluator version pinning | catalog version ids | `evaluator-versions.json` |
| Running evals in the cloud | `evals.create` + run | `foundry/run_cloud_eval.py` |
| Eval datasets | project datasets, versioned | `foundry/to_foundry_dataset.py` |
| Result storage | project-scoped eval runs | Foundry, not this repo |
| Tracing | OTel GenAI conventions in App Insights | `export_traces.py` |
| Judged evaluators | built-in catalog | `foundry/submit_to_foundry.py` |
| Agent-change comparison | `microsoft/ai-agent-evals` | `staging-replay.yml` |
| Tools | MCP | throughout |
| CI identity | workload identity federation | `evals.yml` |

Results are **not** stored in this repo. Eval runs live in the project; this
repo holds definitions and ground truth.

### Native, and we are NOT using it — the real gap

**Continuous evaluation.** GA since March 2026. *(Now implemented — `foundry/continuous_eval.py`. Kept here because the reasoning still applies.)* Foundry samples live agent
traces at a configured rate (`AgentEvaluationSamplingConfiguration`,
`samplingPercent`, `maxRequestRate`), runs evaluators automatically, and
surfaces results in the Observability dashboard with alert rules. It
supports **custom** evaluators, not only built-ins — Microsoft's wording is
that custom and built-in evaluators "compose naturally, running against the
same traffic, producing results in the same schema, feeding into the same
dashboards and alert rules."

Ours are registered custom evaluators. They qualify.

What we do instead: a GitHub Actions cron exports the last 24h of traces,
converts them, scores them locally, and uploads a CI artifact. That is
hand-rolled monitoring where a native, GA, dashboard-integrated mechanism
exists.

**It is not a drop-in replacement**, for the reason documented above: the
native trace paths read `invoke_agent` spans, which on these traces carry
`tool_call` but no `tool_result`, and four of the eight checks read results.
Continuous evaluation also evaluates an *interaction*, where our converter
decomposes an orchestration into one row per agent run.

So the honest position is not "we were right to skip it" — it is that
continuous evaluation should be **added**, at a low sampling rate, for the
dashboard and alerting it brings, while the nightly keeps doing the
per-agent decomposition it cannot. Two surfaces answering different
questions, which is the pattern this repo already uses for judged evaluators.

**The schedules API.** Foundry's schedules support cron and recurrence
triggers. We use GitHub Actions cron. This was a deliberate choice — GitHub
schedules, Foundry scores — and it keeps scheduling next to the code and the
baseline diff. It is a defensible deviation rather than an oversight, but it
is a deviation: if the nightly moved to continuous evaluation, the schedule
would move with it and the GitHub cron would only cover the frozen sets.

### Not native, deliberately — re-checked, and two claims corrected

A second research pass found that two things I listed as having no native
equivalent do have one. The decisions survive; the reasons had to change.

**Baseline comparison IS native, and I said it was not.** Foundry lets you
set a baseline run and compare others against it, with statistical t-testing
per cell — p-values and sample sizes — and Microsoft documents running this
from GitHub Actions and Azure DevOps to block releases on clear regressions.

Why `baselines/*.json` stays in git anyway:

- **A t-test is the wrong instrument for a deterministic check.** That
  comparison exists to separate a real regression from run-to-run noise. Our
  eight checks have no noise: the same spans score the same way every time. A
  verdict flip is a flip. Statistical machinery over a deterministic signal
  adds a way to be wrong, not a way to be right.
- **A baseline must be reviewable in the pull request that changes it.** When
  a verdict moves, the diff belongs in the same review as the code that moved
  it. A baseline stored server-side changes without a reviewer.

Where Foundry's baseline comparison *does* fit is the judged sample and the
live staging replay, both stochastic. `--baseline-agent-id` through
`ai-agent-evals` already does exactly that.

**Dataset versioning and ground truth ARE native, and I implied otherwise.**
Foundry datasets are versioned, reusable across runs, and the schema carries
a `ground_truth` field. `expected.json` could live there.

It does not, for a reason that is not preference: **the merge gate runs with
no Azure.** `run_evals.py` and the frozen-set replay must work on a laptop
with no credentials and in a CI job with no secrets — that is what makes them
a gate rather than a dependency. Ground truth that lives only in a project
cannot be read by a job with no project access.

`to_foundry_dataset.py` already carries `expected_actions` into the uploaded
dataset, so cloud runs get it natively. The file in git is the source of
truth and the dataset is a projection of it, which is the right direction for
the dependency to point.

**Superseded by a third pass — see [`NATIVE-RESEARCH.md`](NATIVE-RESEARCH.md).**
Two of the three below turned out to have native equivalents after all:

| What | Status after re-checking |
|---|---|
| `run_evals.py` running locally | **Native path exists.** `azure-ai-evaluation.evaluate()` runs with no project and no credentials — verified by running it here with every `AZURE_*` variable deleted. The gate logic (baseline diff, exit code) is still ours. Adopting it costs stdlib-only. |
| Cassette replay | **Still ours.** ACS denies rather than substitutes; APIM `mock-response` generates from a schema rather than replaying bytes; APIM caching is a dictionary where a cassette is an ordered queue; Agent Framework mockable tools apply to code agents, not hosted prompt agents. Host it natively on the Functions MCP extension. |
| Building the dataset ourselves | **Partly native.** `azure_ai_traces` does drop `tool_result`, but `AIAgentConverter` reads the Agent Service and keeps it — `break_tool_call_into_messages` says so and reads `function.output`. Whether it covers MCP toolbox calls and per-agent decomposition is untested. |
| GitHub Actions scheduling | Foundry schedules (cron and recurrence) exist. Keeping scheduling beside the code and the baseline diff is a choice — and a smaller one now that continuous evaluation covers the live path. |

### The one-line answer

Definitions, datasets, runs and results are native and live in Foundry.
Continuous evaluation is now wired (`foundry/continuous_eval.py`). What stays
ours is the offline merge gate, the git-reviewable baseline for deterministic
checks, and the cassette replay — each because of a property Foundry does not
offer, rather than a feature it lacks.

## Judges are sampled, never per-commit

Judged evaluation is slow and the scores wobble. The split:

- **Merge gate:** deterministic checks only. Fast, free, no variance.
- **Agent change:** the action against staging. Significance testing is the
  point — a 3% move on 4 queries is noise and it will say so.
- **Nightly / weekly:** `foundry/submit_to_foundry.py --sample 20` for portal history.
- **Always on:** continuous evaluation at a low sampling rate.

## Branch to project

| Branch | GitHub environment | Foundry project | What runs |
|---|---|---|---|
| any | — | none | `frozen-sets` — committed traces vs committed baselines. No Azure. |
| `staging` | `staging` | `automation-solutions-test` | `staging-replay` (**invokes agents**), drift, judged sample |
| `main` | `prod` | `automation-solutions` | drift, judged sample. **Never invokes agents.** |

The branch and the GitHub environment share the name `staging`; `main` maps to
the `prod` environment. The Foundry projects keep their own names, so the
`staging` environment points at `automation-solutions-test`.

Everything except the replay reads recorded traces and writes evaluation
results, which is why `main` can safely target production.

**There is no production replay, and there must not be.** Running the replay
against `automation-solutions` would re-triage real tickets, and
`connectwise-operations-agent` would write the results into the system of
record. `staging-replay.yml` hard-codes `automation-solutions-test` as a
constant rather than reading it from a variable, so a mis-set environment
variable cannot redirect it. Production is evaluated from traces only.

A scheduled workflow always runs on the **default branch**, so deriving the
target from the branch would silently send every nightly run at one project.
The `plan` job handles that: on a schedule it fans out to both, on a push or
dispatch it follows the branch, and `workflow_dispatch` can name one.

### What a push actually runs

Pushing this repo does **not** run anything against Foundry. Only `frozen-sets`
runs on a push, and it is entirely offline — committed traces against committed
baselines, no Azure, no secrets, no agents.

| Trigger | Runs | Touches Foundry |
|---|---|---|
| push / PR, any branch | `frozen-sets` | no |
| push to `staging` touching `replay/**` | `staging-replay` | **yes — invokes agents in `automation-solutions-test`** |
| nightly 06:00 UTC | `drift` on **both** environments | reads App Insights |
| Monday 07:00 UTC | `drift` + judged sample | reads App Insights, calls the judge |
| manual dispatch | whatever you pick | depends |
| `repository_dispatch: agent-change` | `staging-replay` | **yes — invokes agents** |

So the branch mapping decides *which project the scheduled and dispatched jobs
read from*, not what a push does. A push to `main` runs the offline gate and
nothing else.

### Variables per environment

Set on the GitHub environment (`staging` and `prod`), not repo-wide:

| Name | Kind | Example |
|---|---|---|
| `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | var | federated credential for that project |
| `AZURE_AI_PROJECT_ENDPOINT` | var | the project endpoint; the replay guard checks this contains `automation-solutions-test` |
| `AZURE_JUDGE_DEPLOYMENT` | var | the pinned judge deployment — see below |
| `DEFAULT_AGENT_IDS` | var | `staging` only: `agent-name:version` to replay when a push supplies none |
| `DEFAULT_BASELINE_AGENT_ID` | var | `staging` only, optional: the version to compare against |
| `LOG_ANALYTICS_WORKSPACE_ID` | secret | that project's App Insights workspace |

The identity needs **Log Analytics Reader** *and* **Privileged Monitoring Data
Reader**. The second is required to read `AppGenAIContent` — see
`docs/TELEMETRY.md`. Without it the export succeeds and returns spans with no
`gen_ai.*` content, and every check scores an empty run.

Add a required reviewer on the `prod` environment if you want a human in the
loop before anything touches it, and give each environment its own app
registration — then a mistake in a staging workflow physically cannot reach
production. Federated credential subjects:

```
repo:verve-it/automation-solutions-evaluations:environment:staging
repo:verve-it/automation-solutions-evaluations:environment:prod
```

## Choosing the judge model

Four things decide this, in order.

**1. It cannot be the same family as the agents.** A judge scores its own
family's output higher — self-preference bias is well documented and gets
*stronger* with more capable judges. Your agents run `gpt-5.6-luna`; pick a
judge from a different family. This is the one rule worth breaking a cost
target over.

**2. These tasks are on the hard end.** Task Adherence has to read a 25,000-
character skill file and decide whether the run followed it. Intent Resolution
has to follow a hand-off across four agents. Tool Call Accuracy has to reason
over a 55-call trajectory. That is reasoning-model territory, not a cheap
chat model. Microsoft's own guidance: `gpt-5-mini` for a cost/performance
balance, a reasoning model such as `o3-mini` or a later o-series mini for
complex evaluation.

**3. Cost barely matters here, because we sample.** Twenty rows weekly. Do not
trade judge quality for a saving that rounds to nothing. Compare that with the
agents themselves, which burn ~1M uncached tokens per triage.

**4. Pin it, and treat a change like a baseline promotion.** The judge
deployment *and* its model version are part of the measurement. Change either
and every earlier score becomes incomparable — the same failure mode as
rewriting a baseline in place. Pin the version on the deployment, record it in
the run name, and when you do upgrade, re-run the previous sample on the new
judge before trusting the trend.

**Recommendation:** `o3-mini` (or the current o-series mini) pinned to an
explicit model version, deployed once per environment as
`AZURE_JUDGE_DEPLOYMENT`. Drop to `gpt-5-mini` only if throughput becomes a
problem, and re-baseline when you do.

**Calibrate it against your reviewers.** Once Dataverse AI Review is
accumulating, you have something most teams never get: human labels on the
same runs. Score a set the judge has already scored and check they agree. If
they do not, the judge is wrong, not the reviewers. That calibration is worth
more than any model choice above, and it is the point at which "what is
industry standard" stops mattering because you have your own answer.

## Prerequisites

| For | Needs |
|---|---|
| Continuous evaluation | App Insights connected to the Foundry project; the project managed identity holding Foundry User; a `create_agent_evaluation` call per run, so the **agents** call it, not us |
| `ai-agent-evals` action | The `staging` environment's endpoint, a judge deployment, federated credentials, and `replay/` populated with dev ticket ids |
| `foundry/submit_to_foundry.py` | `azure-ai-evaluation`, a judge deployment, and `tool_manifests/` filled or ToolCallAccuracy is meaningless |

## Sources

- [Agent evaluation with the Foundry SDK](https://learn.microsoft.com/en-us/azure/foundry-classic/how-to/develop/agent-evaluate-sdk)
- [Continuously evaluate your AI agents](https://learn.microsoft.com/en-us/azure/foundry-classic/how-to/continuous-evaluation-agents)
- [microsoft/ai-agent-evals](https://github.com/microsoft/ai-agent-evals)
- [Run an evaluation in a GitHub Action](https://learn.microsoft.com/en-us/azure/foundry/how-to/evaluation-github-action)
- [General purpose evaluators](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/general-purpose-evaluators) — judge model guidance
- [Self-preference bias in LLM-as-a-judge](https://arxiv.org/pdf/2410.21819)
- [Set up tracing for AI agents](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup)

## The checks, registered in Foundry

`run_evals.py` keeps the checks in a Python dict in this repo, which means
nothing about them is visible in the Foundry project. They are now also
published to the **evaluator catalog** as versioned, reusable code-based
evaluators, so they sit beside Microsoft's built-ins, apply to any agent on
the same tool surface, and can run in continuous evaluation.

```powershell
python foundry/register_evaluators.py --dry-run            # inspect, calls nothing
python foundry/register_evaluators.py --project-endpoint $env:AZURE_AI_PROJECT_ENDPOINT --model-deployment $env:AZURE_JUDGE_DEPLOYMENT

python foundry/to_foundry_dataset.py traces/2026-09-03-full-triage.json --expected expected.json --tool-defs tool_manifests/ -o artifacts/foundry-dataset.jsonl
python foundry/run_cloud_eval.py artifacts/foundry-dataset.jsonl --name full-triage --dataset-version 2026-09-17 --judged task_adherence --wait --trace traces/2026-09-03-full-triage.json
```

`--wait` runs the comparison below inline. Without it the command prints the
eval and run ids and the `foundry/check_cloud_eval.py` line to follow up with.

| Registered | Threshold | Gating | Reproduces |
|---|---|---|---|
| `cw_no_wasted_calls` | 1.0 | yes | `no_wasted_calls` |
| `cw_valid_tool_args` | 1.0 | yes | `valid_tool_args` |
| `cw_no_dead_ends` | 0.75 | yes | `no_dead_ends` (`max_empty_rate` 0.25, the other way up) |
| `cw_trajectory` | 1.0 | yes | `trajectory` |
| `cw_no_tool_errors` | 1.0 | no | `no_tool_errors` |
| `cw_no_search_cascade` | 1.0 | no | `no_search_cascade` |
| `cw_no_truncation` | 1.0 | no | `no_truncation` |
| `cw_cost_latency` | 1.0 | no | `cost_latency` |

`pass_threshold` is an init parameter, so the tolerance lives in Foundry
rather than baked into the score.

### Why we build the dataset instead of using `azure_ai_traces`

Foundry's trace-sourced evaluation reads only spans where
`gen_ai.operation.name == invoke_agent`. On these traces those spans carry
`tool_call` content items and **no `tool_result`** — every result is on an
`execute_tool` span, which that path discards. Four of the eight checks read
results, so on `azure_ai_traces` they cannot run at all.

`foundry/to_foundry_dataset.py` sidesteps it. The converter already reads
`execute_tool` spans, so the results are in hand; the dataset carries standard
`messages` with `tool_call`/`tool_result` items for the built-in judged
evaluators, plus a `tool_outcomes` column for ours. That column exists because
`messages` cannot distinguish "succeeded and returned nothing" from "failed and
returned nothing", and that distinction is the whole of `empty_failed`.

It also gives the per-agent decomposition Foundry does not do:
`TracesDataGenerationJobSource` and `azure_ai_traces` are both scoped to a
single agent identity, and Microsoft's multi-agent guidance is to evaluate the
orchestrator rather than fan out.

### Dataset size

One row reached 1.1 MB and the evals service answered with a 500. Two causes,
both fixed:

- **Every tool result was stored twice** — once in `messages` as a
  `tool_result`, once in `tool_outcomes`. `tool_outcomes` now carries
  `result_head` (the first 600 characters) and `result_len`, which is
  everything the checks read: classification and emptiness look at the head,
  truncation looks at the length.
- **`--no-messages`** drops the `messages` column entirely. The registered
  custom evaluators do not need it; only the built-in judged ones do.

| | file | largest row |
|---|---|---|
| before | 2.04 MB | 1.10 MB |
| trimmed results | 1.21 MB | 0.61 MB |
| trimmed + `--no-messages` | **0.11 MB** | **0.05 MB** |

Parity is unaffected: 56 verdicts, 0 mismatches, before and after.

`foundry/to_foundry_dataset.py` warns before the upload when a row exceeds a
megabyte, and `foundry/run_cloud_eval.py` refuses `--judged` on a dataset built
without `messages`.

### The item schema is not optional

`data_source_config.item_schema` was a bare `{"type": "object"}`, which is not
permissive — the service defaults undeclared properties to string, and the run
dies on the first integer with

```
Error validating file against schema: 35756 is not of type 'string'
```

(35756 being `usage.uncached_input_tokens`.) The schema is derived from the
rows themselves, **recursing into nested objects** — declaring `usage` as a
bare `{"type": "object"}` leaves its integers undeclared and hits exactly the
same error. Arrays are deliberately left undescribed: `tool_outcomes` carries
integers and booleans and validates fine without an `items` schema, so the
validator does not descend into them.

`intent` is written as `""` rather than null to keep every property a single
type — a nullable column becomes a type union, and `traj_key` already carries
the intent.

Retrieving the created eval is how to check what the service actually stored;
it normalises `item_schema` into `schema_.item`, so an empty `item_schema` in
the response is not a sign it was ignored.

**A nested object cannot hold a non-string value**, whatever the schema says.
`tools/diagnose_schema.py` submits one-row datasets differing in one way each and
reports which survive:

| shape | result |
|---|---|
| top-level integer | pass |
| top-level float | pass |
| integer inside an array of objects | pass |
| **integer inside a nested object** | **fail** |

So `usage` is flattened to `usage_*` columns. Arrays are not descended into,
which is why `tool_outcomes` and `tool_definitions` keep their structure.
`foundry/to_foundry_dataset.py` refuses to write a dataset containing a nested object
column.

### What the port costs

A code-based evaluator returns exactly **one float, 0.0–1.0**, in a sandbox
with no network and a 256 KB code limit. `run_evals.py` returns a verdict *and*
a reason — "4 avoidable call(s): empty_failedx2, missing_scriptx1" — and a
score cannot say why. So:

- **run_evals.py stays** as the local gate that explains itself, and as the
  baseline-diff regression gate, which Foundry has no equivalent for.
- **The registered evaluators** are for catalog membership, portal run
  history, reuse across agents, and continuous evaluation.

`cw_trajectory` scores **recall**, not F1. Extra steps are allowed by design —
a healthy analysis run takes 51 of them — so an F1 threshold would fail
everything. `run_evals.py` still reports precision and F1 locally, in the shape
Task Navigation Efficiency uses.

Fidelity is tested, not assumed: `tests/test_foundry_evaluators.py` scores both
frozen trace sets with `run_evals.py` and with the ported functions and asserts
every comparable verdict matches. **56 verdicts, 0 mismatches.** It also
executes each uploaded `code_text` standalone and asserts it scores identically to
the imported function.

### Checking a cloud run actually evaluated anything

```powershell
python foundry/check_cloud_eval.py <eval_id> <run_id> --trace traces/2026-09-03-full-triage.json
```

The ids are printed by `foundry/run_cloud_eval.py`, or pass it `--wait` and it chains
straight into this. It waits for the run, prints the per-row scores, and diffs them against
`run_evals.py` at each evaluator's threshold.

**Watch for a clean sweep of 1.0s.** If `{{item.tool_outcomes}}` does not
resolve, every evaluator sees an empty list and scores 1.0 for "nothing wrong
here". That reads as a perfect run and is actually no evaluation at all — the
same silent-pass hazard as `ToolCallAccuracy` reporting success for a tool
type it cannot read. `foundry/check_cloud_eval.py` fails the run when it sees it.

`--raw` dumps the first output item if the result shape has moved.

### Pin the evaluator versions

`foundry/register_evaluators.py` writes `evaluator-versions.json`, and
`foundry/run_cloud_eval.py` pins from it. Without that the criterion's
`evaluator_version` is empty and the run floats to whatever is latest — so two
runs of the same baseline can be scored by different code, and a "regression"
may just be a re-registration.

**Commit the lock file with the baseline it belongs to**, the same discipline
as the baselines themselves. `--no-lock` deliberately runs against the latest.

### Still not in Foundry

`expected.json`, the frozen baselines, the baseline diff and the CI gate, the
cassettes, and the trace sets. The baseline diff is the one with no Foundry
equivalent; the rest could move once these runs prove out.
