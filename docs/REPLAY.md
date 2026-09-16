# Record-and-replay: stubbing the tools with recorded output

Answers the question "are the evals running the tools live?"

## What runs live today

| Component | Invokes agents | Calls ConnectWise | Network |
|---|---|---|---|
| `trace_to_eval.py` | no | no | **none** |
| `run_evals.py` | no | no | **none** |
| `make_cassette.py` | no | no | **none** |
| `export_traces.py` | no | no | Log Analytics read |
| `submit_to_foundry.py` | no | no | judge model — sees recorded text only |
| `replay_server.py` | no | **no** | serves a cassette |
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
recorded trace ──► make_cassette.py ──► cassettes/<date>-<op_id>.json
                                              │
agent under test ──► Foundry toolbox ──► replay_server.py (MCP)
                                              │
                                        artifacts/replay-journal.json
```

The agent sees tools with the **same names and the same schemas** as
production. Every call is answered from the recording. No ConnectWise request
is made and no write is performed — a write returns the response the real
write returned.

```bash
python3 make_cassette.py traces/2026-09-15-ops-worst-case.csv -o cassettes/
python3 replay_server.py cassettes/2026-09-15-73d29f4c3a13.json \
    --tool-defs tool_manifests/ --journal artifacts/replay-journal.json
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
difference gets scored as the agent's fault. `make_cassette.py --strict`
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
mistakes that production would reject go unnoticed. `replay_server.py` warns
loudly when this happens. See `tool_manifests/README.md`.

## What has to be wired outside this repo

The eval-repo half — cassette format, replay server, divergence policy — is
here and tested. The Foundry side is not, and is yours:

1. **Host `replay_server.py`** (or an equivalent) somewhere the Foundry project
   can reach. It is stdlib-only and stateless apart from the cassette.
   `--token` enables a bearer check.
2. **Register a toolbox** pointing at it — e.g. `ConnectwiseMCP-Replay` — with
   the same tool names. The schemas come from `--tool-defs`.
3. **Bind an agent variant to it.** The agent under test then differs from
   production by its toolbox binding only. Keep everything else identical, or
   you are evaluating a different agent.
4. **Drive it**, one cassette per run, and collect `/summary`.

Step 3 is the honest caveat: a replayed agent is not byte-identical to the
production agent, because its toolbox binding differs. Keeping the tool names
and schemas identical is what keeps that difference from mattering — another
reason the manifest is the highest-leverage open item.

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
