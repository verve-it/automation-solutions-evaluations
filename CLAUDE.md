# CLAUDE.md

Read this before answering anything about how this repo evaluates agents.

## THE TOOLS ARE STUBBED. This is the architecture, not an aspiration.

The agent-change gate runs agents against **recorded tool responses**, not
against ConnectWise:

- **No ConnectWise request is made.** Not for reads, not for writes.
- **A write returns the response the real write returned, and writes nothing.**
  The agent sees `{"id": 805545, ...}` and believes it succeeded. Nothing
  happened.
- **The same ticket replays identically** however the live system has moved on.
  That is the point: determinism is what makes it a gate.

Mechanism: `replay/make_cassette.py` turns a recorded trace into an ordered
cassette; `replay/replay_server.py` serves it as an MCP server;
`replay/run_replay.py` binds the agent under test to it, invokes, scores and
tears down.

Verified end to end: 50/50 recorded calls matched, 4 writes replayed as
recorded successes, zero ConnectWise requests.

### Nothing here ever invokes an agent against real tools

Not in staging, not against the dev instance, not once a week, not behind a
flag. An eval that writes to a system of record is not an eval.

This used to be qualified. `.github/workflows/staging-replay.yml` ran
`microsoft/ai-agent-evals` against the dev ConnectWise instance and was
described here as "the weekly smoke test for the write path", and
`run_replay.py` took `--allow-live-children` to replay an orchestration with
its children pointed at the live service. Both are **removed**. The workflow
never once passed — `DEFAULT_AGENT_IDS` was never set, so both of its runs
failed before invoking anything — and the flag was one hurried run away from
being the thing that made the whole apparatus pointless.

| Gate | Mechanism | Tools |
|---|---|---|
| Eval-code change | frozen sets vs frozen baselines | none |
| **Agent change** | **cassette replay** | **STUBBED** |
| Production behaviour | recorded traces, `run_evals.py` | none |
| Judged sample | recorded traces, Foundry evaluators | none |

Every row is `none` or `STUBBED`. If a row ever reads otherwise, that is the
bug.

Reachability is solved by `functions/replay-mcp/`: Foundry calls the replay
server, so it must be reachable from Azure. `localhost` cannot work and
`run_replay.py` refuses it. `infra/main.bicep` provisions the host; `deploy.sh`
provisions and publishes.

### Not the Functions MCP extension, and the reason is a schema

The extension is GA and is right for building a *new* tool server. Its
`toolProperties` is a flat `{propertyName, propertyType, description,
isRequired, isArray}` with **nowhere to put an `enum`**. Eight manifest
properties are enums, including `cw_resolve.reference_type` — the twenty-value
enum that is the only reason `valid_tool_args` can fail at all. Advertising it
as a bare string would invite the agent to send values production rejects, so
the divergence would be ours. A stub that changes the tool contract is not a
stub.

