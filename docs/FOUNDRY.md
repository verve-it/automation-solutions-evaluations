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
- **Portal run history**, via `submit_to_foundry.py` or the action's summary.
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

## Judges are sampled, never per-commit

Judged evaluation is slow and the scores wobble. The split:

- **Merge gate:** deterministic checks only. Fast, free, no variance.
- **Agent change:** the action against staging. Significance testing is the
  point — a 3% move on 4 queries is noise and it will say so.
- **Nightly / weekly:** `submit_to_foundry.py --sample 20` for portal history.
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
| `submit_to_foundry.py` | `azure-ai-evaluation`, a judge deployment, and `tool_manifests/` filled or ToolCallAccuracy is meaningless |

## Sources

- [Agent evaluation with the Foundry SDK](https://learn.microsoft.com/en-us/azure/foundry-classic/how-to/develop/agent-evaluate-sdk)
- [Continuously evaluate your AI agents](https://learn.microsoft.com/en-us/azure/foundry-classic/how-to/continuous-evaluation-agents)
- [microsoft/ai-agent-evals](https://github.com/microsoft/ai-agent-evals)
- [Run an evaluation in a GitHub Action](https://learn.microsoft.com/en-us/azure/foundry/how-to/evaluation-github-action)
- [General purpose evaluators](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/general-purpose-evaluators) — judge model guidance
- [Self-preference bias in LLM-as-a-judge](https://arxiv.org/pdf/2410.21819)
- [Set up tracing for AI agents](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup)
