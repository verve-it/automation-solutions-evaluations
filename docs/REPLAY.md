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
| `replay/run_replay.py` | yes | **no** | the replay server only |

Every row performs no write. `run_replay.py` is the only one that invokes an
agent at all, and it binds that agent to the stub first.

There used to be one exception: `staging-replay.yml` ran
`microsoft/ai-agent-evals`, which invoked the agents for real against the dev
ConnectWise instance. **It is removed.** Nothing in this repo may reach a
system of record — an eval that writes to one is not an eval. The stub layer
described here is the per-change gate, and the only one.

## Correcting the handoff

§1 of `docs/HANDOFF.md` says: *"This is why there is no replay harness and no
stub layer, and why that is the correct design rather than a gap."*

That conflated two different things. What the mutation constraint rules out is
**re-running agents against production ConnectWise**. It does not rule out
replaying them against recorded tool output — that is a different mechanism
with none of the same hazards, and it is strictly better than the live staging
run for gating an agent change:

| | live staging replay (removed) | cassette replay |
|---|---|---|
| Data drift between runs | yes — the ticket has been triaged | none |
| Writes performed | yes, to dev ConnectWise | **none** |
| Repeatable | no | yes |
| Needs a dev instance | yes | no |
| Can use production traces | no | **yes** |
| Cost | agent inference + ConnectWise | agent inference only |

The left column is why it was removed rather than kept as a smoke test.

The one thing it cannot do is tell you a write still *works* against the live
service. Nothing in this repo answers that by writing, at any frequency: it is
the tool server's own tests' question, not an agent evaluation's.

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

That is the server's summary. The run manifest, which the gate uploads, keeps
only `seq`, `tool`, `outcome` and `reason` of `first_divergence`: `key` is the
call's arguments, which are recorded content.

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
    --cassette cassettes/2026-09-15-73d29f4c3a13.json \
    --agent connectwise-operations-agent \
    --server-url https://<app>.azurewebsites.net/mcp/2026-09-15-73d29f4c3a13
```

It reads the agent version under test, clones its definition with **only the
tools swapped** for an MCP tool pointing at the replay server, creates that as
a temporary version **of `<agent>-replay`** tagged `eval-replay-temp`, checks
that name now resolves to the clone, invokes it, collects `/summary`, and
deletes the temporary version -- or the whole replay agent, when the run
created it -- including on failure.

**Never a version of the agent under test.** A new version is what an agent's
name resolves to while it exists (only drafts are excluded, and hosted
versions cannot be drafts), so a clone of `connectwise-operations-agent` would
answer that agent's production traffic from the recording for the length of
the replay — writes silently dropped. `<agent>-replay` is called by nothing
else, and nothing has to be set up for it: adding its first version creates
it (how Microsoft's hosted-agent sample creates an agent), and a run that
created it deletes the whole agent afterwards. So any agent can be replayed
with no per-agent setup, and between runs there is no `-replay` agent for
anything to call by name. One that already exists is kept, minus the run's
version.

The tool is bound under the recording's `server_label`, read from the
cassette's `ConnectWise-PSA-ForAgents___*` calls: Foundry prefixes every MCP
tool with it, so another label renames every tool the agent's skills mention.

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

### Binding is per agent kind

A replay points the agent at the replay server. Where that binding lives
depends on what kind of agent it is, so `run_replay.py` handles each kind
explicitly rather than assuming one shape:

| Kind | Where the tools are | How a replay rebinds it |
|---|---|---|
| `prompt`, `voice` | `tools` in the definition | swap `tools`, copy every other field verbatim |
| `hosted` | inside the uploaded code | set an environment variable, re-upload the **same code bytes** |
| `workflow`, `external` | neither | refused by name |

Every triage agent in this project is **hosted** — `--describe` says so — so
the hosted path is the one that matters, not an edge case.

For a hosted agent the clone differs from its base version by exactly the
variables named and nothing else: `download_code` returns the current bytes
and they go straight back through `create_version_from_code`. That is the same
guarantee the prompt path gives by copying every other field.

### How these agents actually bind, and why that is lucky

`--inspect-code` reported every hosted agent in this project reading
`CONNECTWISE_TOOLBOX_NAME`, `CONNECTWISE_TOOLBOX_VERSION` and
`CONNECTWISE_TOOLBOX_AUTH_SCOPE`, with `ai.azure.com` as the only host in the
code. They resolve a **Foundry toolbox by name** and hold no endpoint.

So the replay creates a toolbox whose one tool points at the replay server,
names it in the clone's environment, runs, and deletes it. **No code change,
and the agent cannot tell the difference.** The bearer token and session go in
the *toolbox's* headers rather than the agent's environment, because Foundry
is what calls the replay server.

A toolbox left behind would answer a real agent with recorded data, so
teardown runs even when the agent version is never created, and a failure to
delete one is reported rather than swallowed.

### An orchestration is refused

The orchestrator reaches its children **by name**. A name resolves to that
child's own default version, whose environment still points at the real
ConnectWise toolbox — so a replay of an orchestration would stub the
orchestrator and send every child's calls, **including writes**, to the live
service, with nothing in the journal to show for it.

`run_replay.py` refuses a multi-agent cassette and names the single-agent ones
that do replay fully stubbed. There is **no override**: the refusal is the only
thing between such a cassette and a live write, and a flag that disables it is
one hurried run away from being set. `--allow-live-children` existed and was
removed.

### Checking what an agent reads

That is a fact about the code, and the code is downloadable:

```
python3 replay/run_replay.py --describe                      # kinds and tools
python3 replay/run_replay.py --describe --inspect-code       # what the code reads
```

`--inspect-code` downloads a hosted agent's zip, reports which environment
variables its Python reads and which hosts it names, and saves nothing. Only
the shape is printed — not the source, and no values.

If the code reads nothing from the environment, the endpoint is fixed in the
code and a replay needs a code change rather than a variable. `--inspect-code`
says that in as many words.

Name the variable once it is known:

```
python3 replay/run_replay.py --cassette ... --server-url ... \
    --replay-env-var CONNECTWISE_MCP_URL
