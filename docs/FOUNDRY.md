# What Foundry does for us, and what we do ourselves

Written to answer one question: *are we hand-rolling anything Foundry offers
for little effort?*

## Three surfaces, three jobs

| Surface | What it scores | Data source | Where it runs | Judge cost |
|---|---|---|---|---|
| **Deterministic checks** (`run_evals.py`) | Process quality — wasted calls, dead ends, cascades, argument validity, trajectory | Recorded production traces | CI merge gate, nightly drift | none |
| **`microsoft/ai-agent-evals`** | Foundry evaluator catalog, with confidence intervals and significance vs a baseline agent version | Agents **invoked** with a query set | CI on agent change, **staging only** | per run |
| **Foundry continuous evaluation** | Judged metrics on live traffic at a sampling rate | Production traces, sampled | Production, always on | per sampled run |

They are not alternatives. The first scores what production actually did; the
second scores what a changed agent *would* do; the third watches for drift
without anyone asking.

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

## Prerequisites

| For | Needs |
|---|---|
| Continuous evaluation | App Insights connected to the Foundry project; the project managed identity holding Foundry User; a `create_agent_evaluation` call per run, so the **agents** call it, not us |
| `ai-agent-evals` action | Staging project endpoint, a judge model deployment, federated credentials, and `replay/` populated with dev ticket ids |
| `submit_to_foundry.py` | `azure-ai-evaluation`, a judge deployment, and `tool_manifests/` filled or ToolCallAccuracy is meaningless |

## Sources

- [Agent evaluation with the Foundry SDK](https://learn.microsoft.com/en-us/azure/foundry-classic/how-to/develop/agent-evaluate-sdk)
- [Continuously evaluate your AI agents](https://learn.microsoft.com/en-us/azure/foundry-classic/how-to/continuous-evaluation-agents)
- [microsoft/ai-agent-evals](https://github.com/microsoft/ai-agent-evals)
- [Run an evaluation in a GitHub Action](https://learn.microsoft.com/en-us/azure/foundry/how-to/evaluation-github-action)
- [Set up tracing for AI agents](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup)
