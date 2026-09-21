#!/usr/bin/env python3
"""
mcp_core.py — cassette playback and MCP dispatch, with no transport attached.

`replay_server.py` serves this over stdlib HTTP for local runs;
`functions/replay-mcp/server.py` serves the same objects from an Azure
Function. They must answer identically or the gate means different things in
the two places it runs, so the decisions live here and the transports own
nothing but sockets.

Everything the stub guarantee rests on is in this file:

  * no ConnectWise request is made, for reads or writes
  * a write returns what the real write returned, and writes nothing
  * a call that was never recorded returns a typed `not_recorded` error
    rather than a fabricated answer

Replay state is separable on purpose
------------------------------------
`Cassette` holds the recording, which is immutable. The cursor into each
queue and the journal of what happened are *state*, and `dump_state()` /
`load_state()` make them a value. In-process that buys nothing. Hosted, it is
what lets a replay stay ordered when the platform is free to run the next
call on a different instance — see `state_store.py`.
"""

from __future__ import annotations

import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import json, threading
from collections import defaultdict

from make_cassette import canonical_args
from trace_to_eval import base_tool_name

# The MCP revision this server implements. A client that asks for a different
# one is told this one; MCP's negotiation allows that and the client decides
# whether it can proceed.
PROTOCOL_VERSION = "2025-06-18"

SERVER_NAME = "connectwise-replay"


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

    # ------------------------------------------------------------- state

    def dump_state(self):
        """The mutable half, as plain JSON-able data."""
        return {"cursor": dict(self.cursor), "journal": self.journal}

    def load_state(self, state):
        """Adopt state from a previous call of the same replay.

        Absent or malformed state is treated as a fresh replay rather than an
        error: the alternative is failing an agent run because a state blob
        was lost, which would read as an agent regression.
        """
        state = state or {}
        self.cursor = defaultdict(int, state.get("cursor") or {})
        self.journal = list(state.get("journal") or [])

    # ------------------------------------------------------------ replay

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
            "matched_prefix": prefix_len(self.journal),
            "first_divergence": first_divergence,
            "writes_attempted": sum(1 for e in self.journal
                                    if e.get("is_write")),
            "lossy_cassette": self.data.get("lossy", False),
            "journal": self.journal,
        }


def prefix_len(journal):
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


def tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


NOT_RECORDED_MESSAGE = ("This call was not made in the recorded run, so there "
                        "is no recorded response. The replay diverged here.")


def handle_rpc(req, cassette, tools):
    """One JSON-RPC request in, one response payload out.

    `{}` means "nothing to answer" (a notification). Both transports send it
    with a 200 and an empty body's worth of meaning.
    """
    method, rid = req.get("method"), req.get("id")

    if method == "notifications/initialized":
        return {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        }}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        rec, _entry = cassette.call(name, params.get("arguments") or {})
        if rec is None:
            # NEVER fabricate a plausible answer here. The agent would reason
            # over a fiction and the result would be scored as real behaviour.
            return {"jsonrpc": "2.0", "id": rid, "result": tool_result(
                json.dumps({"error": "not_recorded", "tool": name,
                            "message": NOT_RECORDED_MESSAGE}), is_error=True)}
        # A write returns what the real write returned. Nothing is written.
        return {"jsonrpc": "2.0", "id": rid,
                "result": tool_result(rec["result"], is_error=not rec["success"])}

    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"no method {method}"}}


def parse_error(rid=None):
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32700, "message": "parse error"}}
