# Staying migratable: dataset construction and cassette replay

We own these two because no native equivalent exists (see
[`NATIVE-RESEARCH.md`](NATIVE-RESEARCH.md)). The question this answers is
different: **when Microsoft does ship one, how do we make the switch cheap?**

The answer in both cases is the same shape — match the conventions Microsoft
and the ecosystem are converging on, so the thing we own is a *producer* of a
standard format rather than a private format with a private consumer.

---

## Where we already are, and it is better than expected

Our **input** side is already convention-aligned, by accident of Foundry
emitting it:

| Attribute in our traces | Convention |
|---|---|
| `gen_ai.tool.name` x230 | OTel GenAI, and what the MCP conventions say to use |
| `mcp.method.name` x108 | OTel MCP, **required** attribute (`tools/call`, `initialize`) |
| `mcp.protocol.version` x5 | OTel MCP |

Worth knowing: the MCP conventions explicitly say to use `mcp.method.name`
and `gen_ai.tool.name`, and **not** `mcp.tool.name`, which the convention
does not define. Anything we write that invents a tool-name attribute is
inventing a divergence.

The span tree the conventions model — `invoke_agent` with nested `chat` and
`execute_tool` spans — is exactly the tree `trace_to_eval.py` walks. Our
converter is not a private interpretation; it reads the standard shape.

---

## 1. Dataset construction

### The moving target

OTel GenAI and MCP semantic conventions are at **Development** stability. In
v1.42.0 (June 2026) they moved out of the core semantic-conventions repo into
a dedicated `semantic-conventions-genai` repo *specifically so they can
iterate faster than the core stability bar allows*. There are **no releases
or tags yet**, so there is no versioned schema URL to pin against.

That is the risk to manage. Not that a native converter arrives — that the
attribute names underneath ours change first.

### The seam

`trace_to_eval.py` already isolates attribute names as module constants
(`K_TOOL`, `K_TOOL_ARGS`, `K_TOOL_RES`, `K_AGENT`, …). That is the seam, and
it is the right one. A convention change is a change to those constants, not
to the walk.

**Action: record which convention the constants encode.** They currently
encode "whatever Foundry emitted in September 2026". When a versioned GenAI
semconv release lands, the diff between it and that note is the migration.

### Field naming on the way out

Foundry's evaluation data uses `{{item.query}}` for input fields and
`{{sample.output_items}}` / `{{sample.output_text}}` for the agent response;
when evaluating traces the field names are used directly with no prefix.

Our rows carry `messages`, `tool_definitions`, `tool_outcomes`,
`expected_actions`. `tool_definitions` is already the canonical name.
`tool_outcomes` is ours — a compacted per-call summary that exists because a
1.1 MB row was rejected by the datasource validator with a 500.

**Deliberately not changing this now.** Adding canonical aliases means
re-adding the full tool-call payloads that had to be trimmed. The mapping is
one function, and it is cheaper to write it when there is something to map
*to* than to carry the row weight for a year on a guess.

The mapping, for when that day comes:

| ours | canonical |
|---|---|
| `messages` | `query` + `response` (already the source they are built from) |
| `tool_outcomes` | `tool_calls` — re-expand from the trace, not from the row |
| `tool_definitions` | `tool_definitions` — no change |
| `expected_actions` | `ground_truth` |

---

## 2. Cassette replay

### There is no Microsoft format. There is an ecosystem convergence.

Microsoft ships no record/replay for MCP. Several independent
implementations do, and they have converged on the same shape:

| Project | Format |
|---|---|
| `mcp-replay` | JSONL, meta header first line, schema id `mcp-replay/cassette@1` |
| `mcpcassette` | JSONL, raw JSON-RPC 2.0 messages |
| `mcp-cassette` (vcrpy for MCP) | transport level, diffable, committable |
| Agent VCR (Capital One) | `.vcr`, shared across Python and TypeScript |

The common denominator: **JSONL, one interaction per line, raw JSON-RPC
preserved, a header declaring a schema identifier.**

### Where ours differs, and whether that is wrong

Ours is a single JSON object with an `interactions` array, and each
interaction is *semantic* — `tool`, `arguments`, `result`, `is_write`,
`error_kind`, `truncated`, `key` — rather than raw JSON-RPC.

That is not an accident and mostly should not change:

- `is_write` drives the "no write is ever performed" guarantee.
- `key` is the canonicalised match key the ordered queue uses.
- `error_kind` and `truncated` are what the checks read.

But it has a real cost: **a raw-JSON-RPC cassette is replayable by any MCP
replayer; ours needs our server.** That is the lock-in, and it is ours, not
Microsoft's.

### What was changed now, and what was not

**Changed:** the cassette header declares a schema identifier and the MCP
protocol version, so a future reader can recognise it and so a converter has
something to branch on. Cheap, no behaviour change.

**Not changed:** raw JSON-RPC is not recoverable from traces. The spans carry
`gen_ai.tool.call.arguments` and the result — not the JSON-RPC envelope, ids
included. A JSONL export can be *synthesised* with generated ids, and that
is the honest shape of the eventual converter: mechanical, and lossy on ids
that never existed in the recording.

That converter is perhaps fifty lines. Writing it now, against four
third-party formats and no Microsoft one, would be guessing at which target
matters.

### Hosting: native, and not the extension

`functions/replay-mcp/` runs the replay server on Azure Functions, Flex
Consumption, as a **custom handler** — the shape Microsoft documents for
hosting a server built with an MCP SDK, with the `mcp-custom-handler`
configuration profile in `host.json`. That profile is preview-flagged:
`AzureWebJobsFeatureFlags=EnableMcpCustomHandlerPreview`, which Bicep sets
because Microsoft's sample does -- though the host honoured the profile
without it.

The Functions **MCP extension** was the earlier recommendation here and it was
wrong. Its `toolProperties` is a flat list of `{propertyName, propertyType,
description, isRequired, isArray}`; there is no `enum`. Of 88 advertised
properties in `tool_manifests/connectwisemcp.json`, 35 are `Optional[X]` and
survive as `isRequired: false`, but **8 are enums and do not survive at all** —
among them `cw_resolve.reference_type`, whose twenty values are the only reason
`valid_tool_args` is a check that can fail. A stub advertising a looser
contract than production invites divergence it then blames on the agent.

This is a hosting decision, not a format one: the extension stays the right
answer for `cwpsa-mcp` itself if it ever moves to Functions.

### The interface is already native

Worth separating from the file format: `replay_server.py` speaks **MCP
streamable HTTP**, the same protocol a real toolbox speaks, which is why an
agent can be bound to it by swapping one `server_url`. If Microsoft ships a
native stub, the agent-side binding does not change at all — only what sits
behind the URL. The expensive part of a migration is already absorbed.

---

## The three things that would actually make a migration hard

Ranked, because not all lock-in is equal.

1. **Cassette format divergence.** The JSONL-plus-raw-JSON-RPC convention is
   where four independent projects landed. Ours is readable and diffable but
   is not that. A converter is the answer, written when there is a target.
2. **`tool_outcomes` as a private column.** Compacted for a real reason, but
   it means our dataset is not directly consumable by an evaluator that
   expects `tool_calls`. The mapping above is the fix.
3. **Unversioned semantic conventions.** The least visible and the most
   likely to bite: the conventions are explicitly iterating fast, and nothing
   in this repo records which snapshot our constants encode.

Nothing here is urgent. All three are cheap now and expensive if discovered
during a migration under time pressure.
