# MCP tool manifests

**This directory is empty on purpose, and filling it is the highest-leverage
work left.** See §6 of `docs/HANDOFF.md`.

## The gap

`gen_ai.tool.definitions` only covers **A2A agent registrations**. On the
orchestrator it holds exactly three entries — the child agents. Child agents
have no A2A children, so the attribute is absent entirely, which is why
`evaluator_ready` fails on 5 of 7 runs in the frozen baseline.

Consequence: **no schema exists in telemetry for any ConnectWise tool.** Tool
Input Accuracy and Tool Output Utilization cannot run anywhere, and argument
validation has to be hand-written per tool, per agent, for ever.

## Why it matters more than it sounds

With a manifest, argument validation is **generated rather than written** —
required params, types, enums, unexpected fields, per tool, automatically.
That is the same six criteria Tool Input Accuracy checks, deterministic and
free. It is the difference between a suite that grows linearly with the agent
count and one that does not.

Every `cw_resolve` failure in the baseline would have been caught this way. To
see that with your own eyes, run the known-bad set against the hand-written
test fixture:

```bash
python3 trace_to_eval.py traces/2026-09-15-ops-worst-case.csv -o out-ops \
    --tool-defs tests/fixtures/connectwisemcp-v1-partial.json
python3 run_evals.py out-ops/eval_runs.jsonl --expected expected.json
```

`valid_tool_args` then fails on every unsupported `reference_type`, and
`evaluator_ready` goes green because the runs finally carry schemas.

That fixture is **not** the real toolbox schema. It is two hand-written tools
used to exercise the mechanism. Do not score anything with it.

## Getting the real thing

Four routes, best first.

**0. Try `AIAgentConverter` before extracting anything.** It takes an Agent
Service thread id and run id and returns `query`, `response`, `tool_calls` and
**`tool_definitions`** — read from the Agent Service, not from telemetry. If it
covers the ConnectWise tools, this whole directory becomes unnecessary. Your
traces carry the thread id as `gen_ai.conversation.id` (`conv_...`). Half an
hour to find out. See `docs/FOUNDRY.md`.

**1. Call `tools/list` on the toolbox endpoint.** The URL is already in your
traces — it is the span name of every MCP call:

```
POST /api/projects/automation-solutions/toolboxes/ConnectwiseMCP/versions/5/mcp
```

```bash
python3 fetch_tool_manifest.py \
    --host https://<your-foundry-host> \
    --project automation-solutions \
    --toolbox ConnectwiseMCP \
    -o tool_manifests/connectwisemcp.json
```

`az login` first, or paste a token with `--token`. `--print-url` shows the
endpoint without calling it.

**2. From a `tools/list` dump or portal export:**

```bash
python3 extract_tool_manifest.py --from-tools-list tools-list.json \
    --toolbox ConnectwiseMCP -o tool_manifests/connectwisemcp.json
```

**3. A skeleton from the traces, in the meantime** — every tool the agents
actually called, with the description telemetry carries and the argument keys
observed, and `parameters: null` for each. Tools with a null schema are skipped
by the validator rather than guessed at:

```bash
make manifest-skeleton
```

There are no `tools/list` spans in the September exports — `mcp.method.name`
only shows `initialize` and `tools/call` — so route 1 or 2 is needed. Worth
re-checking after any telemetry change:

```kusto
dependencies
| where name has "tools/list"
| extend d = customDimensions
| project timestamp, name, keys = bag_keys(d), dims = d
| take 5
```

## The version in the URL is a binding revision, not a schema version

In the frozen full-triage set the analysis agent shows ConnectwiseMCP **v5**
and the ops agent **v1** — same toolbox, same orchestration. The `cw_resolve`,
`cw_get_ticket` and `load_skill` descriptions are **byte-identical** across
both, so the number tracks when each agent's binding was last edited, not the
tool contract.

So a manifest normally declares:

```json
"versions": ["*"]
```

Pin to specific revisions only when you have evidence the contract actually
differs — compare `gen_ai.tool.description` across revisions first. The
converter records the revisions each run used either way, so a genuine
divergence stays visible.

## File format

```json
{
  "toolbox": "ConnectwiseMCP",
  "versions": ["*"],
  "source": "where this came from, and when",
  "tools": [
    {
      "name": "cw_resolve",
      "description": "...",
      "parameters": { "type": "object", "properties": {}, "required": [] }
    }
  ]
}
```

- `name` is the **bare** tool name. Telemetry carries the Foundry-prefixed form
  (`ConnectWise-PSA-ForAgents___cw_resolve`); the converter and the validator
  match on the bare name.
- `parameters` is a JSON Schema object. `inputSchema` is accepted as an alias,
  so a raw `tools/list` entry can be dropped in unchanged.
- `parameters: null` means "not known yet" — skipped, never guessed.
- `versions` lists the binding revisions this manifest covers; `["*"]` is the
  normal answer. A single `"version": "5"` is still accepted and means `["5"]`.

The validator implements a deliberately small JSON Schema subset: `required`,
`additionalProperties: false`, `type`, and `enum`. That is the six criteria
Tool Input Accuracy checks. Anything it does not understand it declines to
judge rather than failing a run on a keyword it got wrong.
