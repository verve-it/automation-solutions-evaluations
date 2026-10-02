#!/usr/bin/env python3
"""
verify.py — prove the hosted replay server is a faithful stub.

    python3 functions/replay-mcp/verify.py https://<app>.azurewebsites.net \
        --token "$REPLAY_TOKEN"

Deploying it is not the same as it being right. This replays every cassette
the server carries, from the local copy of the same recording, and checks the
things the gate rests on:

  1. Every recorded call comes back byte-identical.
  2. Writes are replayed as recorded successes and nothing is written.
  3. The advertised schemas still carry their enums -- the reason this is a
     custom handler and not an mcpToolTrigger. A stub that advertises a looser
     contract than production invites divergence it then blames on the agent.
  4. Two sessions on one cassette do not consume each other's queue.
  5. Calls sent together on one session are all answered and all journalled.
     The ops agent fans out -- nine MCP calls in flight at once in its
     recordings -- and a server that loses those races hands the agent errors
     where the recording had results.
  6. Replay state is in blob storage (managed identity or SAS), not in the
     process, and a SAS has not expired. In-process state holds a replay's
     order only while one instance serves all of it.

Exits non-zero if any of that fails, so it can gate a deployment.

Stdlib only, and no Azure SDK: this has to run from anywhere that can reach
the URL, including a laptop with no az login.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))


class Client:
    def __init__(self, base, token=None, timeout=60):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method, path, body=None, session=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if session:
            req.add_header("Mcp-Session-Id", session)
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            payload = json.loads(response.read() or b"{}")
            return payload, dict(response.headers)

    def health(self, resolve=False):
        return self._request("GET", "/?resolve=1" if resolve else "/")[0]

    def initialize(self, cassette):
        body, headers = self._request(
            "POST", f"/mcp/{cassette}",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        return body, headers.get("Mcp-Session-Id")

    def tools(self, cassette, session):
        body, _ = self._request(
            "POST", f"/mcp/{cassette}",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
        return body["result"]["tools"]

    def call(self, cassette, session, name, arguments, rid):
        body, _ = self._request(
            "POST", f"/mcp/{cassette}",
            {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}, session)
        return body["result"]

    def summary(self, cassette, session):
        return self._request("GET", f"/summary/{cassette}",
                             session=session)[0]


def local_cassette(cassette_dir, cassette_id):
    path = os.path.join(cassette_dir, f"{cassette_id}.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def check_schemas(tools):
    """The enums are the reason this is a custom handler. Confirm they arrived.

    The Functions MCP extension's flat toolProperties has nowhere to put one.
    If they are missing here, the server is advertising a looser contract than
    production and every divergence it produces is ours.
    """
    enums = {}
    for tool in tools:
        for name, schema in (tool.get("inputSchema", {})
                             .get("properties") or {}).items():
            options = schema.get("enum") or next(
                (b["enum"] for b in schema.get("anyOf", []) if "enum" in b),
                None)
            if options:
                enums[f"{tool['name']}.{name}"] = len(options)
    return enums


def advertised_local_tools(tools, local=None):
    """Tools the agent runs itself that the server lists as its own.

    A server built before the fix advertised every recorded name, so the
    agent under replay was offered `<label>___load_skill` beside its real
    one -- a tool production never lists. That changes the trajectory the
    gate scores, so a deployment still doing it fails verification: the
    fix is a redeploy, and this is what says so.
    """
    if local is None:
        if REPO_ROOT not in sys.path:
            sys.path.insert(0, REPO_ROOT)
        import evalconfig
        local = evalconfig.local_tools()
    return sorted({t.get("name") for t in tools} & set(local))


def _first_diff(a, b):
    """Offset of the first character at which a and b differ."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))