```

Defaults are tried when none is given (`CONNECTWISE_MCP_URL`,
`MCP_SERVER_URL`, `REPLAY_MCP_URL`) and the token and session go alongside as
`REPLAY_MCP_TOKEN` and `REPLAY_MCP_SESSION`. Those are defaults, not
assumptions — setting a variable nothing reads changes nothing, which is why
`--inspect-code` exists.

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

Flex Consumption is unlikely to scale out for one replay, but a gate does
not rest on "unlikely" -- and one replay is not a sequential client: the ops
agent sends up to nine calls at once. The floor for
`maximumInstanceCount` on that plan is **40** — pinning to one instance is not
on offer. So the cursor and journal live in blob storage, guarded by an ETag
(`replay/state_store.py`), keyed by the MCP session id that `initialize`
issues. One session is one replay, which is the lifetime the cursor should
have. Calls that arrive together on one session -- the agent's fan-outs --
are serialised within an instance, and a save lost to another instance is
reloaded and re-applied; only a race lost on every retry returns a 409 saying
the replay is unordered, rather than an answer that looks fine. The journal
survives an instance recycle, which is what makes `/summary` worth reading
afterwards.

The store reaches blob storage as the function app's **managed identity** --
a token from the platform's `IDENTITY_ENDPOINT`, a bearer header on the blob
REST API, no key and nothing that expires -- or, where the identity could
not be given a role, with a container SAS minted at deploy time. Both are
stdlib: the SDKs cannot be imported by a custom handler. If the configured
store cannot be reached the server still starts -- a handler that will not
start is a 502 that says nothing -- but answers every MCP call with
`replay state unavailable: <cause>` and retries the store a few seconds later.
It never answers from in-process state, which another instance cannot see.
`GET /?resolve=1` reports the store or the cause; `verify.py` fails anything
but blob storage, fails an expired SAS and warns thirty days ahead.

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
  "agent": "connectwise-operations-agent",
  "replay_agent": "connectwise-operations-agent-replay",
  "base_version": "82",
  "temp_version": "3",
  "temp_version_deleted": true,
  "cassette": "2026-09-15-73d29f4c3a13.json",
  "tools": "stubbed — no ConnectWise request, no write performed",
  "matched_prefix": 50,
  "suggested_eval_name": "replay-connectwise-operations-agent-v82"
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
| Production behaviour | recorded traces, `run_evals.py` | nightly |
| Judged sample | recorded traces, Foundry evaluators | weekly |

The live staging replay is gone rather than demoted to a smoke test. It was
the slowest, the most expensive, the least repeatable, and the only thing here
that could leave state behind in a system of record — and it never once ran to
completion, because `DEFAULT_AGENT_IDS` was never set.


---

## Gating a deployment on this

`.github/workflows/agent-gate.yml` is a **reusable** workflow, because the
agents merge in a different repository than this one. Called from there,
before the deploy:

```yaml
jobs:
  evals:
    uses: verve-it/automation-solutions-evaluations/.github/workflows/agent-gate.yml@main
    with:
      environment: staging        # or prod
    secrets: inherit

  deploy:
    needs: evals                  # <- this is the gate
