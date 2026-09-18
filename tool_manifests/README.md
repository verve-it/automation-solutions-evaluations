# MCP tool manifests

`connectwisemcp.json` holds the real schemas for all 20 ConnectWise PSA tools,
including the 20-value `enum` on `cw_resolve.reference_type` that landed in
`cwpsa-mcp` at `cbf4e2b`. Generated argument validation is real: it catches a
bad `reference_type` **from the contract, before the call**, rather than by
string-matching the server's error text after it.

## What is here

| | |
|---|---|
| `connectwisemcp.json` | 20 tools, `versions: ["*"]` |
| source | FastMCP tool registry of `github.com/EliSeale/cwpsa-mcp` at `cbf4e2b`, read in-process 2026-09-17 |
| `tests/fixtures/connectwisemcp-v1-partial.json` | two hand-written tools, mechanism test only — **never score with it** |

Regenerate with:

```bash
python3 tools/extract_tool_manifest.py --from-source /path/to/cwpsa-mcp \
    --toolbox ConnectwiseMCP -o tool_manifests/connectwisemcp.json
```

That registers every `register(mcp)` under `src/cwpsa/tools/tier*/` against a
`FastMCP` instance, stubbing each secret `config.py` asks for (discovered by
reading the file, so a new secret upstream does not break extraction), and
reads back the generated `inputSchema`. Same schema FastMCP serves from
`tools/list` — but from source, not from the deployed toolbox revision. If the
deployment lags the repo, this lies. See **Re-extracting** below for the routes
that read the deployed contract instead.

## Where it stands

```
                        no manifest   3987f59     cbf4e2b
full-triage
  evaluator_ready         2/7          6/7         6/7
  valid_tool_args         not scored   6/6         5/6
ops-worst-case
  evaluator_ready         0/2          2/2         2/2
  valid_tool_args         not scored   2/2         2/2
```

Two things happened, and it is worth being precise about which is which.

**`evaluator_ready` went 2/7 → 6/7 when the directory was filled.** Runs carry
schemas, so the Foundry tool-quality evaluators have something to read.

**`valid_tool_args` went 6/6 → 5/6 when the enum landed.** A check going *down*
is the good news here: at `3987f59` it passed every run in a trace set chosen
for being full of bad calls, and said nothing about it. Now:

```
valid_tool_args: 1 bad argument(s) in 8 validated call(s):
  cw_resolve: 'reference_type'='severity' not in [agreement, agreement_type,
  board, company, configuration, configuration_type, +14 more]
```

### The known-bad set still passes, and that is correct

`ops-worst-case` is 2/2 on `valid_tool_args` and always should be. Its
`cw_resolve` failures used `type`, `subtype`, `item` and `site` — all **valid**
reference types. Those runs failed because `resolve_reference` dropped
`context` (fixed upstream in `cbf4e2b`), and because a numeric `query` was
never treated as an id. Behavioural bugs, not argument bugs. No schema of any
kind catches them, and a check that appeared to would be reading tea leaves.
`no_wasted_calls`, `no_dead_ends` and `no_search_cascade` catch them, which is
their job.

`tests/test_frozen_sets.py` locks both halves down: the enum must fire on
`"severity"`, and it must not fire on the resolver bugs.

## What §6 of the handoff got right and wrong

> Every `cw_resolve` failure in the baseline would have been caught by a
> generated check.

Half right, and the half that was wrong mattered. Of the fourteen tool errors
in the two frozen sets, exactly **one** is an argument error, and generated
validation now catches it. The other thirteen — the empty resolves, the ids
passed as names, the malformed `cw_query` filters, the 404 hrefs — are not
argument errors and never were. §6 was measured against
`connectwisemcp-v1-partial.json`, a hand-written fixture that invented the
enum, so it read one mechanism demo as coverage of all fourteen.

The general claim holds: with enums in the contract, validation is **generated
rather than written**, per tool, automatically, for agents nobody has written a
check for yet. That is the thing that does not grow linearly with the agent
count. It just does not retroactively cover bugs that live in the server's
behaviour rather than in its arguments.

## Still unconstrained

`cw_query.entity`, `cw_describe.entity` and `cw_update.entity` are free
strings, and `cw_query.filter` is a free object. That covers the malformed
filters and the bad entity path in the frozen sets.

An `entity` enum was **proposed and correctly rejected** upstream: ~36k tokens
across 7 tools on *every* request, roughly 10% of the analysis agent's peak
context spent on a constant, to catch a class of error the server already
reports clearly. `cwpsa-mcp` carries a test documenting the reasoning. Do not
re-propose it from `docs/MCP-SERVER-FINDINGS.md` without reading that first.

So `valid_tool_args` covers **arity, types, required params, unexpected fields,
and `reference_type`**. Not entity paths, not filter shapes. That is the honest
boundary.

## One case-folding caveat

The upstream `BeforeValidator` normalizes case before the enum check, so the
server accepts `"Company"`. That normalization is **invisible in the schema** —
the generated `inputSchema` lists lowercase values only. So this check would
flag `"Company"` as a violation the server would have accepted.

Latent, not live: every `reference_type` in both frozen sets is already
lowercase. If a case-mismatch false positive ever shows up, the fix is here —
case-fold before comparing — not upstream.

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
`additionalProperties: false`, `type`, and `enum`. Anything it does not
understand it declines to judge rather than failing a run on a keyword it got
wrong.

## The version in the URL is a binding revision, not a schema version

In the frozen full-triage set the analysis agent shows ConnectwiseMCP **v5**
and the ops agent **v1** — same toolbox, same orchestration. The `cw_resolve`,
`cw_get_ticket` and `load_skill` descriptions are **byte-identical** across
both, so the number tracks when each agent's binding was last edited, not the
tool contract. Hence `"versions": ["*"]`. Pin to specific revisions only with
evidence the contract actually differs; the converter records the revisions
each run used either way.

## Re-extracting

If the server changes, refresh the manifest. Best route first.

**1. `tools/list` against the running MCP server.** This is the ground truth —
it is the deployed contract, not the source.

```powershell
python3 tools/extract_tool_manifest.py --from-url https://<mcp-host>/mcp \
    --toolbox ConnectwiseMCP -o tool_manifests/connectwisemcp.json
```

The Foundry toolbox endpoint
(`/api/projects/<p>/toolboxes/<t>/versions/<v>/mcp`) rejects both `az`
tokens and API keys with `AgenticIdentityToken` — it is reachable only from
inside an agent run. Go at the MCP server directly.

**2. From a saved `tools/list` dump or portal export:**

```powershell
python3 tools/extract_tool_manifest.py --from-tools-list tools-list.json --toolbox ConnectwiseMCP -o tool_manifests/connectwisemcp.json
```

**3. From source**, which is how the current file was made:

```bash
python3 tools/extract_tool_manifest.py --from-source /path/to/cwpsa-mcp \
    --toolbox ConnectwiseMCP -o tool_manifests/connectwisemcp.json
```

Needs no credentials and no network, and is exact as long as the deployment
matches the commit. Check `source` in the manifest against `git log` in the
server repo before trusting a score.

**4. A skeleton from the traces** — tool names and observed argument keys,
`parameters: null` throughout, so the validator skips rather than guesses:

```bash
make manifest-skeleton
```
