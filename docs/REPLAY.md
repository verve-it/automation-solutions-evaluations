# Record-and-replay: stubbing the tools with recorded output

Answers the question "are the evals running the tools live?"

## What runs live today

| Component | Invokes agents | Calls ConnectWise | Network |
|---|---|---|---|
| `trace_to_eval.py` | no | no | **none** |
| `run_evals.py` | no | no | **none** |
| `replay/make_cassette.py` | no | no | **none** |
| `export_traces.py` | no | no | Log Analytics read |
| `foundry/submit_to_foundry.py` | no | no | judge model — sees recorded text only |
| `replay/replay_server.py` | no | **no** | serves a cassette |
| **`staging-replay.yml`** (`microsoft/ai-agent-evals`) | **yes** | **yes** | yes |

Everything except the last scores **recorded** traces and touches nothing.
`staging-replay.yml` is the exception: it invokes the agents for real, and the
agents call real MCP tools against whatever ConnectWise the project is wired
to. That is why it is pinned to `automation-solutions-test` and the dev
instance, and why there is no production counterpart.

**The stub layer described here is what replaces that live run as the
per-change gate.**

## Correcting the handoff

§1 of `docs/HANDOFF.md` says: *"This is why there is no replay harness and no
stub layer, and why that is the correct design rather than a gap."*

That conflated two different things. What the mutation constraint rules out is
**re-running agents against production ConnectWise**. It does not rule out
replaying them against recorded tool output — that is a different mechanism
with none of the same hazards, and it is strictly better than the live staging
run for gating an agent change:

| | live staging replay | cassette replay |
|---|---|---|
| Data drift between runs | yes — the ticket has been triaged | none |
| Writes performed | yes, to dev ConnectWise | **none** |
| Repeatable | no | yes |
| Needs a dev instance | yes | no |
| Can use production traces | no | **yes** |
| Cost | agent inference + ConnectWise | agent inference only |

The one thing it cannot do is tell you a write still *works*. Keep a
low-frequency live run for that, as a smoke test rather than a gate.

## How it works

```
recorded trace ──► replay/make_cassette.py ──► cassettes/<date>-<op_id>.json
                                              │
agent under test ──► Foundry toolbox ──► replay/replay_server.py (MCP)
                                              │
                                        artifacts/replay-journal.json
```

The agent sees tools with the **same names and the same schemas** as
production. Every call is answered from the recording. No ConnectWise request
is made and no write is performed — a write returns the response the real
write returned.

```powershell
python3 replay/make_cassette.py traces/2026-09-15-ops-worst-case.json -o cassettes/
python3 replay/replay_server.py cassettes/2026-09-15-73d29f4c3a13.json --tool-defs tool_manifests/ --journal artifacts/replay-journal.json
```

### Ordered, not a dictionary

`cw_get_ticket {"ticket_number": 805392}` returns **five different results**
inside one recorded orchestration, because the agents mutate the ticket as they
go. Keyed by (tool, arguments) alone, all five collapse into one and the agent
never sees its own writes land.

So each key holds a **queue**, consumed in recorded order. Arguments are
canonicalised — sorted keys, no whitespace — so an agent that serialises
differently does not diverge for no reason.

### Divergence is the design, not an edge case

You replay precisely when the agent has changed, so calls that were never
recorded are the **common case**. Three outcomes:

| Outcome | When | Response |
|---|---|---|
| `matched` | exact pair recorded, response unconsumed | the recorded response |
| `repeated` | recorded, responses used up | the last one again (`--on-exhausted diverge` to treat it as divergence instead) |
| `diverged` | never recorded | a typed `{"error": "not_recorded"}` — **never a fabrication** |

Returning a plausible-looking answer for an unrecorded call would have the
agent reason over a fiction and the result scored as real behaviour. The server
refuses to do it.

### What you get out

A replayed run is scored on its **matched prefix** and its divergence point:

```json
{"matched": 14, "repeated": 0, "diverged": 1, "matched_prefix": 14,
 "first_divergence": {"tool": "cw_describe", "key": "cw_describe|{...}"},
 "writes_attempted": 1}
```

That answers *"did this change alter the trajectory, and where"* — the right
question for an agent-change gate. It is **not** the same as *"did the agent do
the task well"*, which still needs recorded production traces scored by
`run_evals.py`. Two questions, two mechanisms.

## Two things that must be fixed first

**1. The `cw_query` truncation blocks this.** A cassette built from a
truncated result feeds the agent *less* than the original saw, and the
difference gets scored as the agent's fault. `replay/make_cassette.py --strict`
refuses to write such a cassette, and today that refuses **both** full-triage
orchestrations:

```
SKIP  2026-09-03-bed408b416e8.json   71 interactions, 4 write(s), 3 agent(s)
        seq 24: cw_query result truncated at 8192 chars
```

The ops traces are clean. So the truncation fix — paging or field projection
on `cw_query` — has gone from a data-quality nit to a prerequisite for the
replay gate.

**2. The tool manifest blocks fidelity.** Without schemas the replayed tools
are advertised with an empty `inputSchema`, so the agent is told it may send
anything. It is no longer a faithful stand-in for production, and argument
mistakes that production would reject go unnoticed. `replay/replay_server.py` warns
loudly when this happens. See `tool_manifests/README.md`.

