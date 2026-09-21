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

**bash**

```
export REPLAY_TOKEN=$(openssl rand -hex 32)
./deploy.sh my-resource-group eastus2
```

**PowerShell** — there is no `openssl` and no `export` on a stock Windows box,
so `-NewToken` generates one and prints it once:

```powershell
.\deploy.ps1 -ResourceGroup my-resource-group -NewToken
```

Save that token when it prints. It goes into a secure app setting and Azure
will not read it back out, so it is not recoverable from the deployment.
Re-running with `-NewToken` issues a new one and rotates it.

Either script provisions everything in `infra/main.bicep` and publishes the
package, then prints the `run_replay.py` command to use next.

### Then check it, because deployed is not the same as right

```
python3 functions/replay-mcp/verify.py https://<app>.azurewebsites.net \
    --token "$REPLAY_TOKEN"
```

It replays every deployed cassette from the local copy of the same recording
and checks the four things the gate rests on: every recorded call comes back
byte-identical, writes are replayed as recorded successes, the advertised
schemas still carry their enums, and two sessions do not consume each other's
queue. Exits non-zero on any of them, so it can gate a deployment.

### Region

A resource group's location says where its *metadata* lives; the resources
inside it may sit anywhere. So an existing group in a region Flex Consumption
does not serve is not a reason to make a second group — pass a supported
region and the resources go there:

```powershell
.\deploy.ps1 -ResourceGroup my-resource-group -Location eastus2
```

Omit it and the scripts use the group's own region if it already exists, or
`eastus2` for a new one. Either way they check the region against
`az functionapp list-flexconsumption-locations` **before** deploying and print
the supported list if it does not qualify — the alternative is a template
failure three resources in.

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
| `build.py` | assembles `.build/` — rebuilds the cassettes, copies the shared modules and manifests |
| `build.sh` | wrapper, so `make replay-package` keeps working |
| `deploy.sh` / `deploy.ps1` | `az deployment group create` then package deployment |
| `infra/rbac.bicep` | the role assignment alone, for whoever can make one |
| `verify.py` | post-deploy proof: replays every cassette and compares |
| `infra/main.bicep` | storage, Log Analytics, App Insights, FC1 plan, function app, identity, RBAC |
| `infra/main.bicepparam` | the knobs |

**Nothing in `.build/` is authored.** `mcp_core.py`, `state_store.py`,
`make_cassette.py` and `trace_to_eval.py` are copied from the one place each
lives. The build is Python, not shell, for the same reason: half this repo's
users are on PowerShell, and a second build implementation would drift from
the first exactly like a second playback implementation would. Two copies of the playback logic would be two answers to the question
this repo gates on. `tests/test_replay_hosting.py` asserts the local server and
this one are the *same functions*, not two that agree today.

---

## Publishing uses az, not Core Tools

`func azure functionapp publish <name>` resolves the app by searching the
subscription and reads only the first page of results. Past roughly 999
resources it reports **"Can't find app with name"** for an app that plainly
exists. `az` takes the resource group explicitly and does not search, so both
scripts use:

```
az functionapp deployment source config-zip -g <rg> -n <app> \
    --src package.zip --build-remote true
```

`--build-remote` is not optional for Python: `requirements.txt` has to be
installed somewhere, and it is not going to be a Windows laptop. Despite the
command's name this routes to **Flex Consumption package deployment**, which is
the only deployment technology Flex supports — plain zip deploy is not.

The scripts also wait for the app to become readable before publishing. ARM
returns before a new app is consistently visible, and publishing into that
window fails in a way that reads like the app was never created.

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

`infra/main.bicepparam` takes its own values from the environment —
`REPLAY_NAME`, `REPLAY_LOCATION`, `REPLAY_TOKEN`, `REPLAY_CASSETTE`,
`REPLAY_ON_EXHAUSTED` — rather than from inline `-p` overrides, because the
Azure CLI accepts **one** parameter source per deployment: a `.bicepparam`
file *or* inline parameters, never both. Environment variables are how a
parameter file stays parameterised, and they read the same from bash and
PowerShell.

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

## Storage auth, and the one permission Contributor lacks

`infra/main.bicep` takes `storageAuth`:

| | Needs | Cost |
|---|---|---|
| `identity` (default) | `Microsoft.Authorization/roleAssignments/write` at deploy time — User Access Administrator or Owner | none. No key exists anywhere. |
| `connectionString` | nothing beyond Contributor | a storage account key sits in app settings, readable by anyone who can read the app's configuration, and it does not rotate itself |

Contributor stops at exactly one step: granting the identity Storage Blob Data
Owner. Everything before it succeeds, so the failure arrives late, with the
storage account already created. Both deploy scripts recognise it and print the
two ways on.

**If you cannot assign roles:**

```powershell
.\deploy.ps1 -ResourceGroup my-resource-group -StorageAuth connectionString
```

```bash
REPLAY_STORAGE_AUTH=connectionString ./deploy.sh my-resource-group
```

**Then, when someone who can assign roles is available**, they run
`infra/rbac.bicep` — the role assignment alone, nothing else, safe to run while
the app is serving:

```bash
az deployment group create -g <rg> -f infra/rbac.bicep \
    -p storageAccountName=<storage> identityName=<name>-replay-id
```

and you redeploy as normal. The key disappears from configuration and nothing
else about the app changes. Both templates compute the assignment name from the
same inputs, so running both is idempotent rather than a duplicate.

The identity is created in **both** modes, deliberately. ARM evaluates both
sides of a ternary, so a reference to a resource that only sometimes exists is
a deployment error rather than a dead branch — only the role assignment is
conditional, and nothing reads its output.

---

## Cost and blast radius

FC1 scales to zero; a gate that runs on merges costs close to nothing between
runs. Storage holds one small blob per session and the deployment package.

Under `storageAuth: identity` the app has a **user-assigned managed identity**
and the storage account has `allowSharedKeyAccess: false`, so there is no
connection string anywhere to leak. Under `connectionString` there is one, in
app settings — that is the trade, and it is why `identity` is the default. The identity is created before the site on purpose — assigning the role
to a principal that only exists once the site does is what breaks
identity-based deployment storage on a first deploy.

The app is reachable from the internet with a bearer token in front of it.
Before pointing it at anything beyond the committed fixtures, read
`docs/REPO-BOUNDARY.md`: a cassette is recorded customer data.
