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

### Do not describe the live staging replay as the agent-change gate

`.github/workflows/staging-replay.yml` invokes **real** tools against the dev
ConnectWise instance. It is the **weekly smoke test for the write path** —
slow, costs judge inference, leaves state behind, different answer every run.
It is not how you decide whether an agent change is safe.

| Gate | Mechanism | Tools |
|---|---|---|
| Eval-code change | frozen sets vs frozen baselines | none |
| **Agent change** | **cassette replay** | **STUBBED** |
| Agent change, judged | `ai-agent-evals` in staging | live, dev instance |
| Production behaviour | recorded traces, `run_evals.py` | none |
| Write path still works | one live staging run | live, dev instance |

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
  `run_replay.py` creates a temp agent version and deletes it; the trace keeps
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

Replay state now goes over the blob REST API with a container SAS minted by
`infra/main.bicep`. `requirements.txt` is empty, the deployment asks for no
remote build, and the whole server is stdlib.

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
- **Make the failure carry its own evidence.** Cheaper than another round
  trip: print the interpreter, the path, and the directory listing, so the
  next occurrence names itself.
- **Never `git reset --hard origin/<branch>`** without checking for local
  commits first. Doing that has discarded delivered work twice in this repo.
- **`origin` is not their working tree.** A patch absent from `origin/develop`
  may still be applied locally. Ask; do not infer.