```

**`needs:` is the gate.** Without it the evals run, report, and the deploy
goes ahead regardless — a dashboard, not a gate.

What it does, in order: verifies the replay server still serves what was
recorded, replays each **single-agent** cassette against stubbed tools,
exports the window those replays ran in, **attributes each replayed run to the
recording it replays**, scores it against that recording's baseline row,
writes the scores into the job summary, and creates the run in Foundry.

### Why attribution, and what happened without it

A baseline row is keyed by `(orchestration_id, run_agent)`, where
`orchestration_id` is the **recorded** run's App Insights `operation_Id`. A
replay is a new invocation with a new one. The first version of this gate
scored the export as-is and diffed it against a baseline file named in the
workflow, so every replayed row was "new, not in baseline, not compared".
Scored that way, the two worst recorded runs — 0 of 2 passing, 4 of 8 gating
verdicts — left the gate at **exit 0**. It could not fail.

`replay/attribute_runs.py` fixes it without guessing:

| Step | How |
|---|---|
| Find the replay's row | `(run_agent, agent_version) == (replay_agent, temp_version)` from the manifest. Only a replay calls the replay agent, so other traffic in the window — the agent under test included — is never scored. |
| Prove it used the stub | Refuse an A2A call, another agent in the operation, any toolbox but the replay's own, a session the server did not see, another agent than the cassette recorded, and — per tool — more calls than the server journalled, counting every tool the recording did not run locally (bare names included: a write through a locally built client has no prefix). Each of those is a call that reached something real. |
| Present it as the recording | `run_agent`, `traj_key`, `mcp_toolboxes` and tool definitions become the recording's, so an unchanged agent matches its baseline. What was observed stays under the row's `replay` key. The replayed spans for the Foundry dataset are renamed the same way. |
| Re-key it | to `(recorded_orchestration_id, recorded_agent)`, which `run_replay.py` now writes into the manifest from the cassette. |
| Pick the baseline | the committed row for that recording, from **every** file in `baselines/`. None, or two, is a failure. |
| Wait for ingestion | exit 3 while the row is absent or, for some tool, shows fewer calls than the replay server journalled; the workflow re-exports up to ten times, a minute apart. A half-ingested run would score as a regression. |

Then `run_evals.py --strict-baseline` fails if any replayed run is not in the
baseline, any baseline row was not scored, or nothing was compared. Drift must
not use `--strict-baseline`: new orchestrations arrive there every day and are
supposed to be reported, not gated.

What that buys on today's fixtures: of the 8 gating verdicts across the two
ops cassettes, 4 passed in the recording and can therefore regress. The other
4 already fail and can only be fixed. There is no known-good single-agent
cassette yet, so the gate currently guards against making the worst runs
worse; a clean ops-agent recording would let it guard a good one.

### What a builder sees when it is red

The job summary, not an artifact zip:

| Check | Gates? | Pass rate | Failing | N/A |
|---|---|---|---|---|
| `no_wasted_calls` | yes | 71% ⚠ | 2 | 0 |
| `valid_tool_args` | yes | 83% ⚠ | 1 | 1 |
| `no_tool_errors` | no | 57% | 3 | 0 |

…followed by **what regressed against the baseline** — agent, recording,
check, whether it gates, reason — then the runs that failed and which check
each failed on, and a link into **Foundry → Evaluation** for the same runs
scored by the registered evaluators. The failing-runs list includes known
failures the baseline accepts; the regression table is what actually turned
it red, and only a regression on a gating check can. `run_cloud_eval.py` runs even when scoring failed, because a
failed gate is exactly when someone wants to open the run and look at it.

Checks marked *no* report but never fail the build — only `GATING`
(`no_wasted_calls`, `no_dead_ends`, `trajectory`, `valid_tool_args`) decides.
`n/a` means the check did not apply: no schema for that tool, or no threshold
configured.

### Regression, or a floor, or both

| | Gates on | Set with |
|---|---|---|
| Regression | worse than the frozen baseline | `--baseline` (always on) |
| Floor | absolute pass rate | `min-score`, `min-check-score` |

They compose, and neither overrides the other. **Start with regression only**
— the frozen known-good set currently has 2 of 7 runs failing a gating check,
which the baseline records and accepts. An absolute floor of 90% would fail
every build until those are fixed, and a gate that is always red is a gate
people route around. Raise a floor once the known failures are closed, and
set it from where you actually are.

### What it does not cover

Orchestrations. The gate skips any cassette with more than one agent rather
than relying on `run_replay.py` to refuse it — a gate that depends on a
refusal to avoid live writes is one flag away from doing them. Today that
means `connectwise-operations-agent` is gated and the triage orchestration is
not.
