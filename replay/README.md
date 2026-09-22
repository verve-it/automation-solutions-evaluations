# Cassette replay

The agent-change gate. `make_cassette.py` turns a recorded trace into an
ordered cassette, `replay_server.py` serves it as an MCP server, and
`run_replay.py` binds the agent under test to it, invokes, scores and tears
down. `docs/REPLAY.md` is the full account; `functions/replay-mcp/` hosts the
server where Foundry can reach it.

**No ConnectWise request is made, for reads or writes.** A write returns the
response the real write returned and writes nothing.

## What used to be here

`full-triage.json` — a list of dev-instance ticket ids — and a README
describing how to populate it. It was the data file for
`.github/workflows/staging-replay.yml`, which ran `microsoft/ai-agent-evals`
to **invoke** the agents against the dev ConnectWise instance and score the
result.

Both are removed. Nothing in this repo invokes an agent against real tools, in
any environment: an eval that writes to a system of record is not an eval. The
workflow never once ran to completion — `DEFAULT_AGENT_IDS` was never set on
the `staging` environment, so both of its runs failed at the step that
resolves which agent to evaluate.

Two things it had that the cassette replay does not, recorded so they are not
rediscovered as gaps:

- **Confidence intervals and a significance test** against a baseline agent
  version, which tells a flaky agent from a broken one. `docs/ASSERT.md`
  covers getting that without invoking anything.
- **Coverage of the write path end to end.** A stubbed write proves the agent
  asked for the right write, not that ConnectWise would accept it. That is a
  question for the agents repo's own tests against its dev instance, not for
  an eval suite.

If a live check is ever wanted again, it does not belong in this repository.
