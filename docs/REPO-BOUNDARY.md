# Should the agents and the evals share a repo?

**No. Two repos.** This keeps coming up, so here is the decision, what it costs,
and the one thing that would change it.

## Why

**1. A broken eval must not be able to block an agent hotfix.** This is the one
that decides it. If the evals gate the agents and live beside them, the first
time a flaky eval blocks a production fix at 6pm somebody disables the gate —
and it never comes back on. Separation is what keeps the gate credible, because
turning it off is a visible, deliberate act rather than a line in the same PR.

**2. Different blast radius, so different release rules.** An agent change can
write bad data into ConnectWise. An eval change cannot do anything worse than
report the wrong number. They should not share an approval path, a deploy, or a
rollback story. Note the asymmetry in the CI already: `prod` has a required
reviewer and the replay workflow refuses to run outside `automation-solutions-test`.
Those protections are meaningful precisely because eval changes do not carry
them.

**3. Different consumers.** The layer-1 and layer-2 checks — errors, dead ends,
truncation, wasted calls, tool-contract validity — are ~80% of the check code
and apply to any agent on the same tool surface, including agents this team
does not own. A shared repo makes that awkward the first time someone else's
agent wants in.

## Who owns what

| | agents repo | evals repo (this one) |
|---|---|---|
| Agent prompts, contracts | ✅ | |
| Skill files (`normalization`, `classification`, …) | ✅ | |
| Toolbox bindings, MCP server config | ✅ | |
| The agent variant bound to the replay toolbox | ✅ | |
| `expected.json`, `baselines/`, `traces/` | | ✅ |
| Checks, converter, scorer | | ✅ |
| Cassettes, `replay/replay_server.py` | | ✅ |
| `replay/` dataset + the staging replay workflow | | ✅ |
| Production drift + judged sample | | ✅ |

The rule of thumb: **anything that can change what an agent does** belongs in
the agents repo. **Anything that only measures** belongs here.

## What crosses, and how

One versioned artifact, published by the agents repo and consumed here. Not
source coupling, not a submodule, not a shared checkout:

```json
{
  "contract_version": "2026.09.17",
  "agents":   ["triage-orchestrator", "triage-analysis-agent", ...],
  "intents":  ["Full Triage", "Normalization Only", ...],
  "toolbox":  {"name": "ConnectwiseMCP", "tools": [ ...tools/list... ]},
  "skills":   [{"name": "normalization", "sha256": "730f8adc...", "version": "12"}]
}
```

- `agents` feeds `AGENT_NAMES` in `trace_to_eval.py`. Adding an agent without
  updating it silently collapses its runs into the caller's trajectory.
- `intents` is the key space for `expected.json`.
- `toolbox` is `tool_manifests/`, now filled from the `cwpsa-mcp` source.
  The open item moved into that repo: the tools declare no enums, so
  generated argument validation has nothing to check. See
  `tool_manifests/README.md`.
- `skills` is what makes "which rules were in force" answerable for a
  historical run.

Triggering is already wired the other way: the agents repo fires a
`repository_dispatch` of type `agent-change` at this repo after a release. See
`.github/workflows/staging-replay.yml`.

## The honest cost

Skill-hash resolution gets worse. `trace_to_eval.py --skill-registry` records a
sha256 per run so an old run's rules can be read back, but the *source* of
those skills lives in the other repo. In a monorepo you would resolve a hash to
a file with `git log`. Here you need the contract artifact to carry the hashes,
or a local registry, which is why both exist.

That is a real cost. It is smaller than the cost of someone disabling the gate.

## What would change the answer

One thing: **if the evals are never going to gate anything.** If this stays a
reporting tool that nobody blocks a release on, reason 1 evaporates and the
monorepo's convenience wins. That is not where this is heading — the frozen-set
gate is already wired into CI — so the answer is two repos.

Not reasons to merge: "it's annoying to keep them in sync" (that is the
contract artifact's job), or "we're one team" (team shape changes faster than
repo boundaries should).