## What has to be wired outside this repo

`replay/run_replay.py` now does steps 2-4 for you. What it cannot do is make
the server reachable.

**The one real constraint: Foundry calls the replay server, not the other way
round.** So the server has to be reachable *from Azure*. `localhost` will not
do, and the driver refuses it rather than letting you discover it as a
timeout inside an agent run — which surfaces as an agent failure rather than
as a configuration mistake. Host `replay/replay_server.py` anywhere with a
public name, or put a tunnel in front of it. It is stdlib-only and stateless
apart from the cassette, and `--token` enables a bearer check.

With that URL in hand:

```bash
python3 replay/run_replay.py \
    --cassette cassettes/2026-09-03-4dda7f4fa5f0.json \
    --agent triage-orchestrator \
    --server-url https://replay.example.net/mcp
```

It reads the agent version under test, clones its definition with **only the
tools swapped** for an MCP tool pointing at the replay server, creates that as
a temporary version tagged `eval-replay-temp`, invokes it, collects
`/summary`, and deletes the temporary version — including on failure.

Everything else in the definition is copied verbatim. A replayed agent
already differs from production by its tool binding; letting the model,
instructions or temperature drift as well makes the comparison meaningless.
A test asserts the swap touches nothing else.

`--dry-run` prints the exact binding it would create and touches nothing.
Use it first.

### Where to host it

**Azure Functions, Flex Consumption, as a custom handler.** Built:
`functions/replay-mcp/`, with `infra/main.bicep` and `deploy.sh`.

```
export REPLAY_TOKEN=$(openssl rand -hex 32)
functions/replay-mcp/deploy.sh <resource-group> eastus2
```

PowerShell, where neither `export` nor `openssl` exists:

```powershell
.\functions\replay-mcp\deploy.ps1 -ResourceGroup <resource-group> -NewToken
```

Two earlier versions of this document were wrong about this, in opposite
directions, and both corrections are worth keeping.

**It said Container Apps, and that Functions was the wrong host.** Wrong:
Functions hosts MCP servers natively and the MCP extension is GA.

**It then said to port the server to the MCP extension's tool triggers.**
Also wrong, and this is the more interesting one. Functions has two MCP
stories and they differ on schema fidelity, not only on state:

| Path | Advertised schema | Fit |
|---|---|---|
| **MCP extension** (tool trigger) | flat `toolProperties`: `propertyName`, `propertyType`, `description`, `isRequired`, `isArray` | right for a *new* server, wrong for a stub |
| **Custom handler** (`mcp-custom-handler` profile) | whatever our server advertises — the manifest, verbatim | **this one** |

There is nowhere in `toolProperties` to put an `enum`. Of the 88 properties
in `tool_manifests/connectwisemcp.json`, 35 are `Optional[X]` and survive as
`isRequired: false` — but **8 are enums and do not survive**, among them
`cw_resolve.reference_type`. Those twenty values are the only reason
`valid_tool_args` is a check that can fail at all; before the MCP server
typed it as a `Literal`, the check passed 100% of a known-bad trace set.

A stub that advertises a looser contract than production tells the agent
under test it may send values production rejects. The divergence that follows
is ours, and the gate blames the agent for it. So the schemas go out verbatim
and the transport is ours — which is exactly the case Microsoft documents as
"Host servers built with MCP SDKs on Azure Functions", with the
`mcp-custom-handler` profile in `host.json` for it.

The extension remains the right answer for `cwpsa-mcp` itself.

### The preview flag, and what it did not explain

`AzureWebJobsFeatureFlags = EnableMcpCustomHandlerPreview` is what Microsoft's
sample carries in `local.settings.json`, and the name says `mcp-custom-handler`
is preview. `infra/main.bicep` sets it.

It explained nothing about our 502. Host `4.1054.250.26428` honoured the
profile with the flag absent:

    1 functions found (Custom)
    Created function http-handler1 for route {*route}
    Using port 8000 specified via configuration for custom handler.

The handler was started and it died on `ModuleNotFoundError: No module named
'mcp_core'`. That is recorded here because the wrong answer was written down
confidently first, from the sample alone, before anyone read a log.

So the accurate statement is narrower: the MCP extension is GA, this profile
is preview-flagged, and we set the flag because the sample does. Worth it -- a
preview flag is a smaller problem than a stub that cannot advertise an enum --
but it is a trade.

### A replay takes the cassette and a URL

```
python3 replay/run_replay.py \
    --cassette cassettes/<cassette-id>.json \
    --server-url https://<app>.azurewebsites.net/mcp/<cassette-id> \
    --token "$REPLAY_TOKEN" --dry-run
```

**Not the agent name, and not the query.** Both are in the recording:
`agents[0]` is the entry agent and `query` is what it was given. Asking a
caller to retype the query invites replaying a slightly different question
than the one recorded, and the gate would score the difference as the agent's.

A cassette with several agents replays by running its **entry** agent. The
children are reached over A2A, which `make_cassette.py` deliberately leaves
out of the toolbox, so each child is replayed as its own agent run rather than
answered from the cassette.

