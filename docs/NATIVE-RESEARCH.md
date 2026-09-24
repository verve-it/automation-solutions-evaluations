# Second research pass on the three non-native pieces

Asked to check thoroughly whether the cassette replay, the dataset
construction and the offline merge gate can and should be Microsoft-native,
and if not, how to build them on native architecture.

**Two of the three were wrong.** Written up in full because the corrections
matter more than the conclusions.

---

## 1. Offline merge gate — I was wrong. A native offline path exists.

I said a merge gate cannot be native because it must run with no Azure, no
credentials and no network.

`azure-ai-evaluation`'s `evaluate()` does exactly that. `azure_ai_project` is
optional, and since the SDK dropped its promptflow-service dependency there
is nothing to start. Run against a code-based custom evaluator, in this
container, with every `AZURE_*` environment variable deleted:

```
=== RAN WITH NO AZURE PROJECT, NO CREDENTIALS ===
metrics: {'dead_ends.cw_no_dead_ends': 0.5}
   {'outputs.dead_ends.cw_no_dead_ends': 1.0}
   {'outputs.dead_ends.cw_no_dead_ends': 0.0}
```

Aggregated metric, per-row outputs, no network. The native SDK is an offline
evaluation harness, and I asserted it was not.

### What that does and does not change

`evaluate()` scores rows. `run_evals.py` scores rows **and** diffs against a
frozen baseline, computes the gating exit code, prints the tracking table and
the skill-drift report. Those are not in `evaluate()` and would still be ours.

What moving would buy: the evaluator functions the gate runs become literally
the same objects Foundry runs, rather than two implementations kept in step
by `tests/test_foundry_evaluators.py`. That parity test exists because we
have two implementations. One implementation, no parity test.

What it would cost: the merge gate gains a dependency on
`azure-ai-evaluation`, which pulls numpy, pandas, openai and ~60 packages.
Today the converter and scorer are stdlib-only and the merge gate installs
nothing. That is a real property — it is why the gate runs in seconds and
cannot break on a dependency resolution.

**Done — `foundry_evaluators/native.py`.** It adapts the same `grade_*`
functions `checks.py` already holds to the shape `evaluate()` wants, so the
local harness runs the native evaluator objects rather than a second
implementation.

Verified verdict for verdict against `run_evals.py` on both frozen sets:
**62 verdicts, 0 mismatches**, with every `AZURE_*` variable deleted.
`tests/test_foundry_evaluators.py` keeps it that way.

The dependency note below still stands, which is why this is a scoring path
rather than a replacement:

**Recommendation was: adopt it for the scoring pass, keep the gate logic.** Have
`run_evals.py` call `evaluate()` with `foundry_evaluators/checks.py` as the
evaluator set, then do the baseline diff on its output. Requires moving the
merge gate off stdlib-only, which is a deliberate trade and should be made
deliberately, not by drift.

One gotcha found while testing: `evaluate()` introspects the evaluator
signature to work out required columns, and a `**kwargs` parameter is read as
a required input named `kw`. Evaluators need explicit keyword parameters.

---

## 2. Dataset construction — I was wrong. `AIAgentConverter` keeps tool results.

I justified building the dataset ourselves with "`azure_ai_traces` reads
`invoke_agent` spans, which carry `tool_call` but no `tool_result`". That is
true of `azure_ai_traces`. I used it to conclude no native converter
preserves results, and that is false.

`AIAgentConverter` reads the **Agent Service** — thread and run — not
telemetry. From the installed package:

```python
def break_tool_call_into_messages(tool_call: ToolCall, run_id: str) -> List[Message]:
    """Breaks a tool call into a list of messages, including the tool call
    and its result."""
...
output = safe_loads(tool_call.details.get("function")["output"])
```

It reads the result. It also offers `prepare_evaluation_data(thread_ids=...,
filename=...)` for batch preparation.

### Both open questions are now answered — and the answer is "do not switch"

Neither is documented. Both are answerable from the SDK source and from our
own traces, which is where the answers came from.

**Q2, per-agent decomposition — answered from the committed trace.** One
orchestration, 62 spans carrying `gen_ai.conversation.id`, and **one distinct
value**:

```
gen_ai.conversation.id                       conv_059406d2a02be95d00Eg1WCe…  x62
microsoft.gen_ai.main_agent.conversation_id  conv_059406d2a02be95d00Eg1WCe…  x28
```

Orchestrator and every child agent share the orchestrator's conversation.
`microsoft.gen_ai.main_agent.conversation_id` exists precisely to link a
child's span back to it, and it holds the same value. A converter keyed on
the conversation returns **one blob for the whole orchestration**, not a row
per agent run. The per-agent decomposition `trace_to_eval.py` does would be
lost, and with it the ability to say which agent regressed.

