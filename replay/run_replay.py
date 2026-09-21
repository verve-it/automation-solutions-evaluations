#!/usr/bin/env python3
"""
run_replay.py — the agent-change gate. Stubbed tools, no live service.

This is the gate, not the smoke test. An agent under test is bound to the
replay server instead of ConnectwiseMCP, so every tool call is answered from
a recorded run:

  * no ConnectWise request is made, for reads or writes
  * a write returns the response the real write returned, and writes nothing
  * the same ticket replays identically however the live system has moved on

The live staging replay (`.github/workflows/staging-replay.yml`) invokes real
tools against the dev instance. That is the weekly smoke test for the write
path. It is slow, costs judge inference, leaves state behind, and gives a
different answer each run. Do not use it to decide whether an agent change is
safe — use this.

    # one command, if the server is somewhere Foundry can reach
    python3 replay/run_replay.py --cassette cassettes/2026-09-03-4dda7f4fa5f0.json \\
        --agent triage-orchestrator --server-url https://replay.example.net/mcp

    # see exactly what it would do to the project, touch nothing
    python3 replay/run_replay.py --cassette ... --agent ... --dry-run

What it does, in order
----------------------
1. Serves the cassette (locally with --serve, or you host it).
2. Reads the agent version under test and clones its definition, replacing
   the ConnectWise toolbox binding with an MCP tool pointing at the replay
   server. Everything else is copied verbatim -- change anything else and you
   are evaluating a different agent.
3. Creates that clone as a temporary agent version, tagged in metadata.
4. Invokes it with the query from the cassette.
5. Collects /summary: matched prefix, first divergence, writes attempted.
6. Deletes the temporary version. Always, including on failure.

Reachability is the one real constraint
---------------------------------------
Foundry calls the replay server; the replay server does not call Foundry. So
the URL has to be reachable FROM Azure. `localhost` will not do, and this
script refuses it rather than letting you discover that as a timeout inside an
agent run. Host it, or put a tunnel in front of it.
"""

from __future__ import annotations

import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import argparse, json, subprocess, time, urllib.error, urllib.request
import datetime as _dt

REPLAY_TOOL_LABEL = "connectwise_replay"
TEMP_MARKER = "eval-replay-temp"


# ------------------------------------------------------------- reachability

LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")


def is_locally_scoped(url):
    """True when Azure could not reach this URL.

    Foundry calls the replay server, not the other way round. A localhost URL
    produces a tool call that times out inside an agent run, which surfaces as
    an agent failure rather than as a configuration mistake.
    """
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".local")


# ------------------------------------------------------------------ serving

def serve(cassette, port, tool_defs, journal):
    """Start replay_server.py as a subprocess and wait for it to answer."""
    cmd = [sys.executable, os.path.join(REPO_ROOT, "replay", "replay_server.py"),
           cassette, "--port", str(port)]
    if tool_defs:
        cmd += ["--tool-defs", tool_defs]
    if journal:
        cmd += ["--journal", journal]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://localhost:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base, timeout=1).read()
            return proc, base
        except urllib.error.URLError:
            time.sleep(0.2)
        except Exception:
            time.sleep(0.2)
    proc.kill()
    raise SystemExit(f"replay server did not come up on {base}")


def summary_url(server_url):
    """Where the journal for this replay lives.

    Locally the server is one cassette on one port and /summary is enough.
    Hosted, the cassette is a path segment -- https://host/mcp/<id> -- and the
    journal for it is https://host/summary/<id>. Deriving it here keeps the
    caller from having to pass two URLs that can disagree.
    """
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(server_url)
    parts = [p for p in parsed.path.split("/") if p]
    if "mcp" in parts:
        parts[len(parts) - 1 - parts[::-1].index("mcp")] = "summary"
    else:
        parts.append("summary")
    return urlunparse(parsed._replace(path="/" + "/".join(parts), query="",
                                      fragment=""))


def summary(base, token=None, session=None):
    req = urllib.request.Request(summary_url(base))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if session:
        req.add_header("Mcp-Session-Id", session)
    return json.loads(urllib.request.urlopen(req).read())


# ------------------------------------------------------------------- binding