Hosting our own MCP server on Functions is itself documented and native ("Host
servers built with MCP SDKs on Azure Functions"); `host.json` carries the
`mcp-custom-handler` profile for it.

Microsoft's sample sets `AzureWebJobsFeatureFlags=EnableMcpCustomHandlerPreview`
in `local.settings.json`, and the flag's name says the profile is preview.
`infra/main.bicep` sets it too. **It was not, however, the cause of anything**:
host `4.1054.250.26428` honoured the profile without it, logging `1 functions
found (Custom)` and `Using port 8000 specified via configuration for custom
handler`. I asserted otherwise once, from the sample alone, before reading a
log. Don't repeat that.

So: the MCP **extension** is GA and this profile is preview-flagged. Still the
right trade -- the extension cannot express an enum -- but a trade.

## Native vs ours — settled, with evidence

Do not re-open these without reading `docs/NATIVE-RESEARCH.md` and
`docs/MIGRATION-READINESS.md`. Each was researched, several were wrong the
first time, and the corrections are recorded there.

| Concern | Where it runs |
|---|---|
| Evaluator definitions, datasets, eval runs, **results** | **Foundry.** Results are project-scoped eval runs, not in this repo. |
| Continuous evaluation | **Foundry.** `foundry/continuous_eval.py`. Custom evaluators qualify. |
| Scoring, offline | **Native SDK.** `azure-ai-evaluation.evaluate()` runs with no project and no credentials — `foundry_evaluators/native.py`, 62 verdicts 0 mismatches against `run_evals.py`. |
| Baseline diff, gating exit code | **Ours.** Foundry's baseline comparison is a t-test, which is the wrong instrument for a deterministic check, and a server-side baseline changes without a reviewer. |
| Dataset construction | **Ours.** `AIAgentConverter` is a *classic* threads-and-runs API retiring 2027-03-31; our traces are `conv_` with no `thread_`/`run_`. It also returns one blob per conversation and every child agent shares the orchestrator's, so per-agent decomposition would be lost. Unhandled tool types are **silently skipped**. |
| Cassette replay | **Ours.** No native tool stubbing exists — checked ACS, APIM `mock-response`, APIM caching, Agent Framework mocks. Hosted on **Azure Functions, Flex Consumption, as a custom handler** — `functions/replay-mcp/`. |

Our traces already carry `gen_ai.tool.name`, `mcp.method.name` and
`mcp.protocol.version` — the OTel conventions' own attributes. The conventions
are at **Development** stability in a repo that exists to iterate fast, with
no version to pin. That, not a native converter arriving, is the thing likely
to break first.

## Project facts live in eval-config.json, not in scripts

The point of this repo is evaluating **any** agent, so nothing about
ConnectWise or triage is a constant in a script. `evalconfig.py` reads
`eval-config.json`; `evalconfig.DEFAULTS` is deliberately empty, because a
default that happens to fit this project is how the constants got embedded the
first time.

What can be derived is derived rather than configured:

- **Agents come from the trace.** Every distinct `gen_ai.agent.name` in an
  export is an agent. `learn_agents(spans)` returns the configured set plus
  those, and it **returns** — it does not write `AGENT_NAMES`, because a
  global that conversion rewrites makes every later read depend on which trace
  was converted first, and `scrub_trace.protected_vocabulary()` reads it.
  Pass the result to `tool_step(span, agents)`.
- **Write tools come from the server.** MCP defines
  `annotations.readOnlyHint` and `destructiveHint`; where a manifest has them
  they decide, ahead of the config. `extract_tool_manifest.py` keeps them now
  — it used to drop them.

The old hard-coded `WRITE_TOOLS` was wrong in both directions: it named
`cw_patch`, which does not exist, and missed eight tools that plainly mutate
(`cw_log_time`, `cw_set_approval`, `cw_convert`, …). The committed cassettes
happened not to call any of them, so the counts reported were right by luck.
A missed write tool is counted as a read, and the gate then reports "0 writes,
none performed" about a run that attempted several.

## Other things worth not re-deriving

- **Root holds four scripts.** `export_traces` → `trace_to_eval` → `run_evals`,
  plus `scrub_trace`. Everything else is in `foundry/`, `replay/`, `tools/`,
  `dataverse/`. Moved scripts put the repo root on `sys.path` themselves.
- **`foundry/` vs `foundry_evaluators/`**: the first is our tooling that calls
  Foundry; the second is code uploaded and executed *by* Foundry.
- **Traces must be scrubbed before committing.** `tests/test_committed_traces.py`
  fails if a tracked trace lacks pseudonym tokens or carries a real address.
  One repo salt, for ever — a different salt gives the same person a different
  token and silently breaks cross-trace reading.
- **`valid_tool_args` is only as real as the schema.** It passed 100% of a
  known-bad trace set until `cwpsa-mcp` typed `reference_type` as a `Literal`.
  A check that cannot fail is not a check.
- **Divergence in a replay is not automatically failure.** An agent change that
  removes a wasted call *should* diverge.
- **Gates compare DELTA, not absolute pass rates.** Known failures stay failing
  without blocking unrelated work.
- **A replay's eval belongs to the BASE version, not the ephemeral clone.**
  `run_replay.py` creates a temp version of the *replay agent* and deletes it; the trace keeps
  its id, so `artifacts/replay-run.json` records what it was cloned from.
- **`evaluate()` reads `**kwargs` as a required column named `kw`.** Evaluators
  need explicit keyword parameters.

## The hosted replay package is FLAT

`functions/replay-mcp/.build/` contains no directories. The deployment keeps
files at the root of `wwwroot` and drops subdirectories — `lib/` first, then
`tool_manifests/` once `lib/` was flattened, each as a 502 with a stack trace
behind it. Cassettes and manifests live in one `replay_payload.json` beside
`server.py`, and `build.py` refuses to finish if a directory appears.

A checkout still uses `cassettes/` and `tool_manifests/`. `Source` in
`server.py` reads whichever is there.

## No SDK in the hosted replay server, and no build step

`azure-storage-blob` is not importable in a custom handler: Oryx installs it
into `.python_packages/lib/site-packages`, which the Functions *Python worker*
adds to `sys.path`, and a custom handler is `python server.py` with none of
that setup. The app logged `ModuleNotFoundError: No module named 'azure'` with
the package plainly deployed, and that is why a 50-call replay journalled 3 —
the store degraded to in-process and each instance kept its own cursor.

Replay state goes over the blob REST API, stdlib only, two ways:

- **`storageAuth=identity` (the default):** a token from the platform's
  managed-identity endpoint (`IDENTITY_ENDPOINT` / `IDENTITY_HEADER`, api
  2019-08-01, `client_id` = the user-assigned identity) as a bearer header.
  Nothing to leak, nothing to expire. This mode used to reach an SDK-based
  store that could not import, and fell back to in-process state *silently* —
  the default deployment was the broken one. That store is deleted.
- **`storageAuth=connectionString`:** a container SAS minted by
  `infra/main.bicep`. It expires (`stateSasExpiry`, a year); `/` reports when,
  and `verify.py` warns 30 days out and fails once it has.

**`verify.py` fails any backend but those two.** It used to print a NOTE on
in-process state and pass. It reads `GET /?resolve=1`, so the answer is about
a store the instance has actually reached, and the gate runs verify first.

**A configured store is durable or an error, never in-process.** A store that
cannot be reached is not swapped for a MemoryStore: the server still starts
(a handler that will not start is a 502 that says nothing), answers every MCP
call with `replay state unavailable: <cause>`, and tries the store again a few
seconds later. The fallback it replaced was kept for the instance's life, so
one failed token request on one instance of a scaled-out app gave it its own
cursor and the agent answers from the wrong place in the recording.

A role assigned directly to the identity takes up to ~10 minutes (the ~24
hours in Microsoft's managed-identity docs is for group membership, which
this does not use). `requirements.txt` is empty, the deployment asks for no
remote build, and the whole server is stdlib. `build.py` ships `evalconfig.py`:
without it the package could not import, and
`test_the_built_package_starts_on_its_own` now starts it in isolation.

## Binding is per agent kind, and every triage agent is HOSTED

| Kind | Tools live | Rebind by |
|---|---|---|
| `prompt`, `voice` | `tools` in the definition | swapping `tools` |
| `hosted` | inside the uploaded code | an environment variable + re-upload of the same code |
| `workflow`, `external` | neither | refused by name |

`--describe` against the project: 3 prompt agents (Classification, Intake,
NotesAndCompany — all bound to a *confluence* MCP server) and 5 hosted
(VerveOS, connectwise-operations-agent, triage-analysis-agent,
triage-evaluation-agent, triage-orchestrator), every one `python main.py`
with a single variable, `AZURE_AI_MODEL_DEPLOYMENT_NAME`.

`--inspect-code` settled how they bind: every hosted agent reads
`CONNECTWISE_TOOLBOX_NAME`, `CONNECTWISE_TOOLBOX_VERSION` and
`CONNECTWISE_TOOLBOX_AUTH_SCOPE`, and the only host in their code is
`ai.azure.com`. **They resolve a Foundry toolbox by name and hold no endpoint
at all** — so a replay creates a temporary toolbox pointing at the replay
server, names it in the clone's environment, and deletes it afterwards. No
code change. The bearer token and session ride on the *toolbox's* headers,
because Foundry is what calls the replay server, not the agent.

## The clone is never a version of the agent under test

It is a version of **`<agent>-replay`**, a separate agent that nothing but
`run_replay.py` calls. It is **made on demand and deleted after the run**: a
first version creates it, so any agent replays with no per-agent setup, and
nothing named `-replay` lingers between runs for anything to call. A new version of `connectwise-operations-agent` itself
would be what that name resolves to while it existed — the SDK says only
*draft* versions are excluded from "latest", and `create_version_from_code`,
the only way to make a hosted version, takes no draft flag — so production
calls reaching the agent by name would be answered from the recording and
their writes silently not performed. Pinning production with a version
selector does not fix it either: then the replay's own by-name call reaches
the production version and its live toolbox. Under its own name, "latest" is
the clone; `routing_problem()` checks that before anything is invoked, and the
gate serialises its runs (`concurrency:`) because every replay shares the name.

The replay tool is bound under the **recording's** `server_label`
(`ConnectWise-PSA-ForAgents`), read from the cassette. Foundry names MCP tools
`<server_label>___<tool>`, so any other label is a renamed tool, a changed
trajectory and a changed contract — it once made an unchanged agent fail
`trajectory`. `replay_tool_label` in `eval-config.json` is only a fallback.

## An orchestration cannot be fully stubbed, and is refused

The orchestrator reaches its children over A2A **by name**
(`TRIAGE_ANALYSIS_AGENT_NAME` and friends). A name resolves to that agent's
own default version, whose environment still points at the real ConnectWise
toolbox — so replaying an orchestration stubs the orchestrator and sends every
child's calls, **writes included**, to the live service. The journal would
never show it.

`run_replay.py` refuses a multi-agent cassette, with **no override** — the
refusal is the only thing between such a cassette and a live write. The
single-agent cassettes -- two `connectwise-operations-agent`, five
`triage-analysis-agent` -- replay fully stubbed today. Fixing it for
orchestrations needs the children addressable by version,
which is the agent code's decision, not this script's.

## Gating a deployment

`.github/workflows/agent-gate.yml` is a **reusable** workflow (`workflow_call`)
because the agents merge in another repository. It verifies the replay server,
replays every single-agent cassette against stubbed tools, scores against the
baseline, writes the table into `$GITHUB_STEP_SUMMARY`, and creates the run in
Foundry → Evaluation. `needs:` in the calling workflow is what makes it a gate
rather than a dashboard.

Four things the gate must not do, all of which look like they work:

- **Compare by operation_Id.** A baseline is keyed by the *recorded* run's
  `operation_Id`; a replay is a new invocation with a new one. Scored as
  exported, every replayed row is "not in baseline, not compared" and the gate
  passes whatever the agent did — it passed the two worst recorded runs (0 of
  2 runs, 4 of 8 gating verdicts), exit 0. `replay/attribute_runs.py` picks
  each replay's row out by `(replay_agent, temp_version)`, **proves it ran
  against the stub and only the stub** — no A2A, no other agent in its
  operation, no toolbox but the replay's own, the agent the cassette recorded,
  and per tool no more calls in the trace than the replay server journalled —
  "local" tools taken from the recording, never inferred from a missing
  prefix, because a write through a client the agent built itself has none —
  then presents it under the
  recording's agent, `traj_key`, toolbox and tool definitions and pulls that
  recording's committed baseline row. `run_evals.py --strict-baseline` then
  fails if any replayed run went uncompared. Nightly drift must **not** use
  strict: new orchestrations arrive every day.
- **Fail on a reporting check.** Only a regression on a `GATING` check fails
  the build; the others are shown and marked as not failing it. A replay's
  `not_recorded` divergence is a tool error, so `no_tool_errors` flips on
  exactly the agent changes the gate should let through.
- **Export by the clock.** The project also serves real traffic, so a flat
  `--hours 1` scores whatever else ran in that hour as part of this agent
  change's verdict. `run_replay.py` records `started_utc`/`finished_utc` in
  its manifest and the gate exports that window; with no manifest it refuses
  rather than widening.
- **Publish the export.** A workflow artifact in a **public** repository is
  downloadable by anyone, and `scrub_trace.py` is propose → human review →
  apply, so it cannot be dropped into CI. Neither `agent-gate.yml` nor the
  nightly drift job uploads raw spans or the Foundry dataset. Verdicts,
  journals (which replay the already-scrubbed cassettes) and the Foundry run
  are the record. Raw exports live under `out/`, which is never uploaded;
  `artifacts/replay-*.json` once matched the raw spans file, so the test
  matches every upload glob against every path an export step writes.

Attribution retries while App Insights catches up (exit 3 = not ingested, or
only partly: for some tool, fewer calls in the trace than the replay server
journalled). A half-ingested run scores as a regression, so it is never scored
early. The comparison is per tool because local tools (`load_skill`,
`tool_search`) are in the trace and never reach the server.

What is refused **before** anything is created, because a check in the trace
comes after the writes: another agent than the cassette recorded (an
orchestrator on a single-agent cassette reaches its children by name, live),
and a hosted agent whose environment names another agent in the project.
The gate has no `agent:` input for the same reason.

## The stub answers calls that arrive together

The ops agent fans out: its recordings have nine MCP calls in flight at once.
The hosted server used to load a session's state, answer, and save it
conditionally on the ETag, so every call in a burst but one lost the race, got
a 409 and was never journalled — the agent saw errors where the recording had
results. It now serialises a session's calls within an instance and, when
another instance wins, reloads and re-applies the call. `verify.py` sends a
concurrent fan-out on every deployment and fails a server that drops one --
that exercises the in-instance lock; the cross-instance retry is covered by a
test in which another instance really consumes the queue head mid-call.

Local tools are the union of `eval-config.json`'s `local_tools` and every
name the agent's recordings called unprefixed. One recording is not enough:
`73d29f4c3a13` never called `tool_search`, and an unchanged agent that did
would have been refused as a bypass. A bare name in neither still counts as a
call that had to reach the stub.

`run_evals.py` gates on **regression vs baseline** by default and takes
`--min-score` / `--min-check-score` for an absolute floor. They compose. Only
`GATING` checks are held to the floor — a reporting check failing must not
block a deploy. A check result is `{"passed": True|False|None}`; `None` means
the check did not apply and is excluded from its denominator, never counted as
either verdict.

## Working agreements

- **Patches, not pushes.** Deliver a `git format-patch` file. Do not push.
- **One patch per message**, newest commit only, unless a range is asked for.
- **Verify before claiming.** Run it, then say what happened.
- **No fix for a failure whose log you have not read.** A 502, an "unhealthy"
  health check and a crashed handler are the same symptom; the log is the only
  thing that separates them. Shipping a plausible cause from a sample file
  cost a deploy cycle here and put a wrong explanation into five documents.
  When the log is out of reach, say so and ask for it -- that is one message,
  where a guess is a round trip.
- **Half this repo's users are on PowerShell.** `openssl`, `export`, `make`
  and `python3` have each blocked a run by being named in advice that could
  not be followed. `tasks.ps1` mirrors the Makefile; messages that print a
  command branch on `os.name`.
- **Make the failure carry its own evidence.** Cheaper than another round
  trip: print the interpreter, the path, and the directory listing, so the
  next occurrence names itself.
- **Never `git reset --hard origin/<branch>`** without checking for local
  commits first. Doing that has discarded delivered work twice in this repo.
- **`origin` is not their working tree.** A patch absent from `origin/develop`
  may still be applied locally. Ask; do not infer.
