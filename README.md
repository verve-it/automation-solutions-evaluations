# Triage Automation Evals

Deterministic evaluation of the ConnectWise triage agents, scored from
recorded traces, locally and in Microsoft Foundry.

Four Foundry agents propose and execute changes to support tickets. Before
pushing more autonomy to production we need to know whether they are behaving
well, and to tell when a change makes them worse. The evaluation that existed
before this was an LLM reading the workflow output with no reference and
declaring it good. This replaces that with checks that read what the tools
actually returned.

---

## The constraint everything follows from

The agents mutate the ticket they operate on, and `connectwise-operations-agent`
writes to a system of record. Re-running a triage produces different results
because the first run already changed the data.

**Never point an evaluator at a live agent against production ConnectWise.**

That rules out re-running agents against production. It does not rule out
replaying them against *recorded tool output* — see
[Cassette replay](#cassette-replay-built-not-wired). Exactly one thing in this
repo invokes agents at all: `staging-replay.yml`, pinned to the test project
and the dev instance.

---

## Quickstart

```powershell
python -m pytest tests/ -q                          # 228 tests, offline
python trace_to_eval.py traces/2026-09-03-full-triage.json -o out
python run_evals.py out/eval_runs.jsonl --expected expected.json
```

Developed on **Windows**: use `python`, not `python3`. PowerShell's line
continuation is a backtick, so commands here are written on one line. `make`
targets assume a POSIX shell — use WSL or run the commands directly.

The converter and the scorer are stdlib-only on purpose: scoring a frozen
trace set must need nothing installed. Only the Azure-facing scripts need
`pip install -r requirements.txt`.

---

## What each piece does

### Local pipeline

| Script | Does |
|---|---|
| `export_traces.py` | Log Analytics → raw spans. Joins `AppGenAIContent` for the payloads (see [Telemetry](#telemetry)). |
| `trace_to_eval.py` | spans → one JSONL row per **AI Run** (one agent's execution) |
| `run_evals.py` | scores the eight checks, diffs against a frozen baseline, sets the exit code |
| `scrub_trace.py` | redacts customer data before a trace is committed |

### Foundry

| Script | Does |
|---|---|
| `foundry/to_foundry_dataset.py` | trace → Foundry evaluation dataset |
| `foundry/register_evaluators.py` | publishes the checks to the evaluator catalog, writes the version lock |
| `foundry/run_cloud_eval.py` | uploads the dataset, creates the eval and run, pins versions |
| `foundry/check_cloud_eval.py` | waits for a run, fetches scores, diffs them against local |
| `foundry/submit_to_foundry.py` | the judged evaluators (Task Adherence, Intent Resolution) over a sample |
| `tools/diagnose_schema.py` | probes what the datasource validator accepts |

### Tool manifests

| Script | Does |
|---|---|
| `tools/extract_tool_manifest.py` | a manifest, from four sources: `--from-url` (the live MCP server — the deployed contract, and the one to prefer), `--from-tools-list` (a saved dump), `--from-source` (a checkout of the server), `--from-trace` (a skeleton, `parameters: null`) |

### Cassette replay (built, not wired)

| Script | Does |
|---|---|
| `replay/make_cassette.py` | a recorded trace → a replay cassette |
| `replay/replay_server.py` | an MCP server answering from a cassette. No ConnectWise request, no writes. |

---

## The checks

| Check | Gating | Catches |
|---|---|---|
| `no_wasted_calls` | **yes** | calls that could not have succeeded: `missing_script`, `invalid_reference_type`, `invalid_entity`, `invalid_projection_field`, `empty_failed` |
| `valid_tool_args` | **yes** | arguments violating the tool's own JSON schema — required params, types, enums, unexpected fields. Generated from `tool_manifests/`; skips without one. Catches a bad `cw_resolve.reference_type` from the contract; `entity` and `filter` are still free-form upstream, so not those. See `tool_manifests/README.md`. |
| `no_dead_ends` | **yes** | succeeded but returned nothing — the hallucinated-entity signal |
| `trajectory` | **yes** | in-order match vs ground truth, extras allowed |
| `no_tool_errors` | info | any error. Too broad to gate. |
| `no_search_cascade` | info | four or more consecutive fruitless calls to one tool |
| `no_truncation` | info | results cut at exactly 8192 chars |
| `cost_latency` | info | tokens and wall clock. Tracked always, gated only with `--max-tokens` / `--max-duration-ms`. |

Adding one is a function returning `_pass()`, `_fail(reason)` or
`_skip(reason)`, registered in `CHECKS` in `run_evals.py`, plus a `grade_*` in
`foundry_evaluators/checks.py` if it should also run in Foundry.

Keep check *logic* as code and check *expectations* as data. Do not build a
generic "evaluate any agent" framework.

### Exit codes

- **Without `--baseline`** — any gating failure exits 1.
- **With `--baseline`** — only a *regression* exits 1, plus **lost coverage**:
  a check that produced a verdict in the baseline and now skips. That case
  looks like silence rather than failure, and it is how an earlier
  intent-keying break went unnoticed.

---

## Running it

### Automatic

| When | What | Touches Foundry |
|---|---|---|
| every push / PR | `frozen-sets` — committed traces vs committed baselines | no |
| nightly 06:00 UTC | `drift` — export → convert → score → **score in Foundry** | yes |
| Monday 07:00 UTC | the above plus the judged sample | yes |
| push to `staging` touching `replay/**` | `staging-replay` — **invokes agents** in the test project | yes |

GitHub Actions does the scheduling; Foundry does the scoring and keeps the
history.

### By hand

```powershell
# register once, and after any change to foundry_evaluators/
python foundry/register_evaluators.py --project-endpoint $env:AZURE_AI_PROJECT_ENDPOINT --model-deployment $env:AZURE_JUDGE_DEPLOYMENT

# build the dataset and score it
python foundry/to_foundry_dataset.py traces/2026-09-03-full-triage.json --expected expected.json --tool-defs tool_manifests/ --no-messages -o artifacts/foundry-dataset.jsonl
python foundry/run_cloud_eval.py artifacts/foundry-dataset.jsonl --name full-triage --dataset-version 2026-09-17 --wait --trace traces/2026-09-03-full-triage.json
```

### Adding a trace to the frozen set

```powershell
python export_traces.py --workspace $LAW_ID --operation-ids <ids> --since <date> -o traces/raw.json
python scrub_trace.py traces/raw.json --learn redact.json
#   REVIEW redact.json by hand — delete ConnectWise vocabulary, add identities
$env:SCRUB_SALT = "<a secret you do not commit>"
python scrub_trace.py traces/raw.json --redact-file redact.json -o traces/<date>-<name>.json --verify
python trace_to_eval.py traces/<date>-<name>.json -o out
python run_evals.py out/eval_runs.jsonl --expected expected.json --json baselines/<name>-<date>.json
```

**Never commit a raw export.** See [Scrubbing](#scrubbing).

### Promoting a baseline

When a change legitimately improves things, the new results become the
baseline. Commit it **in the same commit as the change that caused it**, along
with `evaluator-versions.json`, so the history explains itself.

---

## Where everything is stored

| What | Where | Retention |
|---|---|---|
| Traces, scrubbed | git — `traces/` | forever |
| Ground truth | git — `expected.json` | forever |
| Frozen baselines | git — `baselines/` | forever |
| Evaluator source | git — `foundry_evaluators/` | forever |
| Evaluator version lock | git — `evaluator-versions.json` | forever |
| Registered evaluators | **Foundry** — evaluator catalog | versioned |
| Evaluation datasets | **Foundry** — `triage-eval-runs` | versioned |
| Runs and scores | **Foundry** — portal history | project retention |
| Raw telemetry | App Insights + `AppGenAIContent` | 90 days |
| CI artifacts | GitHub Actions | 90 days |
| Redaction lists, the salt, cassettes, `out/`, `skills/` | **nowhere** — gitignored, local only | — |

The redaction list is a catalogue of exactly the customer data you removed.
Keep it and the salt outside the repo.

---

## Telemetry

**From 2026-09-30 the seven `gen_ai.*` content attributes stop being written
into the span tables** — only a pointer remains, and the values live in
`AppGenAIContent`. A span-only export after that date looks well-formed and
contains no content.

`export_traces.py` joins that table by default. It needs **Privileged
Monitoring Data Reader** on top of Log Analytics Reader. Reading from
`AppGenAIContent` also avoids the 8192-character property cap, which is what
truncated `cw_query` results in the original traces.

Full detail in [`docs/TELEMETRY.md`](docs/TELEMETRY.md).

---

## Scrubbing

`protectGenAISensitiveData` restricts tool content to Privileged Monitoring
Data Reader. Committing a raw export replaces that with "has repo access",
permanently, in git history. The September traces carry **67 real e-mail
addresses**, contact and company names, a site address and phone numbers.

Two steps on purpose — an automatic sweep fails both ways, silently:

- **under-redaction:** key-based redaction left 663 of 675 occurrences of one
  contact name. Almost none of it is in a structured field.
- **over-redaction:** an auto-sweep learned `Priority 4` and `AI Triage
  Complete` as entities. Sweeping a status rewrites the trajectory and the
  frozen set stops matching.

`--verify` scores the trace before and after and fails if any check verdict
differs. The scrubber refuses a list containing the intent enum, the agent
names, or ConnectWise status vocabulary.

---

## Foundry

The checks are registered as versioned code-based evaluators, so they sit in
the catalog beside Microsoft's, apply to any agent on the same tool surface,
and can run in continuous evaluation.

**The dataset is built here rather than read from `azure_ai_traces`**, because
that path reads only `invoke_agent` spans, and those carry `tool_call` but no
`tool_result`. Four of the eight checks read results.

**What the port costs:** a code-based evaluator returns one float, 0.0–1.0, in
a sandbox with no network. `run_evals.py` returns a verdict *and* a reason —
"4 avoidable call(s): empty_failedx2, missing_scriptx1" — and a score cannot
say why. So `run_evals.py` stays as the local gate that explains itself and as
the baseline-diff regression gate, which Foundry has no equivalent for.

Fidelity is tested: both frozen trace sets are scored with `run_evals.py` and
with the ported functions and every comparable verdict must match — **56
verdicts, 0 mismatches**.

Full detail, including the eight payload rejections it took to get a run
through, in [`docs/FOUNDRY.md`](docs/FOUNDRY.md).

---

## Gotchas

- **The export must keep `customDimensions` intact.** A flattening projection
  strips every `gen_ai.*` attribute.
- **Adding an agent means updating `AGENT_NAMES`** in `trace_to_eval.py`, or
  its runs silently collapse into the caller's trajectory.
- **Every MCP call emits two spans** — `execute_tool <x>` carries the payload,
  `tools/call <x>` is empty, and both set
  `gen_ai.operation.name = execute_tool`. Filter on the span *name*.
- **Grouping is by `gen_ai.agent.name`, not the span tree.**
- **`invoke_agent` spans carry a roll-up of token usage.** Counting them and
  the chat spans doubles every figure.
- **`load_skill` returns no version** — only `name` and `description`
  frontmatter. Content hash is the only identity a historical run has.
- **A nested object cannot hold a non-string value** in a Foundry evaluation
  dataset, whatever the schema declares. Arrays are fine.
- **`ToolCallAccuracyEvaluator` returns _pass_ for tool types it cannot read.**
  Never let it be the gate.
- **The toolbox version in a span URL is a binding revision**, not a schema
  version.

`tests/` locks in every one of these. If you are about to simplify one away,
reproduce the case in a trace first.

---

## Repository layout

Root holds the everyday pipeline and nothing else. Everything a normal run
touches is four scripts.

```
export_traces.py          pull spans out of App Insights
trace_to_eval.py          spans -> one row per AI Run
run_evals.py              score the rows, diff against a baseline
scrub_trace.py            redact customer data before committing a trace

foundry/                  our tooling that TALKS TO Foundry
  to_foundry_dataset.py     rows -> a Foundry evaluation dataset
  continuous_eval.py        have Foundry score live runs, natively
  register_evaluators.py    upload the checks, write the version lock
  run_cloud_eval.py         start a cloud run against registered evaluators
  check_cloud_eval.py       poll it, diff the scores against local
  submit_to_foundry.py      the judged evaluators (sampled, not a gate)

foundry_evaluators/       the eight checks, one implementation
  checks.py  _shared.py     as uploaded to Foundry
  native.py                 the same objects, run by azure-ai-evaluation
                            locally and offline

replay/                   record/replay stub -- THE agent-change gate
  make_cassette.py          a trace -> an ordered cassette
  replay_server.py          serve a cassette as an MCP toolbox
  run_replay.py             bind an agent to it, invoke, score, tear down
  full-triage.json          dev tickets for the live smoke test

dataverse/                outcome evaluation -- human review as ground truth
  fetch_outcomes.py         --probe to discover the schema, then pull reviews
  schema.json               entity/attribute names (UNPROBED -- see its README)

tools/                    occasional, not part of a run
  extract_tool_manifest.py  a manifest from a URL, dump, source tree or trace
  diagnose_schema.py        probe what the Foundry datasource validator accepts

expected.json             ground truth, keyed "<agent>|<intent>"
evaluator-versions.json   the registered versions a run pins
baselines/                frozen results — COMMIT THESE
traces/                   raw exports, dated, scrubbed, committed
tool_manifests/           MCP tool schemas — all 20 ConnectWise tools
Makefile / tasks.ps1      the same shortcuts, for bash and PowerShell
tests/                    unit tests + frozen-set replay
docs/                     HANDOFF, FOUNDRY, TELEMETRY, REPLAY, REPO-BOUNDARY,
                          MCP-SERVER-FINDINGS, HISTORY-PURGE, CREDENTIALS,
                          ASSERT, NATIVE-RESEARCH, MIGRATION-READINESS
```

`foundry/` and `foundry_evaluators/` are deliberately separate, and the
distinction is worth keeping straight: one is code that runs **here** and
calls Foundry, the other is code that gets **uploaded and executed by**
Foundry. `register_evaluators.py` inlines `foundry_evaluators/_shared.py`
into each evaluator it uploads, which is why that file has no imports of its
own.

Scripts outside the root put the repo root on `sys.path` themselves, so
`python3 foundry/run_cloud_eval.py` works from anywhere with no package
install.

The Foundry agents live in a **separate repo**. Anything that can change what
an agent does belongs there; anything that only measures belongs here. See
[`docs/REPO-BOUNDARY.md`](docs/REPO-BOUNDARY.md).

---

## What is not done

| Gap | Blocks | Where |
|---|---|---|
| **`entity` and `filter` are still free-form** | `valid_tool_args` covers arity, types, required params, unexpected fields and `reference_type` — not entity paths or filter shapes. An `entity` enum was proposed and correctly rejected upstream (~36k tokens/request). | `tool_manifests/README.md` |
| **The outcome join is unestablished** | all outcome evaluation. `dataverse/fetch_outcomes.py --probe` answers whether the orchestration record carries the App Insights `operation_Id`. Ticket id will not substitute — two orchestrations in the frozen set share ticket 805392. | `dataverse/README.md` |
| **Continuous evaluation rule not created** | `foundry/continuous_eval.py` builds it; it needs an eval id and one run against the project. Until then the live path is only the nightly cron. | `docs/FOUNDRY.md` |
| Hosting the replay server | the deterministic agent-change gate. `run_replay.py` does the Foundry wiring; the server still has to be reachable from Azure. | `docs/REPLAY.md` |
| `replay/` ticket ids | the staging replay | `replay/README.md` |
| Cost / latency budgets | gating on spend | set `--max-tokens` |
| Skill versions | "which rules were in force" across versions | `load_skill` returns no version |
| Intent on the ops hand-off | intent-keyed expectations for the ops agent | the orchestrator sends a JSON write plan with no `intent=` |
