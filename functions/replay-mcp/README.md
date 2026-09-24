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

Save that token when it prints. It cannot be read back from the deployment's
secure parameter, only from the app's settings (`REPLAY_TOKEN`), by anyone
allowed to list them. Re-running with `-NewToken` issues a new one and rotates
it -- which locks out every caller holding the old one, the gate included,
until they are updated. On a redeploy, set the existing token instead.

Either script provisions everything in `infra/main.bicep` and publishes the
package, then prints the `run_replay.py` command to use next.

### Then check it, because deployed is not the same as right

```
python3 functions/replay-mcp/verify.py https://<app>.azurewebsites.net \
    --token "$REPLAY_TOKEN"
```

It waits up to 180 seconds for the server to come up first (`--wait`), because
a new deployment takes a while to start serving and a Flex app that scaled to
zero takes time to come back — reporting either as a failure sends you to read
logs about a server that was only starting.

Then it replays every deployed cassette from the local copy of the same
recording and checks what the gate rests on: every recorded call comes back
byte-identical, writes are replayed as recorded successes, the advertised
schemas still carry their enums, two sessions do not consume each other's
queue, a concurrent fan-out is answered and journalled in full, and replay
state is in blob storage (not in the process) with a SAS that has not expired.
Exits non-zero on any of them, so it can gate a deployment.

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
profile for exactly this case — preview-flagged, and `infra/main.bicep` sets
`AzureWebJobsFeatureFlags=EnableMcpCustomHandlerPreview` because Microsoft's
sample does. So this is native hosting of a faithful stub rather than extension
hosting of a lossy one. Still the better trade: a preview flag is a smaller
problem than a stub that cannot advertise an enum.

`tests/test_replay_hosting.py` asserts the enums are still there. If that test
ever fails because they are gone, the extension becomes a live option again;
reopen the decision rather than deleting the test.

---

## What is here

| File | |
|---|---|
| `server.py` | the handler. Routing, auth, sessions, state. |
| `host.json` | `mcp-custom-handler` profile; runs `python server.py` on port 8000 |
| `build.py` | assembles `.build/` — flat, one `replay_payload.json`, no directories |
| `build.sh` | wrapper, so `make replay-package` keeps working |
| `deploy.sh` / `deploy.ps1` | `az deployment group create` then package deployment |
| `infra/rbac.bicep` | the role assignment alone, for whoever can make one |
| `verify.py` | post-deploy proof: replays every cassette and compares |
| `diagnose.py` | when it is not serving: state, settings, and the handler's own output |
| `infra/main.bicep` | storage, Log Analytics, App Insights, FC1 plan, function app, identity, RBAC |
| `infra/main.bicepparam` | the knobs |

### The package is flat, and that is not tidiness

`.build/` contains **no directories**. The deployment keeps files at the root
of `wwwroot` and drops subdirectories: `lib/` went that way first, and once it
was flattened `tool_manifests/` went the same way — each time as a 502 with a
stack trace behind it and a round trip to find out.

So every cassette and every tool manifest is in one file,
`replay_payload.json`, beside `server.py`. `build.py` asserts the package has
no directories before it finishes. A checkout still has `cassettes/` and
`tool_manifests/` as real directories — `make cassettes` writes into them — so
`Source` reads the payload when it is there and the directories when it is
not. One reader, both shapes.

**Nothing in `.build/` is authored.** `mcp_core.py`, `state_store.py`,
`make_cassette.py` and `trace_to_eval.py` are copied from the one place each
lives. The build is Python, not shell, for the same reason: half this repo's
users are on PowerShell, and a second build implementation would drift from
the first exactly like a second playback implementation would. Two copies of the playback logic would be two answers to the question
this repo gates on. `tests/test_replay_hosting.py` asserts the local server and
this one are the *same functions*, not two that agree today.

### When it answers 502

**The deploy already ran the diagnostic for you.** If the publish ends with the
app unhealthy, `deploy.sh` and `deploy.ps1` run `diagnose.py` themselves and
print the handler's own output — handing over a command to run next is two
round trips where one would do.

The Functions host is up and the handler is not answering on its port. That is
all a 502 says — it looks identical whether the process crashed, never
started, started too slowly, or bound somewhere else. What the handler printed
says which:

```
python3 functions/replay-mcp/diagnose.py -g <resource-group>
```

App state, plan, which settings are set (**names only** — `REPLAY_TOKEN` and
the storage connection string are in there), the last deployment, and the
handler's own stdout from Application Insights.

The server is built not to be the cause. Start-up prints the interpreter, its
path and the working directory before anything can fail; a missing module says
which one and where it looked; and the replay state store is reached on
**first use**, not at start-up, because anything slow on the start-up path
(a token, a firewall) turns into a host that gives up and a 502 that says
nothing. If the configured store cannot be reached the server still starts,
but answers every MCP call with `replay state unavailable: <cause>` and tries
the store again a few seconds later -- it never answers from in-process state
another instance cannot see. `GET /?resolve=1` reports the store or the cause,
and `verify.py` **fails** anything but blob storage.

---

## Publishing uses az, not Core Tools

`func azure functionapp publish <name>` resolves the app by searching the
subscription and reads only the first page of results. Past roughly 999
resources it reports **"Can't find app with name"** for an app that plainly
exists. `az` takes the resource group explicitly and does not search, so both
scripts use:

```
az functionapp deployment source config-zip -g <rg> -n <app> --src package.zip
```

No `--build-remote`: there is nothing to build (see below). Despite the
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
| `GET /` | health, and nothing else; `?resolve=1` reaches the state store first |

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
| `REPLAY_STATE_CONTAINER` | container for replay state (both modes) |
| `REPLAY_STATE_ACCOUNT` | blob endpoint; with the container, state is kept as the managed identity (`storageAuth=identity`) |
| `AZURE_CLIENT_ID` | the user-assigned identity to ask for a token as (`storageAuth=identity`) |
| `REPLAY_STATE_SAS` | container URL with a SAS (`storageAuth=connectionString`); wins when set |

Bicep sets `REPLAY_TOKEN`, `REPLAY_CASSETTE`, `REPLAY_ON_EXHAUSTED`,
`REPLAY_STATE_CONTAINER`, and the state settings for the mode it deploys;
the two directories default to the flat package's payload. With no state
setting at all, state is in the process: right for a laptop, and `verify.py`
fails it on a hosted app.

`infra/main.bicepparam` takes its own values from the environment —
`REPLAY_NAME`, `REPLAY_LOCATION`, `REPLAY_TOKEN`, `REPLAY_CASSETTE`,
`REPLAY_ON_EXHAUSTED`, `REPLAY_STORAGE_AUTH`, `REPLAY_ASSIGN_ROLE` — rather
than from inline `-p` overrides, because the
Azure CLI accepts **one** parameter source per deployment: a `.bicepparam`
file *or* inline parameters, never both. Environment variables are how a
parameter file stays parameterised, and they read the same from bash and
PowerShell.

---

## Replay state uses no SDK, and the deployment does no build

`azure-storage-blob` is **not importable in a custom handler.** Oryx installs
it into `.python_packages/lib/site-packages`, which the Functions *Python
worker* puts on `sys.path` — and a custom handler is `python server.py` with
none of that setup. The deployed app said so plainly:

```
WARNING  replay state could not use blob storage: ModuleNotFoundError: No module named 'azure'
```

That is why the journal came back with 3 of 50 calls in it: the store had
degraded to in-process, every instance kept its own cursor, and calls were
answered with the first recorded response where a later one was due.

So state goes over the **blob REST API**, `urllib` and `json`, authorised one
of two ways:

| `storageAuth` | How | Expires |
|---|---|---|
| `identity` (default) | the app's user-assigned managed identity: a token from the platform's identity endpoint (`IDENTITY_ENDPOINT`, `X-IDENTITY-HEADER`, api 2019-08-01), sent as a bearer header | nothing |
| `connectionString` | a container SAS minted at deploy time and handed over as `REPLAY_STATE_SAS` | `stateSasExpiry`, a year by default; `/` reports it, `verify.py` warns 30 days out and fails after. Redeploy to roll it. |

The identity path used to go through `azure-storage-blob`, which cannot import
here, so the **default** deployment silently kept state in-process. It is
stdlib now; the SDK store is gone. A role assigned directly to the identity
takes up to ~10 minutes to take effect; until then verify reports the store
`unavailable` with a 403, and the server retries by itself.

`requirements.txt` is therefore empty and the deployment asks for **no remote
build**. A build that installs packages where nothing looks is worse than no
build: it succeeds, and the app is still missing its dependency.

Nothing here needs a dependency, so `server.py` no longer puts
`.python_packages` on `sys.path` either.

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

The ETag makes a lost update **loud**. Calls in one session are not
sequential -- the ops agent sends up to nine at once -- so the server
serialises a session's calls within an instance, and when another instance
saves first it reloads and re-applies the call. Only a race lost on every
retry is a 409, saying the replay is unordered rather than giving an answer
that looks fine.

---

## Storage auth, and the one permission Contributor lacks

`infra/main.bicep` takes `storageAuth`:

| | Needs | Cost |
|---|---|---|
| `identity` (default) | `Microsoft.Authorization/roleAssignments/write` at deploy time — Role Based Access Control Administrator, User Access Administrator or Owner | none. No key exists anywhere. |
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

**If you hold one of those roles as well as Contributor**, just deploy on the
default `identity`: the template assigns the role itself.

**If the roles are split between people**, the role holder grants it --
`infra/rbac.bicep` (running a deployment needs Contributor as well, or Owner
alone) or the one `az role assignment create` in its header -- and you
redeploy on identity
*without* declaring the assignment, which a Contributor cannot put even when
it already exists:

```powershell
.\deploy.ps1 -ResourceGroup <rg> -StorageAuth identity -SkipRoleAssignment
```

```bash
REPLAY_STORAGE_AUTH=identity REPLAY_ASSIGN_ROLE=false ./deploy.sh <rg>
```

The key and the SAS disappear from configuration and nothing else about the
app changes. The deploy scripts print the storage account and identity names
they need. A role assigned directly to the identity takes up to ~10 minutes to
take effect; the same identity carries the host's storage and the deployment
package, so in that window the app may not start at all. rbac.bicep computes
the assignment name the same way main.bicep does, so running both is
idempotent rather than a duplicate.

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
