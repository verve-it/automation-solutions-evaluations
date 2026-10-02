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
                           learn_agents, load_spans, load_tool_manifests,
                           tool_step)

CASSETTE_VERSION = 1

# A schema identifier, so a future reader can recognise this file without
# guessing. Microsoft ships no record/replay format for MCP; several
# independent projects do (mcp-replay, mcpcassette, mcp-cassette, Agent VCR)
# and have converged on JSONL with a schema id in a meta header. Ours is a
# single object with semantic interactions rather than raw JSON-RPC, for
# reasons in docs/MIGRATION-READINESS.md — but declaring what it is costs
# nothing and is what makes a converter possible later.
CASSETTE_SCHEMA = "verve/mcp-cassette@1"

# The OTel MCP semantic conventions are at Development stability and
# explicitly iterating fast, with no versioned release to pin against. Record
# which protocol version the recording saw, so a convention change is a diff
# rather than an archaeology exercise.
K_MCP_PROTO = "mcp.protocol.version"

# What the agent was asked. Recorded so a replay needs nothing but the
# cassette: the input is part of the recording, and asking a caller to retype
# it invites asking a slightly different question than the one recorded.
K_IN_MSGS = "gen_ai.input.messages"

# Which tools change the system under test is a fact about that system, not
# about replaying, so it comes from eval-config.json and from what the MCP
# server says about itself -- `annotations.readOnlyHint` and
# `destructiveHint`, which the spec defines for exactly this.
#
# It was a set written here, and it was wrong in both directions: it named
# cw_patch, which does not exist, and missed eight tools that plainly mutate.
# Nothing caught it because a missed write tool is counted as a read, and the
# gate then reports "0 writes, none performed" about a run that attempted
# several.
import evalconfig

CONFIG = evalconfig.load()


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


def mcp_protocol_version(spans):
    """{op_id: protocol version} from whichever span carries it.

    The attribute lives on `initialize` spans, not on tool calls, so this has
    to look at every span for the orchestration rather than the tool spans
    the cassette is built from.

    App Insights coerces the value: MCP protocol versions are plain dates
    like `2025-11-25`, and it stores `2025-11-25T00:00:00.0000000Z`. Trimmed
    back to the date, because that is what the version actually is and what a
    reader would compare against.
    """
    found = {}
    for s in spans:
        raw = s["d"].get(K_MCP_PROTO)
        if not raw or s["op_id"] in found:
            continue
        text = str(raw)
        if "T" in text and text[:10].count("-") == 2:
            text = text[:10]
        found[s["op_id"]] = text
    return found


# Enough rounds to undo a double encoding and stop. Not a loop until stable:
# text that keeps looking escaped after this is not an encoding artefact, it
# is content, and rewriting it would change the question.
MAX_UNESCAPE_ROUNDS = 3


def repair_escaping(text):
    r"""Undo App Insights double-encoding a message, if that is what happened.

    Within ONE export, one orchestration's input arrives with real newlines
    (0x0a) and another's with the two characters `\` and `n` -- in the case
    that prompted this, with two backslashes, having been encoded twice. The
    exporter is inconsistent, not the agent.

    It matters because the query is what the replay asks. Replaying
    `entityType=ticket\\nentityId=805392` when production was given a real
    newline asks a different question, and the gate scores the difference as
    the agent's.

    The condition is narrow on purpose: text that already contains a newline
    is left alone, so a query that legitimately mentions a backslash-n is only
    touched when it has no real line breaks at all. Returns the rounds applied
    so the caller can say so rather than fixing it quietly.
    """
    rounds = 0
    while ("\n" not in text and rounds < MAX_UNESCAPE_ROUNDS
           and "\\n" in text):
        # Longest first: two backslashes then n is one encoding layer, and
        # stripping the short form first would leave a stray backslash.
        if "\\\\n" in text:
            text = (text.replace("\\\\r\\\\n", "\n")
                        .replace("\\\\n", "\n")
                        .replace("\\\\t", "\t"))
        else:
            text = (text.replace("\\r\\n", "\n")
                        .replace("\\n", "\n")
                        .replace("\\t", "\t"))
        rounds += 1
    return text, rounds


def recorded_query(spans):
    """{op_id: the text the entry agent was given}.

    The input is on the `invoke_agent` span, not on any tool call, so this
    walks every span for the orchestration. The earliest one is the entry
    agent -- a child agent's invoke carries the hand-off, not the request.

    Only the `user` part is taken. System instructions are the agent's, not
    the run's, and replaying them as input would ask a different question.
    """
    found = {}
    for s in sorted(spans, key=lambda s: s["timestamp"]):
        raw = s["d"].get(K_IN_MSGS)
        if not raw or s["op_id"] in found:
            continue
        try:
            messages = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for message in messages if isinstance(messages, list) else []:
            if (message or {}).get("role") != "user":
                continue
            text = "".join(
                part.get("content") or ""
                for part in (message.get("parts") or [])
                if part.get("type") == "text")
            if text.strip():
                repaired, rounds = repair_escaping(text)
                found[s["op_id"]] = (repaired, rounds)
                break
    return found


def build(spans, manifests=()):
    """One cassette per orchestration."""
    agents_seen = learn_agents(spans)
    protocols = mcp_protocol_version(spans)
    queries = recorded_query(spans)
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
            step = tool_step(s, agents_seen)
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
                "is_write": evalconfig.is_write_tool(bare, CONFIG,
                                                    manifests),
                "truncated": step["truncated"],
                "error_kind": step["error_kind"],
                "duration_ms": step["duration_ms"],
            })

        if not interactions:
            continue
        cassettes.append({
            "schema": CASSETTE_SCHEMA,
            "cassette_version": CASSETTE_VERSION,
            "mcp_protocol_version": protocols.get(op_id),
            "query": (queries.get(op_id) or (None, 0))[0],
            # Recorded rather than fixed quietly: the query is what the replay
            # asks, so a change to it belongs in the file and in the output.
            "query_unescaped_rounds": (queries.get(op_id) or (None, 0))[1],
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
    ap.add_argument("--tool-defs", action="append", metavar="PATH",
                    help="tool manifest or directory. Its readOnlyHint / "
                         "destructiveHint annotations decide which calls are "
                         "writes, ahead of eval-config.json.")
    ap.add_argument("--strict", action="store_true",
                    help="refuse to write a cassette containing a truncated "
                         "result. Use this for anything that gates.")
    args = ap.parse_args()

    cassettes = build(load_spans(args.spans),
                      load_tool_manifests(args.tool_defs))
    os.makedirs(args.out, exist_ok=True)

    written, skipped = 0, 0
    for c in cassettes:
        name = f"{c['recorded'][:10]}-{c['orchestration_id'][:12]}.json"
        path = os.path.join(args.out, name)
        # `lossy` means a truncated RESULT -- a corrupted fixture the agent
        # would reason over. An unescaped query is neither corrupt nor a
        # reason to refuse the cassette, so it is reported on its own line.
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
        if c.get("query_unescaped_rounds"):
            print(f"          NOTE  the exporter escaped the recorded input "
                  f"{c['query_unescaped_rounds']} time(s) over; unescaped, so "
                  "the replay asks what the agent was given")
        for w in c["warnings"]:
            print(f"          WARNING {w}")
        written += 1

    print(f"\n{written} cassette(s) -> {args.out}")
    if skipped:
        print(f"{skipped} skipped as lossy (--strict)")
        return 1
    return 0


if __name__ == "__main__":
    from evalconfig import public_main
    sys.exit(public_main(main))
