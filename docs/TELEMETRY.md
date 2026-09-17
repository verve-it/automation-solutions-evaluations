# Where the trace content actually lives

## The deadline

**Before 2026-09-30** App Insights writes the seven `gen_ai.*` content
attributes into *both* the span tables (`AppDependencies` / `dependencies`)
and `AppGenAIContent`.

**From 2026-09-30** it stops writing the values into the span tables. The
attribute keys remain; their values become a short pointer to
`AppGenAIContent`, alongside `_MS.GenAIContentId`.

A span-only export taken after that date still looks well-formed — same
columns, same keys — and contains no content. Every check would score an empty
run and report cheerfully. `trace_to_eval.py` prints a loud warning if it sees
that shape.

The seven attributes:

```
gen_ai.input.messages          gen_ai.tool.definitions
gen_ai.output.messages         gen_ai.tool.call.arguments
gen_ai.system_instructions     gen_ai.tool.call.result
gen_ai.evaluation.explanation
```

## What to do

`export_traces.py` joins `AppGenAIContent` by default. Nothing to change in
the pipeline — but the **identity needs a new role**:

| Role | For |
|---|---|
| Log Analytics Reader | the span tables |
| **Privileged Monitoring Data Reader** | **`AppGenAIContent`** |

Grant it on both the `staging` and `prod` environments' identities. Without it
the export succeeds and returns spans stripped of content.

To read the content at all, the workspace needs the feature enabled:

```powershell
az feature register --namespace Microsoft.Insights --name protectGenAISensitiveData
# a feature registration only takes effect once the provider is re-registered
az provider register --namespace Microsoft.Insights
# confirm
az feature show --namespace Microsoft.Insights --name protectGenAISensitiveData --query properties.state
```

## This also fixes the truncation

`AppGenAIContent` exposes the payloads as **real columns**, not property-bag
entries, so the 8192-character App Insights property cap does not apply:

| Column | Replaces |
|---|---|
| `InputMessages` | `gen_ai.input.messages` |
| `OutputMessages` | `gen_ai.output.messages` |
| `SystemInstructions` | `gen_ai.system_instructions` |
| `ToolDefinitions` | `gen_ai.tool.definitions` |
| `ToolCallArguments` | `gen_ai.tool.call.arguments` |
| `ToolCallResult` | `gen_ai.tool.call.result` |

Joined to a span by `SpanId` (also `TraceId`, `ParentSpanId`, `AgentId`,
`AgentName`, `ModelName`).

All seven truncations in the committed traces are `cw_query` results cut at
exactly 8192. Re-exporting those orchestrations with the join should return
them whole — **worth verifying before trusting it**, because a `--strict`
cassette and an honest `no_truncation` check both depend on it:

```powershell
python3 export_traces.py --workspace $LAW_ID --operation-ids bed408b416e8bb61d56f800212b90459,4dda7f4fa5f04ff2855b46978948af2a --since 2026-09-01 -o traces/2026-09-03-full-triage-rejoined.json
python3 trace_to_eval.py traces/2026-09-03-full-triage-rejoined.json -o out
# truncated results should be 0
```

If it comes back clean, re-freeze the baseline and rebuild the cassettes —
`make_cassette.py --strict` currently refuses both full-triage orchestrations
over exactly this.

## Two things it may also fix

**`ToolDefinitions` is a column.** Check whether it is populated for the
ConnectwiseMCP tools before extracting a manifest by hand — it may close
`tool_manifests/` outright.

```kusto
AppGenAIContent
| where isnotempty(ToolDefinitions)
| project TimeGenerated, AgentName, ToolDefinitions
| take 5
```

**Foundry-native trace evaluation.** `azure_ai_traces` reads only
`invoke_agent` spans, and on our traces those carry `tool_call` but never
`tool_result` — which is why the result-reading checks cannot run there. The
results do exist on Microsoft's side, in `ToolCallResult`. Whether Foundry's
evaluation service reads that column is undocumented. If it does, most of this
repo can move into Foundry; see `docs/FOUNDRY.md`.

## Sources

- [Protect sensitive content in traces](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/traces-sensitive-content)
- [AppGenAIContent table reference](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/appgenaicontent)

## Scrubbing before you commit a trace

The rejoined export is untruncated customer data. `protectGenAISensitiveData`
restricts it to Privileged Monitoring Data Reader; committing it replaces that
with "has repo access", permanently, in git history.

The 2026-09-03 orchestrations contain **67 real e-mail addresses** of staff and
customer contacts, contact and company names, a site address and phone
numbers — most of it embedded in write plans and audit notes rather than in
structured fields.

Two steps, because this is a review, not an automation.

```powershell
# 1. propose. Expect noise; the point is a short list you can actually read.
python scrub_trace.py traces/raw.json --learn redact.json

# 2. review redact.json BY HAND. Delete every entry that is ConnectWise
#    vocabulary rather than an identity — a status, a priority, a board or
#    type name, a ticket subject that names nobody. Sweeping one of those
#    rewrites the trajectory and the frozen set silently stops matching.
#    Add any identity the proposer missed.

# 3. apply
$env:SCRUB_SALT = "<a secret you do not commit>"
python scrub_trace.py traces/raw.json --redact-file redact.json -o traces/2026-09-03-full-triage.json --verify
```

`--verify` scores the trace before and after and fails if a single check
verdict differs; the residual check then proves every declared literal is gone.
On the September corpus that is 77 literals and 63 identical verdicts.

**The redaction list is itself a catalogue of the customer data.** It is
gitignored (`redact*.json`). Keep it beside the raw export, outside the repo.

Keep the salt out of the repo too — without one the tokens are a plain hash of
the value and reversible by dictionary attack.

### What the proposer filters, and why

| Filtered | Reason |
|---|---|
| `<server>___<tool>`, `cw_*`, `load_skill` | the `call_tool` envelope's `name` is a tool; redacting it breaks unwrapping and rewrites the whole trajectory |
| anything inside a tool definition | `name` and `description` there are schema, not people |
| ISO dates | `2026-07-06` is not a phone number |
| any phrase appearing in a `load_skill` body | the corpus is mostly our own skill files, whose headings ("Priority Matrix", "Status Rules") look exactly like names |

What it cannot do is decide whether `Pro Care` is a product or a person. That
is the review step, and it is why there is one.