`AZURE_AI_PROJECT_ENDPOINT` is the one thing the caller supplies, and it is
not deployed with the replay server on purpose: traffic is one way. Foundry
calls the replay server; the server never calls Foundry, so the value would be
dead config in the function app. CI already holds it as a GitHub Actions
variable on the staging and production environments.

### State, and why it is not in the process

`Cassette.cursor` is a per-key queue position: the same call appears several
times in one run with different responses, and position is how the right one
comes back. Two independent cursors return a plausible score that means
nothing.

Flex Consumption will not scale out a single sequential client in practice,
but a gate does not rest on "in practice", and the floor for
`maximumInstanceCount` on that plan is **40** — pinning to one instance is not
on offer. So the cursor and journal live in blob storage, guarded by an ETag
(`replay/state_store.py`), keyed by the MCP session id that `initialize`
issues. One session is one replay, which is the lifetime the cursor should
have. A lost update returns a 409 saying the replay is unordered rather than
an answer that looks fine, and the journal survives an instance recycle, which
is what makes `/summary` worth reading afterwards.

Whatever hosts it, use `--token` and pass the bearer to Foundry. `/summary`
is the journal — tool names, canonicalised arguments carrying ticket and
company identifiers, every attempted write.

### Where the results live

Short answer: **in the project, not on either agent.**

Foundry evaluation objects are project-scoped. `evals.create(name=...)` takes
a **dataset** as its data source; it takes no agent id. So an eval is not
owned by the agent under test, and it is certainly not owned by the
temporary clone. Deleting that clone loses no results — there were never any
attached to it.

What *is* stamped with the temporary version is the **trace**. App Insights
records `gen_ai.agent.id` and its version on every span. Delete the version
and that id resolves to nothing: six weeks later a trace names an agent that
cannot be looked up, and nothing says what it was a clone of.

That is why `run_replay.py` writes `artifacts/replay-run.json`:

```json
{
  "agent": "triage-orchestrator",
  "base_version": "82",
  "temp_version": "87",
  "temp_version_deleted": true,
  "cassette": "2026-09-03-4dda7f4fa5f0.json",
  "tools": "stubbed — no ConnectWise request, no write performed",
  "matched_prefix": 50,
  "suggested_eval_name": "replay-triage-orchestrator-v82"
}
```

The ephemeral clone is a **fixture**, not the subject. Name any eval or
dataset built from a replay after `base_version` — the version you are
actually testing. Naming it after the clone produces a project full of eval
runs pointing at agent versions that no longer exist.

The same metadata goes onto the temporary version while it lives
(`base_version`, `cassette`), which is what lets you identify a stray one if
a run is killed before teardown.

### Foundry does not host this for you

Worth stating, because the naming invites the opposite conclusion. The
**Foundry MCP Server** that shipped in preview is Foundry's *own* management
tools, for driving Foundry from an agent or IDE. It is not a place to host
your MCP server. **Toolboxes** are the registration and versioning layer that
points at an endpoint — also not a host.

Custom tool servers are yours to host. Functions is where Microsoft says to
put them.

### On Microsoft's caution about mocks

Foundry's evaluation guidance warns that *"a mock that simplifies a tool
response or a test harness that skips authentication can hide exactly the
bugs you're trying to catch"*, and says evals should use the same APIs, tools
and surfaces as production. That caution is aimed squarely at something like
this, so it is worth being explicit about why this design answers it rather
than ignoring it.

- **Nothing is simplified.** A cassette replays the bytes the real tool
  returned, truncation, error envelopes and all. It is a recording, not a
  hand-written fixture.
- **The surface is identical.** Same MCP protocol, same tool names, same
  schemas — the schemas come from `--tool-defs`, the same manifest the live
  validator uses. Keeping those identical is what stops the binding
  difference from mattering.
- **The bugs a stub genuinely hides are still tested.** Auth, ConnectWise
  schema drift and the write path are exactly what the nightly trace scoring
  and the weekly live staging run cover. The stub is not a replacement for
  those, and this repo keeps both.

What the stub buys that nothing else does is the thing the caution cannot
address: the same input produces the same trajectory, so a difference between
two agent versions is attributable to the change rather than to the ticket
having moved on overnight.

### The honest caveat

Driving the session to completion is the one step that could not be verified
without a live project. The binding, the temp-version lifecycle and the
teardown are all in place and tested offline; if the SDK's session surface
differs from what the script expects, that is the line to adjust, and the
script says so where it happens rather than failing silently.

## Where this leaves the gate design

| Gate | Mechanism | Frequency |
|---|---|---|
| Eval-code change | frozen sets vs frozen baselines | every push |
| **Agent change** | **cassette replay, matched prefix + divergence** | **every change** |
| Agent change, judged | `ai-agent-evals` in staging | before release |
| Production behaviour | recorded traces, `run_evals.py` | nightly |
| Write path still works | one live staging run | weekly smoke test |

The live staging replay drops from "the agent-change gate" to "a smoke test",
which is where it belongs: it is the slowest, the most expensive, the least
repeatable, and the only one that can leave state behind.