def replay(client, cassette_id, recording, verbose=False):
    _info, session = client.initialize(cassette_id)
    if not session:
        return None, ["server issued no Mcp-Session-Id on initialize"]

    tools = client.tools(cassette_id, session)
    problems = []
    enums = check_schemas(tools)
    leaked = advertised_local_tools(tools)
    if leaked:
        problems.append(
            f"advertises the agent's own tools as the server's: "
            f"{', '.join(leaked)}. Production never lists them, so the agent "
            "under replay is offered tools it does not have. The deployed "
            "server predates the fix; redeploy it.")

    matched = mismatched = writes = 0
    for interaction in recording["interactions"]:
        result = client.call(cassette_id, session, interaction["tool"],
                             interaction["arguments"], interaction["seq"])
        got = result["content"][0]["text"]
        if got == interaction["result"]:
            matched += 1
        else:
            mismatched += 1
            if verbose:
                # Lengths and where they part, not the text: both sides are a
                # recorded ConnectWise response, and -v is how a deploy that
                # fails gets rerun in a public job log.
                rec = interaction["result"]
                problems.append(
                    f"seq {interaction['seq']} {interaction['tool']}: "
                    f"expected {len(rec)} chars, got {len(got)}; first "
                    f"difference at char {_first_diff(rec, got)}")
        if interaction["is_write"]:
            writes += 1

    summary = client.summary(cassette_id, session)
    replayed = len(recording["interactions"])
    journalled = summary.get("replayed_calls")
    if journalled != replayed:
        # The journal is the replay state. Losing most of it means the calls
        # went to instances that could not see each other's cursor -- so the
        # queue positions were wrong too, which is what the mismatches are.
        # Ordering is the whole reason a cassette is a queue and not a
        # dictionary, so this is a failed gate, not a reporting glitch.
        problems.append(
            f"journal has {journalled} calls, replayed {replayed} — replay "
            "state is not shared between instances. Each one kept its own "
            "cursor, so a call could be answered with the first recorded "
            "response where a later one was due. Ordering is the gate; "
            "without shared state the result cannot be trusted.")
    if summary.get("diverged"):
        problems.append(f"{summary['diverged']} call(s) diverged")

    return {"session": session, "tools": len(tools), "enums": enums,
            "matched": matched, "mismatched": mismatched, "writes": writes,
            "summary": summary}, problems


def check_isolation(client, cassette_id, recording):
    """Two sessions must not consume each other's queue.

    Uses the first interaction only: if the second session gets the second
    recorded response instead of the first, the cursor is shared and the
    replay is not ordered per run.
    """
    first = recording["interactions"][0]
    _a, session_a = client.initialize(cassette_id)
    _b, session_b = client.initialize(cassette_id)
    if not session_a or not session_b or session_a == session_b:
        return ["server did not issue distinct sessions"]
    got_a = client.call(cassette_id, session_a, first["tool"],
                        first["arguments"], 900)["content"][0]["text"]
    got_b = client.call(cassette_id, session_b, first["tool"],
                        first["arguments"], 901)["content"][0]["text"]
    if got_a != first["result"] or got_b != first["result"]:
        return ["a second session did not start at the beginning of the "
                "queue -- replay state is shared between runs"]
    return []


def check_fan_out(client, cassette_id, recording, width=9):
    """Calls sent together on one session: all answered, all journalled.

    Uses the first recorded call of each distinct key, so every answer is
    known -- the head of its own queue -- whatever order the server takes
    them in. The width matches the widest burst in the recordings.

    What this proves on a deployment is the in-instance path: one client's
    burst reaches one instance, where the per-session lock serialises it. The
    cross-instance retry is proven by tests/test_replay_hosting.py, where
    another instance really advances the cursor between a load and a save.
    """
    heads, seen = [], set()
    for interaction in recording["interactions"]:
        if interaction["key"] not in seen:
            seen.add(interaction["key"])
            heads.append(interaction)
    heads = heads[:width]
    if len(heads) < 2:
        return []
    _info, session = client.initialize(cassette_id)

    def one(i):
        try:
            got = client.call(cassette_id, session, i["tool"], i["arguments"],
                              1000 + i["seq"])["content"][0]["text"]
            return None if got == i["result"] else f"seq {i['seq']} differed"
        except urllib.error.HTTPError as exc:
            return f"seq {i['seq']} got HTTP {exc.code}"

    with concurrent.futures.ThreadPoolExecutor(len(heads)) as pool:
        wrong = [w for w in pool.map(one, heads) if w]
    problems = []
    if wrong:
        problems.append(f"{len(wrong)} of {len(heads)} concurrent calls on one "
                        f"session failed ({'; '.join(wrong[:3])}). The agent "
                        "fans out; a server that loses those races answers "
                        "with errors the recording never had.")
    journalled = client.summary(cassette_id, session).get("replayed_calls")
    if journalled != len(heads):
        problems.append(f"{journalled} of {len(heads)} concurrent calls were "
                        "journalled. The gate compares the trace with the "
                        "journal, so a dropped entry reads as a call that "
                        "never reached the stub.")
    return problems