def replay_tools(server_url, models, token=None):
    """The only difference between the agent under test and production.

    The token travels as a header rather than in the URL. `server_url` is
    stored on the agent version and repeated in every span, so a token in the
    query string would end up in App Insights and in anything exported from
    it.
    """
    kwargs = {}
    if token:
        kwargs["headers"] = {"Authorization": f"Bearer {token}"}
    return [models.MCPTool(
        server_label=REPLAY_TOOL_LABEL,
        server_url=server_url,
        server_description="Recorded ConnectWise responses. No live service.",
        require_approval="never",
        **kwargs,
    )]


def cloned_definition(base_version, server_url, models, token=None):
    """Copy the agent definition, swapping only its tools.

    Everything else -- model, instructions, temperature, reasoning, skills --
    is carried over untouched. A replayed agent already differs from
    production by its tool binding; letting anything else drift makes the
    result meaningless.
    """
    d = base_version.definition
    payload = d.as_dict() if hasattr(d, "as_dict") else dict(d)
    payload["tools"] = [t.as_dict() if hasattr(t, "as_dict") else t
                        for t in replay_tools(server_url, models, token)]
    return payload


def cassette_query(cassette_path):
    """The input the recorded run was given, so the replay asks the same thing."""
    with open(cassette_path, encoding="utf-8") as fh:
        data = json.load(fh)
    for key in ("query", "input", "prompt"):
        if data.get(key):
            return data[key]
    inter = data.get("interactions") or []
    raise SystemExit(
        f"{cassette_path} carries no recorded query "
        f"({len(inter)} interactions). make_cassette.py records the tool "
        "exchange; the agent input has to come from --query or from the "
        "replay set.")


# ---------------------------------------------------------------- reporting

def verdict(s, allow_divergence_after=None):
    """Did this replay reproduce the recorded run?

    Divergence is not automatically failure -- an agent change that removes a
    wasted call SHOULD diverge, and that is the improvement you wanted. What
    matters is where it diverged and whether it attempted writes the recorded
    run did not.
    """
    matched = s.get("matched_prefix", 0)
    total = s.get("recorded_interactions", 0)
    div = s.get("first_divergence")
    lines = [
        f"  recorded interactions : {total}",
        f"  matched prefix        : {matched}",
        f"  writes attempted      : {s.get('writes_attempted', 0)} "
        f"(none performed)",
    ]
    if div:
        lines.append(f"  first divergence      : seq {div.get('seq')} "
                     f"{str(div.get('tool','')).split('___')[-1]}")
    ok = True
    if allow_divergence_after is not None and matched < allow_divergence_after:
        lines.append(f"  FAIL: diverged before seq {allow_divergence_after}")
        ok = False
    return ok, "\n".join(lines)


