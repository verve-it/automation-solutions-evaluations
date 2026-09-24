#!/usr/bin/env python3
"""
replay_server.py — an MCP server that answers from a recorded cassette.

Reference implementation of the stub layer. The agent sees tools with the same
names and the same schemas as production; every call is answered from what the
recorded run returned. No ConnectWise request is made and **no write is ever
performed** — a write returns the response the real write returned.

    python3 make_cassette.py traces/2026-09-15-ops-worst-case.json -o cassettes/
    python3 replay_server.py cassettes/2026-09-15-2c861b0dbf97.json \\
        --tool-defs tool_manifests/ --journal artifacts/replay-journal.json

Then point a Foundry toolbox at http://<host>:8931/mcp and bind the agent
under test to it. See docs/REPLAY.md for the wiring.

Divergence is the whole design
------------------------------
You replay precisely when the agent has changed, so calls that were never
recorded are the common case, not an edge case. Three outcomes per call:

  matched    the exact (tool, arguments) pair was recorded and has an
             unconsumed response. Return it.
  repeated   the pair was recorded but its responses are used up. Return the
             last one again under --on-exhausted repeat (the default, because
             a re-read is usually benign), or diverge under `diverge`.
  diverged   the pair was never recorded. Return a typed `not_recorded` error
             and journal it. NEVER fabricate a plausible answer: the agent
             would reason over a fiction and the result would be scored as
             real behaviour.

So a replayed run is scored on its **matched prefix** and its divergence point:
"did this change alter the trajectory, and where". That is the right question
for an agent-change gate. It is not the same question as "did the agent do the
task well", which still needs recorded production traces.
"""

from __future__ import annotations

# This script lives in a subdirectory but imports the converter and scorer
# from the repo root, so put the root on sys.path before those imports. Keeps
# `python3 replay/replay_server.py` working from anywhere, with no package
# conversion and no editable install. REPO_ROOT is also how sibling
# directories such as foundry_evaluators/ are located.
import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from trace_to_eval import load_tool_manifests

# The playback and the MCP dispatch live in mcp_core so that this server and
# the Azure Function in functions/replay-mcp/ answer identically. Re-exported
# because callers and tests have imported them from here since before the
# hosted version existed.
from mcp_core import (PROTOCOL_VERSION, Cassette, handle_rpc,  # noqa: F401
                      parse_error, prefix_len, tool_definitions, tool_result)

_result = tool_result


class Handler(BaseHTTPRequestHandler):
    cassette: Cassette = None
    tools: list = []
    token: str = None

    def log_message(self, fmt, *a):      # quieter than the default
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", "replay")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not self.token:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {self.token}"

    def do_GET(self):
        # `/` is a health check and says nothing but that the process is up,
        # so it stays open for readiness probes. `/summary` is the journal:
        # tool names, canonicalised arguments carrying ticket and company
        # identifiers, and every attempted write. docs/REPLAY.md tells you to
        # expose this server to Foundry, so it takes the same bearer check as
        # do_POST rather than none at all.
        if self.path.rstrip("/") == "/summary":
            if not self._authorized():
                return self._send({"error": "unauthorized"}, 401)
            return self._send(self.cassette.summary())
        self._send({"status": "ok", "mode": "replay"})

    def do_POST(self):
        if not self._authorized():
            return self._send({"error": "unauthorized"}, 401)

        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(parse_error())

        self._send(handle_rpc(req, self.cassette, self.tools))


def default_port():
    """8931 locally; whatever the Functions host assigned when hosted.

    A custom handler is told its port in FUNCTIONS_CUSTOMHANDLER_PORT and the
    host will not route to anything else, so honouring it costs one line and
    saves a deployment that comes up healthy and answers nothing.
    """
    return int(os.environ.get("FUNCTIONS_CUSTOMHANDLER_PORT") or 8931)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cassette")
    ap.add_argument("--tool-defs", action="append", metavar="PATH",
                    help="tool manifest or directory, so the replayed tools "
                         "advertise production schemas")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=default_port())
    ap.add_argument("--token", help="require this bearer token")
    ap.add_argument("--journal", help="write the replay journal here on exit")
    ap.add_argument("--on-exhausted", choices=("repeat", "diverge"),
                    default="repeat",
                    help="what to do when a recorded call is made more times "
                         "than it was recorded (default: repeat the last)")
    args = ap.parse_args()

    with open(args.cassette, encoding="utf-8") as fh:
        data = json.load(fh)
    cassette = Cassette(data, args.on_exhausted)
    manifests = load_tool_manifests(args.tool_defs)
    tools, missing = tool_definitions(cassette, manifests)

    Handler.cassette, Handler.tools, Handler.token = cassette, tools, args.token

    print(f"cassette   : {data['orchestration_id']} "
          f"({len(data['interactions'])} interactions, {data['writes']} write(s))")
    print(f"tools      : {len(tools)}")
    if data.get("lossy"):
        print("WARNING      cassette is LOSSY — it contains a truncated "
              "result. The agent will see less than the original did.")
    if missing:
        print(f"WARNING      no schema for {', '.join(missing)} — advertised "
              "with an empty schema, so this is not a faithful stand-in. "
              "Fill tool_manifests/.")
    print(f"listening  : http://{args.host}:{args.port}/mcp")
    print(f"summary    : http://{args.host}:{args.port}/summary")
    print("\nNo ConnectWise request is made and no write is performed.")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        summary = cassette.summary()
        print(f"\nreplayed {summary['replayed_calls']} call(s): "
              f"{summary['matched']} matched, {summary['repeated']} repeated, "
              f"{summary['diverged']} diverged "
              f"(matched prefix {summary['matched_prefix']})")
        if args.journal:
            os.makedirs(os.path.dirname(os.path.abspath(args.journal)),
                        exist_ok=True)
            with open(args.journal, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=1)
            print(f"wrote {args.journal}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