# The stores that survive an instance recycle and are shared by every
# instance -- state_store.DURABLE, restated here because this script runs
# from anywhere with no repo imports. tests/test_replay_hosting.py keeps the
# two in step.
DURABLE = ("IdentityBlobStore", "SasBlobStore")
SAS_WARN_DAYS = 30


def redeploy_commands(auth):
    """What to run, from the repo root, to redeploy in the mode it is in.

    A SAS is re-minted only by a connectionString deploy; the scripts'
    default is identity, which is a different change and, for whoever is on
    SAS because they cannot assign roles, one that fails."""
    windows = os.name == "nt"
    script = (".\\functions\\replay-mcp\\deploy.ps1 -ResourceGroup <rg>"
              if windows else "./functions/replay-mcp/deploy.sh <rg>")
    if auth == "container SAS":
        same = (f"{script} -StorageAuth connectionString" if windows
                else f"REPLAY_STORAGE_AUTH=connectionString {script}")
        move = (f"{script} -StorageAuth identity" if windows
                else f"REPLAY_STORAGE_AUTH=identity {script}")
        return same, move
    return script, None


def _unavailable(backend, detail):
    error = detail.get("error", "no cause given")
    why = (f"replay state ({detail.get('wanted', backend)}, "
           f"{detail.get('auth', '?')}) failed: {error}. The server answers "
           "every MCP call with an error while it does, and tries the store "
           "again every few seconds.")
    if "403" in error and detail.get("auth") == "managed identity":
        why += (" A 403 as the identity means it has no data role on the "
                "storage account yet: a role assigned directly to it takes "
                "up to ~10 minutes to take effect. Run this again after "
                "that; nothing needs restarting.")
    return why


def check_state(health, now=None):
    """Replay state must be durable. Anything else is a failed deployment.

    This used to be a NOTE. In-process state holds a replay's order only
    while one instance serves the whole run, and Flex Consumption promises
    no such thing -- the 50-call replay that journalled 3 was exactly that.
    `health` should come from GET /?resolve=1, which resolves the store on
    the instance that answers, so `unresolved` is not a verdict about a store
    nobody has used yet.
    """
    import datetime as dt
    backend = health.get("state")
    detail = health.get("state_detail") or {}
    print(f"\nstate after replaying: {backend} "
          f"({detail.get('auth', 'no detail')})")
    same, move = redeploy_commands(detail.get("auth"))
    days = None
    expires = detail.get("expires")
    if expires:
        try:
            when = dt.datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            days = (when - (now or dt.datetime.now(dt.timezone.utc))).days
        except ValueError:
            print(f"  WARNING    cannot read the state SAS expiry {expires!r}")

    problems = []
    expired = days is not None and days < 0
    alternative = (f", or move to storageAuth=identity, which has nothing to "
                   f"expire ({move})" if move else "")
    if expired:
        problems.append(f"the state SAS expired on {expires}. Redeploy to mint "
                        f"a new one ({same}){alternative}.")
    elif days is not None and days < SAS_WARN_DAYS:
        print(f"  WARNING    the state SAS expires in {days} day(s), on "
              f"{expires}. Redeploy before then ({same}){alternative}.")

    if backend in DURABLE:
        # Reached once is not reached now: a store whose last call failed
        # is reported with the failure.
        if detail.get("error") and not expired:
            problems.append(_unavailable(backend, detail))
        return problems
    if backend == "unavailable":
        if not expired:
            problems.append(_unavailable(backend, detail))
    elif backend == "MemoryStore":
        problems.append(
            "replay state is in-process: no REPLAY_STATE_* setting reached "
            "the handler (its startup log prints the names it saw on the "
            "`state env :` line). A replay stays ordered only while one "
            "instance serves it, and the journal the gate reads does not "
            "survive a restart.")
    elif backend == "unresolved":
        problems.append(
            "the server did not resolve its replay state when asked "
            "(GET /?resolve=1). That is a server.py older than this "
            f"verify.py; redeploy ({same}).")
    else:
        problems.append(
            f"/health reported replay state {backend!r}, which is not a "
            "store this verify.py knows. A server.py older or newer than "
            f"this checkout? Redeploy from it ({same}).")
    return problems