def write_manifest(path, args, base_version, temp_version, s):
    """Record what was actually tested, because the agent version will not.

    Foundry evaluation objects are scoped to the PROJECT, not to an agent --
    `evals.create(name=...)` takes a dataset, not an agent id. So nothing an
    eval stores depends on the temporary version surviving, and deleting it
    loses no results.

    What does carry the temporary version's identity is the TRACE: App
    Insights records gen_ai.agent.id and its version for every span. Delete
    the version and that id resolves to nothing, so six weeks later a trace
    names an agent that cannot be looked up and nothing says what it was a
    clone of.

    This file is that record. Name any eval built from this run after
    `base_version` -- the thing under test -- rather than after the ephemeral
    clone, which is a fixture.
    """
    payload = {
        "agent": args.agent,
        "base_version": str(base_version),
        "temp_version": str(temp_version),
        "temp_version_deleted": True,
        "cassette": os.path.basename(args.cassette),
        "cassette_id": s.get("cassette"),
        "server_url": args.server_url,
        "tools": "stubbed — no ConnectWise request, no write performed",
        "replayed_utc": _dt.datetime.now(_dt.timezone.utc)
                           .replace(microsecond=0).isoformat(),
        "matched_prefix": s.get("matched_prefix"),
        "recorded_interactions": s.get("recorded_interactions"),
        "writes_attempted": s.get("writes_attempted"),
        "first_divergence": s.get("first_divergence"),
        "suggested_eval_name": f"replay-{args.agent}-v{base_version}",
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    return payload


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cassette", required=True)
    ap.add_argument("--agent", required=True, help="agent name under test")
    ap.add_argument("--agent-version", help="base version to clone; "
                                            "default is the latest")
    ap.add_argument("--server-url", help="where FOUNDRY reaches the replay "
                                         "server. Not localhost.")
    ap.add_argument("--serve", action="store_true",
                    help="run replay_server.py locally as a subprocess")
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--tool-defs", default="tool_manifests/")
    ap.add_argument("--token", default=os.environ.get("REPLAY_TOKEN"),
                    help="bearer token the hosted replay server requires. "
                         "Sent as a header, never in the URL.")
    ap.add_argument("--journal", default="artifacts/replay-journal.json")
    ap.add_argument("--manifest", default="artifacts/replay-run.json",
                    help="provenance for this replay. Written even on "
                         "failure -- see the note on the temporary version.")
    ap.add_argument("--query", help="override the agent input")
    ap.add_argument("--min-matched-prefix", type=int,
                    help="fail if the replay diverges before this call")
    ap.add_argument("--project-endpoint",
                    default=os.environ.get("AZURE_AI_PROJECT_ENDPOINT"))
    ap.add_argument("--allow-local", action="store_true",
                    help="skip the reachability check (for testing this "
                         "script, not for a real replay)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the binding that would be created and exit")
    args = ap.parse_args(argv)

    proc = None
    if args.serve:
        proc, base = serve(args.cassette, args.port, args.tool_defs,
                           args.journal)
        print(f"replay server on {base}")
        server_url = args.server_url or base
    else:
        server_url = args.server_url
        if not server_url:
            sys.exit("--server-url is required unless --serve is given")

    try:
        if is_locally_scoped(server_url) and not args.allow_local:
            sys.exit(
                f"{server_url} is not reachable from Azure.\n\n"
                "Foundry calls the replay server; the server does not call "
                "Foundry. A localhost URL becomes a tool call that times out "
                "inside the agent run, which looks like an agent failure "
                "rather than a configuration mistake. Host the server, or "
                "put a tunnel in front of it, and pass the public URL as "
                "--server-url. See docs/REPLAY.md.")

        query = args.query or cassette_query(args.cassette)

        if args.dry_run:
            print(json.dumps({
                "agent": args.agent,
                "base_version": args.agent_version or "latest",
                "server_url": server_url,
                "tools_replaced_with": [
                    {"type": "mcp", "server_label": REPLAY_TOOL_LABEL,
                     "server_url": server_url, "require_approval": "never",
                     "headers": ["Authorization"] if args.token else []}],
                "summary_url": summary_url(server_url),
                "query": query,
                "temp_version_metadata": {"purpose": TEMP_MARKER},
            }, indent=1))
            print("\ndry run: nothing was created in the project.")
            return 0

        if not args.project_endpoint:
            sys.exit("--project-endpoint or AZURE_AI_PROJECT_ENDPOINT "
                     "is required")

        from azure.ai.projects import AIProjectClient, models
        from azure.identity import DefaultAzureCredential

        client = AIProjectClient(endpoint=args.project_endpoint,
                                 credential=DefaultAzureCredential())
        agents = client.agents

        base_version = (agents.get_version(args.agent, args.agent_version)
                        if args.agent_version else agents.get(args.agent))
        definition = cloned_definition(base_version, server_url, models,
                                       args.token)

        base_version = (getattr(base_version, "version", None)
                        or args.agent_version or "latest")
        temp = agents.create_version(
            agent_name=args.agent,
            definition=definition,
            description="temporary: stubbed-tool replay",
            metadata={"purpose": TEMP_MARKER,
                      "base_version": str(base_version),
                      "cassette": os.path.basename(args.cassette)})
        temp_version = getattr(temp, "version", None) or getattr(temp, "id", None)
        print(f"created temporary version {temp_version} "
              f"(clone of {base_version})")

        try:
            session = agents.create_session(
                agent_name=args.agent,
                version_indicator=models.VersionIndicator(version=temp_version))
            print(f"session {getattr(session, 'id', '?')} — query: {str(query)[:80]}")
            print("\nNOTE: driving the session to completion is the one step "
                  "this script cannot verify offline. If the SDK surface "
                  "differs, the session object above is what to drive.")
        finally:
            agents.delete_version(args.agent, temp_version)
            print(f"deleted temporary version {temp_version}")

        s = summary(server_url if not args.serve else base, args.token)
        write_manifest(args.manifest, args, base_version, temp_version, s)
        ok, text = verdict(s, args.min_matched_prefix)
        print("\nREPLAY")
        print(text)
        print(f"\nprovenance -> {args.manifest}")
        return 0 if ok else 1

    finally:
        if proc:
            proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
