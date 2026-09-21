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

**Recommendation: adopt it for the scoring pass, keep the gate logic.** Have
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

### Two things to verify before switching

Both are visible in the source and neither is settled:

1. **MCP toolbox calls may not be covered.** The converter's own comment says
   *"we only support custom functions due to built-in code interpreters and
   bing grounding tooling not reporting their function calls in the same
   way"*, and the code branches on `details.function` versus a list of known
   built-in types. Whether a ConnectwiseMCP toolbox call lands in the
   `function` branch is the whole question, and it is one thread-id away from
   an answer.
2. **Per-agent decomposition.** It converts one thread and run. Our converter
   splits an orchestration into one row per agent run. Whether each child
   agent is its own run id in the Agent Service, or whether they collapse,
   determines whether this replaces the converter or only part of it.

**Recommendation: test it against one real orchestration.** The traces carry
the thread id as `gen_ai.conversation.id` (`conv_...`). Half an hour settles
whether `trace_to_eval.py` can shrink or must stay.

---

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

- **Host:** Azure Functions with the **MCP extension** (tool trigger and
  binding). Stateful, GA, and `SessionId` on the invocation context is the
  correct home for `Cassette.cursor` — one MCP session is one replay. See
  `docs/REPLAY.md`.
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
| Dataset construction | **partly** — `AIAgentConverter` keeps results | test against one orchestration before deciding |
| Cassette replay | **no** | keep, host natively on Functions MCP extension |

Two of three moved from "no native way" to "there is one, and here is the
trade". That is worth knowing before more is built on the assumption I gave.
