#!/usr/bin/env python3
"""
verify.py — prove the hosted replay server is a faithful stub.

    python3 functions/replay-mcp/verify.py https://<app>.azurewebsites.net \
        --token "$REPLAY_TOKEN"

Deploying it is not the same as it being right. This replays every cassette
the server carries, from the local copy of the same recording, and checks the
four things the gate rests on:

  1. Every recorded call comes back byte-identical.
  2. Writes are replayed as recorded successes and nothing is written.
  3. The advertised schemas still carry their enums -- the reason this is a
     custom handler and not an mcpToolTrigger. A stub that advertises a looser
     contract than production invites divergence it then blames on the agent.
  4. Two sessions on one cassette do not consume each other's queue.

Exits non-zero if any of that fails, so it can gate a deployment.

Stdlib only, and no Azure SDK: this has to run from anywhere that can reach
the URL, including a laptop with no az login.
"""

from __future__ import annotations

import argparse
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

    def health(self):
        return self._request("GET", "/")[0]

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


def replay(client, cassette_id, recording, verbose=False):
    _info, session = client.initialize(cassette_id)
    if not session:
        return None, ["server issued no Mcp-Session-Id on initialize"]

    tools = client.tools(cassette_id, session)
    problems = []
    enums = check_schemas(tools)

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
                problems.append(
                    f"seq {interaction['seq']} {interaction['tool']}: "
                    f"expected {interaction['result'][:80]!r} "
                    f"got {got[:80]!r}")
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


def wait_for_health(client, seconds):
    """Poll until the server answers, or until it is fair to call it broken.

    A remote build finishes *after* the publish command returns, and a Flex
    Consumption app that has scaled to zero takes time to come back. Reporting
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
                  "a remote build finishes after the publish returns",
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
              "    python3 functions/replay-mcp/diagnose.py -g "
              "<resource-group>")
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
            detail = "check --token" if exc.code == 401 else exc.reason
            failures.append(f"{cassette_id}: HTTP {exc.code} ({detail})")
            continue
        if report is None:
            failures.extend(f"{cassette_id}: {p}" for p in problems)
            continue

        problems += check_isolation(client, cassette_id, recording)
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

        backend = health.get("state")
        if backend == "MemoryStore":
            print("  NOTE       replay state is in-process, not blob storage. "
                  "Ordering holds only while one instance serves the run.")
        if report["mismatched"]:
            problems.append(f"{report['mismatched']} recorded call(s) came "
                            "back different")
        failures.extend(f"{cassette_id}: {p}" for p in problems)

    backend = None
    try:
        backend = client.health().get("state")
    except Exception:
        pass
    if backend:
        print(f"\nstate after replaying: {backend}")
        if backend == "MemoryStore":
            print("  In-process, not blob storage. Ordering holds only while "
                  "one instance serves the whole run, and Flex Consumption "
                  "does not promise that. The handler logs why the blob "
                  "store was not used.")

    print()
    if missing_locally:
        print("no local recording to compare against, skipped: "
              + ", ".join(missing_locally))
        print("  (run `make cassettes`)")

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
