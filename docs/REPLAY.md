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

Microsoft supports remote MCP servers on Azure Functions two ways, and the
difference matters here more than it looks.

**The self-hosted / BYO path** (public preview) deploys a server built with
the MCP SDKs to Flex Consumption with roughly one line of change. It is the
obvious fit, and it is the wrong one: **stateful execution is not supported
for the self-hosted option in preview**, and this server is stateful.

`Cassette.cursor` is a per-key queue position held in memory
(`replay/replay_server.py`). It is what makes a cassette *ordered rather than
a dictionary* — the same call can appear several times in one run with
different responses, and position is how the right one is returned. Under
scale-out, two instances hold two independent cursors, the same call gets
answered from two different positions, and the replay reports a plausible
score that means nothing. It fails silently, which is the worst shape a
failure can take in a gate.

So, in order of preference:

| Option | Fit | Why |
|---|---|---|
| **Azure Container Apps, min=max=1 replica** | best | Runs the existing stdlib server unchanged. Stateful, scales to zero, Microsoft-native. |
| App Service, single instance, `ARR affinity` off | fine | Same reasoning, more always-on cost. |
| Azure Functions, **MCP extension** | possible | This is Microsoft's stateful MCP path. Means rewriting the server in the Functions programming model. |
| Azure Functions, **self-hosted/BYO** | **no** | Stateless only in preview. Correct until it scales out, then quietly wrong. |

If Functions is a hard requirement, the honest fix is to stop holding the
cursor in memory: key it by MCP session id and put it in Table Storage or
Redis. That is a real change to `Cassette`, not a deployment setting, and it
is not worth doing unless Container Apps is unavailable.

Whatever hosts it, use `--token` and pass the bearer to Foundry. `/summary` is
the journal — tool names, canonicalised arguments carrying ticket and company
identifiers, every attempted write.

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
