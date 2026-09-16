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

The toolbox is versioned, so this is a one-time extraction **per version**, not
a per-run capture.

1. Check whether the `tools/list` span already carries it. (It did not in the
   September traces — there are no `tools/list` spans in the export at all, and
   `mcp.method.name` only shows `initialize` and `tools/call`.)

   ```kusto
   dependencies
   | where name has "tools/list"
   | extend d = customDimensions
   | project timestamp, name, keys = bag_keys(d), dims = d
   | take 5
   ```

2. If not, export the toolbox definition from the Foundry portal, or capture
   the raw `tools/list` JSON-RPC response from the MCP endpoint, then:

   ```bash
   python3 extract_tool_manifest.py --from-tools-list tools-list.json \
       --toolbox ConnectwiseMCP --version 5 \
       -o tool_manifests/connectwisemcp-v5.json
   ```

3. A skeleton from the traces is available in the meantime — every tool the
   agents actually called, with the description telemetry carries and the
   argument keys observed, and `parameters: null` for each. Tools with a null
   schema are skipped by the validator rather than guessed at.

   ```bash
   python3 extract_tool_manifest.py \
       --from-trace traces/2026-09-03-full-triage.csv \
       --toolbox ConnectwiseMCP --version 5 \
       -o tool_manifests/connectwisemcp-v5.json
   ```

## Version-key everything

**Scoring old behaviour against a new schema silently corrupts results.** The
converter matches a manifest to a run by the toolbox version in the run's span
URLs (`/toolboxes/<toolbox>/versions/<v>/mcp`) and applies nothing when they
do not match.

This is not hypothetical. In the frozen full-triage baseline:

| Agent | Toolbox version |
|---|---|
| `triage-analysis-agent` | ConnectwiseMCP **5** |
| `connectwise-operations-agent` | ConnectwiseMCP **1** |

The agent that writes to the system of record is four versions behind the one
that reads. Worth confirming that is deliberate.

## File format

```json
{
  "toolbox": "ConnectwiseMCP",
  "version": "5",
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

The validator implements a deliberately small JSON Schema subset: `required`,
`additionalProperties: false`, `type`, and `enum`. That is the six criteria
Tool Input Accuracy checks. Anything it does not understand it declines to
judge rather than failing a run on a keyword it got wrong.
