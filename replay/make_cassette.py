#!/usr/bin/env python3
"""
make_cassette.py — turn a recorded trace into a replay cassette.

A cassette is every tool interaction of one orchestration, in order, with the
arguments that were sent and the result that came back. Served by
`replay_server.py`, it lets an agent be re-run without touching ConnectWise:
reads return what they returned, writes return what they returned *without
writing*, and nothing depends on data that has since changed.

    python3 make_cassette.py traces/2026-09-03-full-triage.json -o cassettes/

Why ordered and not a dictionary
--------------------------------
`cw_get_ticket {"ticket_number": 805392}` returns **five different results**
inside one recorded orchestration, because the agents mutate the ticket as
they go. Keyed by (tool, arguments) alone, all five collapse into one and the
agent never sees its own writes land. So each key holds a QUEUE, consumed in
recorded order.

What a cassette cannot do
-------------------------
It answers "given the same world, does the agent still behave the same way",
which is the right question for an agent-change gate. It cannot tell you that
a write still succeeds against a real ConnectWise, or that ConnectWise itself
has not changed. Keep a low-frequency live run for that.
"""

from __future__ import annotations

# This script lives in a subdirectory but imports the converter and scorer
# from the repo root, so put the root on sys.path before those imports. Keeps
# `python3 replay/make_cassette.py` working from anywhere, with no package
# conversion and no editable install. REPO_ROOT is also how sibling
# directories such as foundry_evaluators/ are located.
import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import argparse, json, os, sys
from collections import defaultdict

from trace_to_eval import (AGENT_NAMES, K_AGENT, base_tool_name, is_tool_span,
                           load_spans, tool_step)

CASSETTE_VERSION = 1

# Tools that change ConnectWise. Flagged so the replay server can assert the
# agent attempted the same writes while performing none of them.
WRITE_TOOLS = {"cw_create", "cw_update", "cw_update_ticket", "cw_delete",
               "cw_patch"}


def canonical_args(raw):
    """Stable key for matching a live call to a recorded one.

    Whitespace and key order must not decide whether a call matches, or an
    agent that serialises its arguments differently diverges on every call
    for no reason.
    """
    if not raw:
        return "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def interaction_key(tool, raw_args):
    return f"{base_tool_name(tool)}|{canonical_args(raw_args)}"


def build(spans):
    """One cassette per orchestration."""
    by_op = defaultdict(list)
    for s in spans:
        if is_tool_span(s):
            by_op[s["op_id"]].append(s)

    cassettes = []
    for op_id, op_spans in by_op.items():
        op_spans.sort(key=lambda s: s["timestamp"])
        interactions, warnings = [], []
        agents = []

        for seq, s in enumerate(op_spans):
            step = tool_step(s)
            agent = s["d"].get(K_AGENT, "")
            if agent and agent not in agents:
                agents.append(agent)
            if step["is_a2a"]:
                # An A2A hand-off is the caller invoking another agent, not an
                # MCP tool. The callee gets replayed as its own agent run, so
                # the toolbox must not answer for it.
                continue

            bare = base_tool_name(step["tool"])
            if step["truncated"]:
                warnings.append(
                    f"seq {seq}: {bare} result truncated at 8192 chars — "
                    "replaying it feeds the agent incomplete data")
            interactions.append({
                "seq": seq,
                "agent": agent,
                "tool": step["tool"],
                "key": interaction_key(step["tool"], step["arguments"]),
                "arguments": _maybe_json(step["arguments"]),
                "result": step["result"],
                "success": step["success"],
                "is_write": bare in WRITE_TOOLS,
                "truncated": step["truncated"],
                "error_kind": step["error_kind"],
                "duration_ms": step["duration_ms"],
            })

        if not interactions:
            continue
        cassettes.append({
            "cassette_version": CASSETTE_VERSION,
            "orchestration_id": op_id,
            "recorded": op_spans[0]["timestamp"],
            "agents": agents,
            "interactions": interactions,
            "writes": sum(1 for i in interactions if i["is_write"]),
            # A cassette with a truncated result is a corrupted fixture: the
            # agent under test would reason over less than the original did,
            # and the difference would be scored as its fault.
            "lossy": bool(warnings),
            "warnings": warnings,
        })
    cassettes.sort(key=lambda c: c["recorded"])
    return cassettes


def _maybe_json(raw):
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return raw


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spans", help="trace export (JSON or CSV)")
    ap.add_argument("-o", "--out", default="./cassettes",
                    help="output directory")
    ap.add_argument("--strict", action="store_true",
                    help="refuse to write a cassette containing a truncated "
                         "result. Use this for anything that gates.")
    args = ap.parse_args()

    cassettes = build(load_spans(args.spans))
    os.makedirs(args.out, exist_ok=True)

    written, skipped = 0, 0
    for c in cassettes:
        name = f"{c['recorded'][:10]}-{c['orchestration_id'][:12]}.json"
        path = os.path.join(args.out, name)
        flag = "LOSSY" if c["lossy"] else "ok   "
        detail = (f"{len(c['interactions']):>3} interactions, "
                  f"{c['writes']} write(s), {len(c['agents'])} agent(s)")
        if c["lossy"] and args.strict:
            print(f"  SKIP  {name}  {detail}")
            for w in c["warnings"]:
                print(f"          {w}")
            skipped += 1
            continue
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(c, fh, ensure_ascii=False, indent=1)
        print(f"  {flag} {name}  {detail}")
        for w in c["warnings"]:
            print(f"          WARNING {w}")
        written += 1

    print(f"\n{written} cassette(s) -> {args.out}")
    if skipped:
        print(f"{skipped} skipped as lossy (--strict)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
