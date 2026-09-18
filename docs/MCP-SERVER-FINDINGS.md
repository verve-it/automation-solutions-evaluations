# Findings for `cwpsa-mcp`

> ## Status: all four fixed upstream, 2026-09-17
>
> | # | Finding | Outcome |
> |---|---|---|
> | 1 | `reference_type` unconstrained | **Fixed.** `Literal` spelled out, `REFERENCE_TYPES` derived from it via `get_args`. A `BeforeValidator` normalizes case *before* the enum check, so `"Company"` keeps working — the casing caveat below is resolved, not accepted. |
> | 2 | `context` silently dropped | **Fixed.** Honoured or refused, never dropped. `type`/`subtype`/`item`/`status` route through `resolve_board_scoped()` to `/service/boards/{id}/…`. |
> | 3 | numeric `query` returns empty | **Fixed.** Digits short-circuit to a by-id fetch; 404 → empty list, other errors propagate. |
> | 4 | `validation_error` for an unanswerable scan | **Fixed.** New `not_supported` code. |
>
> **Rejected, correctly: the `entity` enum** (the closing suggestion under #1).
> ~36k tokens across 7 tools on *every* request. That is roughly 10% of the
> analysis agent's peak context spent on a constant, to catch a class of error
> the server already reports clearly. A test in `cwpsa-mcp` documents the
> reasoning so nobody re-litigates it from this file. Read #1's last paragraph
> with that settled.
>
> Three further fixes came out of the same pass, found upstream rather than
> here: five reference endpoints pointed at paths that do not exist; 164
> entities gained `get` in the registry; and projections now come from the
> registry instead of a hardcoded `id,identifier,name` (only three of sixteen
> types have an `identifier`). That last one surfaced a real bug — a
> ConnectWise member has `firstName`/`lastName` and no `name`, so the generic
> `_match(key="name")` over `/system/members` could never match. `company`,
> `member`, `board` and `priority` now route to their own resolvers.
>
> ### One caveat, for us not for them
>
> The `BeforeValidator` fixes the **runtime**. It is invisible in the
> **schema**: the generated `inputSchema` is a bare lowercase `enum`, so
> anything validating against the contract rather than calling the server —
> our `valid_tool_args`, Foundry's Tool Input Accuracy, a strict MCP client —
> would flag `"Company"` as an enum violation the server would have accepted.
>
> Verified on fastmcp 4.0.5: server accepts `"Company"` and `"  COMPANY  "`,
> rejects `"severity"`; schema shows only the lowercase values.
>
> Latent, not live. Every `reference_type` in both frozen trace sets is already
> lowercase (`company` x5, `priority` x4, `board` x3, `status` x3, `contact`,
> `site`, `type`, `subtype`, `item` x2 each, `severity` x1). If a
> case-mismatch false positive ever shows up in `valid_tool_args`, this is why,
> and the fix is on our side — case-fold before the enum comparison.
>
> ### Landed here
>
> `cbf4e2b` is on `origin/main` and `tool_manifests/connectwisemcp.json` is
> re-extracted from it. The only change to the tool contract is the enum —
> 20 tools before and after, no added or removed parameters, no description
> churn. The registry, projection and routing fixes are all internal.
>
> `valid_tool_args` went 6/6 → **5/6** on full-triage: `"severity"` is now
> caught by the schema, before the call. `ops-worst-case` stays 2/2, correctly
> — those were resolver bugs, not argument bugs.
>
> `extract_tool_manifest.py --from-source` now does the extraction that was
> ad-hoc the first time, stubbing secrets discovered by reading `config.py`.
>
> > Until then everything below describes `3987f59` and is kept as the record of
> what was found and why.

---


Four issues found while filling `tool_manifests/` from the FastMCP registry of
`github.com/EliSeale/cwpsa-mcp` at `3987f59` and re-scoring the two frozen
trace sets against the real schemas.

Evidence is the September production traces in `traces/`, scored by
`run_evals.py`. Every argument quoted below is copied from a real run.

Ranked by leverage. #1 is a contract change; #2 and #3 are bugs with silent
failure modes; #4 is a papercut.

---

## 1. `cw_resolve.reference_type` is an unconstrained `str`

`src/cwpsa/tools/tier1/resolve.py:38`

```python
async def cw_resolve(
    reference_type: str,      # <-- no Literal, so no enum in the schema
    query: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
```

FastMCP generates:

```json
"reference_type": { "type": "string", "description": "What to resolve.  One of:\n\"company\" — ..." }
```

The valid set is listed only in the docstring, which lands in `description` as
prose. Two consequences:

**The agent never sees the closed set as data.** It sees twenty-odd lines of
English inside a larger tool description, in a context window that by then
holds 280k tokens. In the traces it invented `reference_type: "severity"`,
which no agent would have written against an `enum`.

**Nothing downstream can validate it.** Argument validation generated from the
schema — ours, or Foundry's Tool Input Accuracy evaluator, or any MCP client
that checks — passes `"severity"` because the schema permits any string.

### The set is already closed

It is exactly the keys of two dicts in `src/cwpsa/resolution/engine.py`:

| source | values |
|---|---|
| `_DEDICATED_RESOLVERS` | `agreement`, `configuration`, `contact`, `site` |
| `_REFERENCE_ENDPOINTS` | `agreement_type`, `board`, `company`, `configuration_type`, `department`, `item`, `location`, `manufacturer`, `member`, `priority`, `sla`, `status`, `subtype`, `type`, `work_role`, `work_type` |

Anything else hits `return {"error": f"Unknown reference type '{reference_type}'."}`
at `engine.py:621`. Twenty values, known at import time.

### Suggested fix

```python
ReferenceType = Literal[
    "agreement", "agreement_type", "board", "company", "configuration",
    "configuration_type", "contact", "department", "item", "location",
    "manufacturer", "member", "priority", "site", "sla", "status",
    "subtype", "type", "work_role", "work_type",
]

async def cw_resolve(
    reference_type: ReferenceType,
    query: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
```

FastMCP emits `"enum": [...]` from a `Literal`. Ideally derive it from the
dicts so the three can't drift:

```python
ReferenceType = Literal[tuple(sorted({*_DEDICATED_RESOLVERS, *_REFERENCE_ENDPOINTS}))]  # type: ignore[valid-type]
```

Verified: `Literal[tuple(...)]` is a legal runtime subscript and FastMCP reads
the annotation at registration, so the generated `inputSchema` is exactly

```json
"reference_type": { "enum": ["agreement", "board", "company", ...], "type": "string" }
```

Checked on fastmcp 4.0.5; the repo pins `fastmcp>=3.4.2`, so confirm on
whichever version is deployed. Static type checkers reject the dynamic
subscript — hence the `type: ignore` — so if that bothers you, write the
twenty values out literally and add a test asserting the `Literal` args equal
the union of the two dicts.

Note `reference_type.lower().strip()` at `resolve.py:100` means a `Literal` is
stricter than today's runtime: `"Company"` currently works and would start
being rejected by the client's schema check. That is probably what you want —
but it is a behaviour change, so flag it if any caller relies on the casing
tolerance.

The same argument applies to **`cw_query.entity`** and **`cw_describe.entity`**,
both `{"type": "string"}` today. If the set of supported ConnectWise API paths
is enumerable, a `Literal` there is worth more than on `reference_type`,
because `entity` is the most frequently wrong argument in the traces.

---

## 2. `resolve_reference` silently discards `context`

`src/cwpsa/resolution/engine.py:598`

```python
async def resolve_reference(reference_type, query, context=None):
    rt = reference_type.lower().strip()

    if rt == "status" and context and context.get("board"):
        return await resolve_board_status(context["board"], query)

    dedicated = _DEDICATED_RESOLVERS.get(rt)
    if dedicated is not None:
        return await dedicated(query, context)

    endpoint = _REFERENCE_ENDPOINTS.get(rt)
    ...
    rows = await cw_get(endpoint, fields="id,identifier,name",
                        pageSize=_REFERENCE_PAGE_SIZE)   # context is gone
    return _match(rows, query, key="name")
```

`context` is read for `status`, passed through for the four dedicated
resolvers, and **dropped for the other sixteen**.

`type`, `subtype` and `item` are board-scoped in ConnectWise. `/service/types`
without a `board/id` condition is a different question from the one asked.

### What it looked like in production

Ops run `73d29f4c`, in order, each returning zero matches:

```
cw_resolve  type     "Incident"            {"board": "Internal"}
cw_resolve  subtype  "End User / Endpoint" {"board": "Internal", "type": "Incident"}
cw_resolve  item     "Email"               {"board": "Internal", "type": "Incident", "subtype": "End User / Endpoint"}
```

The agent is doing exactly the right thing — walking the board → type →
subtype → item hierarchy, narrowing `context` at each step. All three come back
empty. It then retried the same three calls with a shorter `context`, which is
where the four-deep `cw_resolve` cascade in that run comes from.

None of this errors. The agent gets `{"count": 0, "matches": []}` and, per the
docstring, correctly does not invent an ID — so the run proceeds without the
type it needed.

### Suggested fix

Either honour `context` for the board-scoped types:

```python
_BOARD_SCOPED = {"type", "subtype", "item", "status"}

if rt in _BOARD_SCOPED:
    board = (context or {}).get("board")
    if not board:
        return validation_error(
            f"Resolving a {rt} requires context.board — these are board-scoped "
            "in ConnectWise and a tenant-wide lookup would be ambiguous.",
            suggestions=[f"cw_resolve('{rt}', '{query}', {{'board': '<board name>'}})"],
        )
    board_id = await _board_scope(board)
    rows = await cw_get(endpoint, conditions=f"board/id={board_id}",
                        fields="id,identifier,name", pageSize=_REFERENCE_PAGE_SIZE)
```

— or, if the endpoint genuinely cannot be scoped, reject the argument rather
than silently ignoring it. Accepting `context` and not using it is the part
that costs debugging time: there is no signal anywhere that the narrowing was
dropped.

`subtype` and `item` are nested deeper still (`subtype` under a `type`, `item`
under a `subtype`), so the same treatment applies to `context.type` and
`context.subtype`, which the traces show the agent already supplying.

---

## 3. `query` accepts a name; the agents pass IDs and get silence

No resolver treats a numeric `query` as an id. `_company_scope` does — it has
`if text.isdigit(): return int(text), None` at `engine.py:196` — so
`context.company` accepts `"4597"` while `query` does not.

From the same ops run:

```
cw_resolve  company  "4597"                          -> 0 matches
cw_resolve  contact  "13649"  {"company": "4597"}    -> 0 matches
cw_resolve  site     "5173"   {"company": "4597"}    -> 0 matches
```

`resolve_site` builds `name contains "5173"`. There is no site named 5173.

The agent had the ids — they came out of a ticket payload — and reached for
`cw_resolve` to expand them into `{id, name}`. That is a reasonable reading of
"Resolve a fuzzy name or phrase to ConnectWise IDs and exact values", and the
inconsistency with `context.company` reinforces it.

### Suggested fix

Short-circuit a digits-only `query` to a direct id fetch, which is one request
and cannot be ambiguous:

```python
if query.strip().isdigit():
    row = await cw_get(f"{endpoint}/{int(query)}", fields=...)
    return [row] if row else []
```

If you would rather keep `cw_resolve` name-only, then say so in the return
instead of returning empty:

```python
{"error": "query looks like an id. cw_resolve maps names to ids; "
          "use cw_get('company/companies', 4597) to go the other way."}
```

Either is fine. Silence is what costs the run — an empty result is
indistinguishable from "this record does not exist", and the agent's skill
correctly forbids inventing an ID, so it stalls.

---

## 4. A full page of reference rows returns a `validation_error`

`engine.py:630`

```python
if len(rows) >= _REFERENCE_PAGE_SIZE:
    return validation_error(f"Reference type '{rt}' has more than 500 records ...")
```

The reasoning in the comment is right — a truncated scan makes a non-match a
lie, so failing loudly beats answering wrongly. But `validation_error` tells
the caller it sent a bad argument, and it didn't; the argument was fine and the
server can't answer. The agent's recovery path for "bad argument" is to change
the argument, which cannot help here.

Worth a distinct error shape so a caller can tell "you asked wrong" from "I
can't answer this" — the suggestions already say the right thing
(`cw_query` directly / add a dedicated resolver), they're just filed under the
wrong heading. Low priority.

---

## Why #1 matters to us specifically

We score these agents from telemetry. With a schema that declares enums,
argument validation is **generated** — required params, types, enums,
unexpected fields, per tool, automatically, for every agent and every future
flow, with nobody writing a check. That is also the six criteria Foundry's
Tool Input Accuracy evaluator applies.

With every parameter declared as a bare `string`, there is nothing to generate
from. We filled the manifest with all 20 tools and the check now passes 100% of
a trace set that was deliberately chosen because it is full of bad calls. The
only thing catching `"severity"` today is a string match on the server's own
error text — which means we catch the failure **after** the call, never before,
and we'd catch nothing at all if the error message were reworded.

`#2` and `#3` are the more expensive bugs in production. `#1` is the one that
changes what the eval suite can do.