def _http_failure(exc):
    """An HTTPError as a line that carries the server's own reason."""
    if exc.code == 401:
        return "HTTP 401 (check --token)"
    reason = exc.reason
    try:
        body = json.loads(exc.read() or b"{}")
        reason = ((body.get("error") or {}).get("message")
                  if isinstance(body.get("error"), dict)
                  else body.get("message")) or reason
    except Exception:
        pass
    return f"HTTP {exc.code} ({reason})"


class _Resolving:
    """The client, asking /health to resolve its store first."""

    def __init__(self, client):
        self._client = client
        self.base = client.base

    def health(self):
        return self._client.health(resolve=True)


def wait_for_health(client, seconds):
    """Poll until the server answers, or until it is fair to call it broken.

    A new deployment takes a while to start serving, and a Flex Consumption
    app that has scaled to zero takes time to come back. Reporting
    either as a failure sends someone to read logs about a server that was
    only starting. So wait, and say what it is doing while waiting.

    Returns (health, None) or (None, why it failed).
    """
    deadline = time.monotonic() + max(seconds, 0)
    waited = False
    while True:
        try:
            return client.health(), None
        except urllib.error.HTTPError as exc:
            transient = exc.code in (502, 503, 504)
            detail = f"health check returned {exc.code}: {exc.reason}"
        except Exception as exc:
            transient = True
            detail = f"cannot reach {client.base}: {exc}"

        if not transient or time.monotonic() >= deadline:
            if transient:
                detail += (f"\n  Still not answering after {seconds}s. The "
                           "Functions host is up and the handler is not.")
            return None, detail

        if not waited:
            print(f"waiting for the server to come up (up to {seconds}s) — "
                  "a new deployment, or an app scaled to zero, starts slowly",
                  flush=True)
            waited = True
        time.sleep(5)


