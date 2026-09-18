#!/usr/bin/env python3
"""
replay_server.py — an MCP server that answers from a recorded cassette.

Reference implementation of the stub layer. The agent sees tools with the same
names and the same schemas as production; every call is answered from what the
recorded run returned. No ConnectWise request is made and **no write is ever
performed** — a write returns the response the real write returned.

    python3 make_cassette.py traces/2026-09-15-ops-worst-case.csv -o cassettes/
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
import argparse, glob, json, os, sys, threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from make_cassette import canonical_args, interaction_key
from trace_to_eval import base_tool_name, load_tool_manifests

PROTOCOL_VERSION = "2025-06-18"


class Cassette:
    """Ordered, keyed playback with a journal of what actually happened."""

    def __init__(self, data, on_exhausted="repeat"):
        self.data = data
        self.on_exhausted = on_exhausted
        self.queues = defaultdict(list)
        for i in data["interactions"]:
            self.queues[i["key"]].append(i)
        self.cursor = defaultdict(int)
        self.journal = []
        self.lock = threading.Lock()

    def tools(self):
        names = []
        for i in self.data["interactions"]:
            bare = base_tool_name(i["tool"])
            if bare not in names:
                names.append(bare)
        return names

    def call(self, tool, arguments):
        key = f"{base_tool_name(tool)}|{canonical_args(json.dumps(arguments))}"
        with self.lock:
            queue = self.queues.get(key)
            seq = len(self.journal)
            if not queue:
                entry = {"seq": seq, "tool": tool, "outcome": "diverged",
                         "key": key}
                self.journal.append(entry)
                return None, entry

            idx = self.cursor[key]
            if idx < len(queue):
                self.cursor[key] += 1
                rec, outcome = queue[idx], "matched"
            elif self.on_exhausted == "repeat":
                rec, outcome = queue[-1], "repeated"
            else:
                entry = {"seq": seq, "tool": tool, "outcome": "diverged",
                         "key": key, "reason": "responses exhausted"}
                self.journal.append(entry)
                return None, entry

            entry = {"seq": seq, "tool": tool, "outcome": outcome, "key": key,
                     "recorded_seq": rec["seq"], "is_write": rec["is_write"],
                     "truncated": rec["truncated"]}
            self.journal.append(entry)
            return rec, entry

    def summary(self):
        counts = defaultdict(int)
        for e in self.journal:
            counts[e["outcome"]] += 1
        first_divergence = next(
            (e for e in self.journal if e["outcome"] == "diverged"), None)
        return {
            "cassette": self.data["orchestration_id"],
            "recorded_interactions": len(self.data["interactions"]),
            "replayed_calls": len(self.journal),
            "matched": counts["matched"],
            "repeated": counts["repeated"],
            "diverged": counts["diverged"],
            # How far the agent followed the recorded path before doing
            # something the recording cannot answer.
            "matched_prefix": _prefix_len(self.journal),
            "first_divergence": first_divergence,
            "writes_attempted": sum(1 for e in self.journal
                                    if e.get("is_write")),
            "lossy_cassette": self.data.get("lossy", False),
            "journal": self.journal,
        }


def _prefix_len(journal):
    n = 0
    for e in journal:
        if e["outcome"] == "diverged":
            break
        n += 1
    return n


def tool_definitions(cassette, manifests):
    """Advertise production schemas where we have them.

    Without a manifest the tools are advertised with an empty schema, which
    changes what the agent is told it may send — so the replay is no longer
    a faithful stand-in. Fill tool_manifests/ before trusting a gate built on
    this. See tool_manifests/README.md.
    """
    by_name = {}
    for m in manifests:
        for t in m["tools"]:
            if t.get("parameters"):
                by_name[base_tool_name(t.get("name", ""))] = t

    out, missing = [], []
    for bare in cassette.tools():
        known = by_name.get(bare)
        if known:
            out.append({"name": bare,
                        "description": known.get("description", ""),
                        "inputSchema": known["parameters"]})
        else:
            missing.append(bare)
            out.append({"name": bare, "description": "",
                        "inputSchema": {"type": "object", "properties": {}}})
    return out, missing


def _result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


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
            return self._send({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32700,
                                         "message": "parse error"}})

        method, rid = req.get("method"), req.get("id")
        if method == "notifications/initialized":
            return self._send({})

        if method == "initialize":
            return self._send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "connectwise-replay", "version": "1"},
            }})

        if method == "tools/list":
            return self._send({"jsonrpc": "2.0", "id": rid,
                               "result": {"tools": self.tools}})

        if method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name", "")
            rec, entry = self.cassette.call(name, params.get("arguments") or {})
            if rec is None:
                return self._send({"jsonrpc": "2.0", "id": rid, "result": _result(
                    json.dumps({
                        "error": "not_recorded",
                        "tool": name,
                        "message": "This call was not made in the recorded "
                                   "run, so there is no recorded response. "
                                   "The replay diverged here.",
                    }), is_error=True)})
            # A write returns what the real write returned. Nothing is written.
            return self._send({"jsonrpc": "2.0", "id": rid,
                               "result": _result(rec["result"],
                                                 is_error=not rec["success"])})

        self._send({"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"no method {method}"}})


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cassette")
    ap.add_argument("--tool-defs", action="append", metavar="PATH",
                    help="tool manifest or directory, so the replayed tools "
                         "advertise production schemas")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8931)
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
