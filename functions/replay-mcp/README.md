# Hosting the stub

The agent-change gate replays an agent against **recorded tool responses**. No
ConnectWise request is made, for reads or writes; a write returns what the real
write returned and writes nothing. That is the architecture, not an aspiration
— see the repo `CLAUDE.md`.

The one thing a checkout cannot provide is **reachability**. Foundry calls the
replay server; the replay server never calls Foundry. So `localhost` can never
work, and `run_replay.py` refuses it rather than letting you find out as a
timeout inside an agent run. This folder is the answer: the same server, on an
Azure Function, at a URL Foundry can reach.

```
export REPLAY_TOKEN=$(openssl rand -hex 32)
./deploy.sh my-resource-group eastus2
```

That provisions everything in `infra/main.bicep` and publishes the package.
It prints the `run_replay.py` command to use next.

---

## Why a custom handler and not the Functions MCP extension

The MCP extension is GA and is the right way to build a *new* tool server. It
is the wrong way to build a *stub of an existing one*.

The extension describes a tool with `toolProperties`, a flat list of

```json
{"propertyName": "...", "propertyType": "string|number|integer|boolean|object",
 "description": "...", "isRequired": false, "isArray": false}
```

There is nowhere in that shape to put an `enum`. Eight properties in
`tool_manifests/connectwisemcp.json` are enums, and one of them is
`cw_resolve.reference_type` — the twenty-value enum whose arrival is the only
reason `valid_tool_args` can fail at all. A stub that advertises it as a bare
string tells the agent under test it may send values production rejects, and
the resulting divergence would be something *we* caused. A stub that changes
the tool contract is not a stub.

So the schemas go out verbatim from the manifest, which needs an MCP server we
control. Hosting one on Functions is itself a documented Microsoft path —
"Host servers built with MCP SDKs on Azure Functions", Flex Consumption, custom
handler — and `host.json` carries the `mcp-custom-handler` configuration
profile for exactly this case. This is native hosting of a faithful stub rather
than extension hosting of a lossy one.

`tests/test_replay_hosting.py` asserts the enums are still there. If that test
ever fails because they are gone, the extension becomes a live option again;
reopen the decision rather than deleting the test.

---

## What is here

| File | |
|---|---|
| `server.py` | the handler. Routing, auth, sessions, state. |
| `host.json` | `mcp-custom-handler` profile; runs `python server.py` on port 8000 |
| `build.sh` | assembles `.build/` — copies the shared modules, manifests, cassettes |
| `deploy.sh` | `az deployment group create` then publish |
| `infra/main.bicep` | storage, Log Analytics, App Insights, FC1 plan, function app, identity, RBAC |
| `infra/main.bicepparam` | the knobs |

**Nothing in `.build/` is authored.** `mcp_core.py`, `state_store.py`,
`make_cassette.py` and `trace_to_eval.py` are copied from the one place each
lives. Two copies of the playback logic would be two answers to the question
this repo gates on. `tests/test_replay_hosting.py` asserts the local server and
this one are the *same functions*, not two that agree today.

---

## Routes

| | |
|---|---|
| `POST /mcp/<cassette-id>` | MCP streamable HTTP |
| `POST /mcp` | same, using `REPLAY_CASSETTE` |
| `GET /summary/<cassette-id>` | the replay journal for one session |
| `GET /` | health, and nothing else |

`/` is open so the platform can probe it. Everything else takes the bearer
token: `/summary` is the journal, which carries canonicalised tool arguments —
ticket and company identifiers — and every attempted write.

Sessions: `initialize` issues an `Mcp-Session-Id`; a client that echoes it gets
an isolated cursor and journal, so two replays of one cassette do not consume
each other's queue. A client that ignores the header shares one session per
cassette, which is the old single-process behaviour rather than a failure.

---

## App settings

| Setting | |
|---|---|
| `REPLAY_TOKEN` | required bearer token. No default — set it. |
| `REPLAY_CASSETTE` | default cassette when the URL names none |
| `REPLAY_CASSETTE_DIR` | default `./cassettes` |
| `REPLAY_TOOL_DEFS` | default `./tool_manifests` |
| `REPLAY_ON_EXHAUSTED` | `repeat` (default) or `diverge` |
| `REPLAY_STATE_ACCOUNT` | blob endpoint for replay state |
| `REPLAY_STATE_CONTAINER` | container for replay state |

Bicep sets all of these. Clear the last two and state stays in the process,
which is correct only while one instance serves a whole replay.

---

## Why replay state is in blob storage

A cassette replays *in order*: `cw_get_ticket 805392` returns five different
results across one orchestration because the agents mutate the ticket as they
go. The queue cursor is therefore the fidelity of the replay, and it is only
correct while every call of a run reaches the same process.

Azure Functions does not promise that. Flex Consumption will not scale out a
single sequential client in practice — but "in practice" is not what a gate
rests on, and the floor for `maximumInstanceCount` on that plan is **40**, so
pinning the app to one instance is not on offer either. Putting the cursor in
blob storage, guarded by its ETag, makes the question moot. It also means the
journal survives an instance recycle mid-run, which is what makes `/summary`
worth reading afterwards.

The ETag check is not there to arbitrate a race between agent turns, which are
sequential. It is there to make a lost update **loud**: two instances answering
one session means the replay is already unordered, and the server returns a
409 saying so rather than an answer that looks fine.

---

## Cost and blast radius

FC1 scales to zero; a gate that runs on merges costs close to nothing between
runs. Storage holds one small blob per session and the deployment package.

The app has a **user-assigned managed identity** and the storage account has
`allowSharedKeyAccess: false`, so there is no connection string anywhere to
leak. The identity is created before the site on purpose — assigning the role
to a principal that only exists once the site does is what breaks
identity-based deployment storage on a first deploy.

The app is reachable from the internet with a bearer token in front of it.
Before pointing it at anything beyond the committed fixtures, read
`docs/REPO-BOUNDARY.md`: a cassette is recorded customer data.