def run_diagnosis(resource_group):
    """Print the handler's own output rather than a command to get it.

    Without a resource group there is nothing to run, and printing a command
    with `<resource-group>` still in it is what made the last round trip a
    round trip. Say which flag would have answered it instead.
    """
    if not resource_group:
        print("\n  Pass -g <resource-group> and this runs the diagnosis for "
              "you. On its own:\n"
              f"    {'python' if os.name == 'nt' else 'python3'} "
              "functions/replay-mcp/diagnose.py -g <resource-group>")
        return
    script = os.path.join(HERE, "diagnose.py")
    print(f"\n=== diagnosing {resource_group} ===", flush=True)
    subprocess.run([sys.executable, script, "-g", resource_group])


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base_url", help="https://<app>.azurewebsites.net")
    ap.add_argument("--token", default=os.environ.get("REPLAY_TOKEN"),
                    help="bearer token the server requires")
    ap.add_argument("--cassette-dir",
                    default=os.path.join(REPO_ROOT, "cassettes"),
                    help="local recordings to compare against")
    ap.add_argument("--cassette", action="append",
                    help="check only this one; repeatable")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every mismatch")
    ap.add_argument("-g", "--resource-group",
                    help="run the diagnosis automatically if the server never "
                         "answers, instead of printing a command to run next")
    ap.add_argument("--wait", type=int, default=180,
                    help="seconds to let the server finish starting before "
                         "calling it broken (default 180). 0 checks once.")
    args = ap.parse_args(argv)

    client = Client(args.base_url, args.token)

    health, problem = wait_for_health(client, args.wait)
    if problem:
        _fail(problem)
        run_diagnosis(args.resource_group)
        return 1

    remote = health.get("cassettes") or []
    print(f"server    : {args.base_url}")
    print(f"cassettes : {len(remote)} deployed")
    if health.get("state"):
        print(f"state     : {health['state']}")
    if health.get("writes") != "never performed":
        return _fail("health endpoint does not report the write guarantee; "
                     "this is not the replay server")

    wanted = args.cassette or remote
    missing_locally, failures, checked = [], [], 0

    # The gate replays every cassette built here, against this server. One
    # the server does not have 404s only after the agent under test has been
    # invoked, and reads as the agent's failure. Checking only what the server
    # lists would pass exactly that: a server deployed before a trace was
    # committed.
    if not args.cassette and os.path.isdir(args.cassette_dir):
        built = sorted(f[:-len(".json")] for f in os.listdir(args.cassette_dir)
                       if f.endswith(".json"))
        for cassette_id in built:
            if cassette_id not in remote:
                failures.append(
                    f"{cassette_id}: built from the committed traces but not "
                    "deployed; the server predates it. Redeploy: "
                    + (".\\tasks.ps1 replay-deploy -ResourceGroup <rg>"
                       if os.name == "nt" else "make replay-deploy RG=<rg>"))

    for cassette_id in wanted:
        if cassette_id not in remote:
            failures.append(f"{cassette_id}: not deployed")
            continue
        recording = local_cassette(args.cassette_dir, cassette_id)
        if recording is None:
            missing_locally.append(cassette_id)
            continue

        print(f"\n{cassette_id}")
        try:
            report, problems = replay(client, cassette_id, recording,
                                      args.verbose)
        except urllib.error.HTTPError as exc:
            failures.append(f"{cassette_id}: {_http_failure(exc)}")
            continue
        if report is None:
            failures.extend(f"{cassette_id}: {p}" for p in problems)
            continue

        # A store that goes away mid-run is a 503 by design; reported here
        # rather than as a traceback that skips the state check.
        for check in (check_isolation, check_fan_out):
            try:
                problems += check(client, cassette_id, recording)
            except urllib.error.HTTPError as exc:
                problems.append(f"{check.__name__}: {_http_failure(exc)}")
            except urllib.error.URLError as exc:
                problems.append(f"{check.__name__}: {exc.reason}")
        checked += 1

        total = len(recording["interactions"])
        print(f"  replayed   : {report['matched']}/{total} byte-identical, "
              f"{report['mismatched']} mismatched")
        print(f"  writes     : {report['writes']} replayed as recorded, "
              f"none performed")
        print(f"  tools      : {report['tools']} advertised")
        if report["enums"]:
            shown = ", ".join(f"{k} ({v})"
                              for k, v in sorted(report["enums"].items()))
            print(f"  enums kept : {shown}")
        print(f"  divergence : {report['summary'].get('diverged', '?')}")

        if report["mismatched"]:
            problems.append(f"{report['mismatched']} recorded call(s) came "
                            "back different")
        failures.extend(f"{cassette_id}: {p}" for p in problems)

    after, why = wait_for_health(_Resolving(client), 60)
    if after is None:
        failures.append(f"could not read /health after replaying: {why}")
    else:
        failures.extend(check_state(after))

    print()
    if missing_locally:
        print("no local recording to compare against, skipped: "
              + ", ".join(missing_locally))
        print("  (run `.\\tasks.ps1 cassettes`)" if os.name == "nt"
              else "  (run `make cassettes`)")

    if failures:
        print("FAILED")
        for failure in failures:
            print(f"  {failure}")
        # Any failure, not only a server that never answered. A wrong answer
        # needs the handler's own output just as much as a missing one, and
        # printing a command to fetch it is the round trip this exists to
        # remove.
        run_diagnosis(args.resource_group)
        return 1

    if not checked:
        return _fail("nothing was checked")

    print(f"OK — {checked} cassette(s) replay identically against the hosted "
          "server.")
    print("No ConnectWise request was made and no write was performed.")
    return 0


def _fail(message):
    print(f"FAILED\n  {message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