**Q1, MCP tool calls — the source answers it structurally.**
`break_tool_call_into_messages` branches on `details.function`, then on five
named built-ins (`code_interpreter`, `bing_grounding`, `file_search`,
`azure_ai_search`, `fabric_dataagent`), and then:

```python
else:
    # unsupported tool type, skip
    return messages
```

**Silently skipped.** Not an error — the tool call simply vanishes from the
converted data, and every tool-reading check then scores a run that appears
to have made no tool calls. That is the vacuous-pass failure mode this repo
has already been bitten by twice.

Every MCP item type in the SDK (`mcp_call`, `mcp_list_tools`,
`mcp_approval_response`) belongs to the **Realtime** surface. Nothing models
an MCP tool call for the agent run path, so whether a ConnectwiseMCP call
lands in the `function` branch is unproven — and the failure mode if it does
not is silence.

**The finding that settles it: we are not on the platform the converter is
for.** `AIAgentConverter` takes `(thread_id, run_id)` and is documented only
under **foundry-classic** — the threads-and-runs platform, deprecated and
**retiring 2027-03-31**. The new Foundry Agents Service uses conversations
with `conv_` ids. Our traces carry `conv_…` and **no** `thread_` or `run_`
ids at all.

So adopting `AIAgentConverter` would mean migrating onto a deprecated API,
losing per-agent decomposition, and risking silent tool-call loss.

**Recommendation reversed: keep `trace_to_eval.py`.** My earlier advice to
test the converter pointed at the classic platform without checking which
platform these agents are on. The native path for the new service is the one
already in use — build a dataset and evaluate it, which `to_foundry_dataset.py`
and `run_cloud_eval.py` do.

## 3. Cassette replay — stays ours. No native equivalent exists.

Checked four candidates. None fit.

| Candidate | Why not |
|---|---|
| **Foundry / ACS** | ACS gates tool execution allow/deny at a lifecycle checkpoint. It denies; it does not substitute a recorded response. |
| **APIM `mock-response`** | Returns a sample generated from an OpenAPI example or schema. A *mock*, not a *replay* — it cannot return the bytes the real tool returned, and cannot return a different response for the same call at two points in a run. |
| **APIM caching** | A cache is a dictionary keyed by request. A cassette is an ordered queue: the same call appears several times with different results, and position is what selects the right one. |
| **Agent Framework mockable tools** | Real, and the right answer — for MAF **code** agents you run yourself. Our agents are Foundry **hosted prompt agents** bound to an MCP toolbox; there is no place to inject a mock. |

The pattern itself is standard — an eval harness providing "a mock MCP server
that records and replays tool responses" is described as normal practice, and
is what `replay/` is. Microsoft does not ship one for hosted agents.

### The native way to build it

Not a feature, but the architecture is native and documented:

- **Host:** Azure Functions, Flex Consumption, as a **custom handler** —
  the documented path for hosting a server built with an MCP SDK, with the
  `mcp-custom-handler` profile in `host.json`. Built: `functions/replay-mcp/`.
  **Not** the MCP extension: its `toolProperties` has no `enum`, and 8 of our
  advertised properties are enums — including `cw_resolve.reference_type`,
  the twenty-value one that makes `valid_tool_args` a check that can fail. A
  stub advertising a looser contract than production causes divergence and
  then blames the agent for it. The profile is preview-flagged --
  `AzureWebJobsFeatureFlags=EnableMcpCustomHandlerPreview`, which Bicep sets
  because Microsoft's sample does, though the host honoured the profile
  without it. See `docs/REPLAY.md`.
- **Register:** Foundry toolbox pointing at the Function endpoint, documented
  as "Connect an MCP server on Azure Functions to Foundry Agent Service".
- **Bind:** agent version whose tools point at the replay toolbox, which
  `replay/run_replay.py` already does through the SDK.

So the replay is native in every respect except that the stubbing logic is
ours, because nothing ships it.

---

## Summary

| Piece | Native equivalent? | Action |
|---|---|---|
| Offline merge gate | **yes** — `evaluate()`, verified offline here | adopt for scoring; keep baseline diff and gating. Costs stdlib-only. |
| Dataset construction | **no** — `AIAgentConverter` is classic-platform (retires 2027-03-31); our traces are `conv_`, and child agents share one conversation | keep `trace_to_eval.py` |
| Cassette replay | **no** | keep; hosted natively on Functions (Flex Consumption, custom handler) — `functions/replay-mcp/` |

Two of three moved from "no native way" to "there is one, and here is the
trade". That is worth knowing before more is built on the assumption I gave.
