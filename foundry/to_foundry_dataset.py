#!/usr/bin/env python3
"""
to_foundry_dataset.py — a trace export -> a Foundry evaluation dataset.

    python3 to_foundry_dataset.py traces/2026-09-03-full-triage.json \\
        --expected expected.json -o artifacts/foundry-dataset.jsonl

Why we build the dataset instead of using `azure_ai_traces`
-----------------------------------------------------------
Foundry's trace-sourced evaluation reads only spans where
`gen_ai.operation.name == invoke_agent`. On these traces those spans carry
`tool_call` content items but **no `tool_result`** — every tool result lives on
an `execute_tool` span, which that path discards. Four of the eight checks read
results, so they cannot run there at all.

Building the dataset ourselves sidesteps that: the converter already reads
`execute_tool` spans, so the results are in hand. It also gives us the
per-agent decomposition Foundry does not do — `TracesDataGenerationJobSource`
and `azure_ai_traces` are both scoped to a single agent identity, and
Microsoft's guidance is to evaluate the orchestrator rather than fan out.

Row shape
---------
Standard columns, so the built-in judged evaluators work:

    messages          the conversation, with `tool_call` and `tool_result`
                      typed content items
    tool_definitions  from telemetry and/or tool_manifests/

Plus columns the registered custom evaluators read:

    tool_outcomes     one entry per call: tool, arguments, result, success.
                      `messages` alone cannot express "succeeded and returned
                      nothing" versus "failed and returned nothing", and
                      that distinction is the whole of `empty_failed`.
    expected_actions  ground truth for the trajectory evaluator
    usage, duration_ms          for cost and latency
    orchestration_id, run_agent, intent, traj_key    to trace a row back
"""

from __future__ import annotations

# This script lives in a subdirectory but imports the converter and scorer
# from the repo root, so put the root on sys.path before those imports. Keeps
# `python3 foundry/to_foundry_dataset.py` working from anywhere, with no package
# conversion and no editable install. REPO_ROOT is also how sibling
# directories such as foundry_evaluators/ are located.
import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import argparse, json, os, sys
from collections import defaultdict

from trace_to_eval import (learn_agents, K_TOOL_RES, convert, is_tool_span, load_spans,
                           load_tool_manifests, tool_step)

# Keep in step with foundry_evaluators/_shared.RESULT_HEAD.
RESULT_HEAD = 600


def _maybe_json(raw):
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}


def steps_by_run(spans):
    """(op_id, agent) -> tool steps, in one pass.

    Keeps the raw results the run row deliberately drops. Grouping once
    rather than rescanning every span per run: the callers iterate runs, and
    a rescan made the build O(runs x spans) over data the same function has
    already walked.
    """
    from trace_to_eval import K_AGENT
    # Once, not per span: the docstring above is about exactly this class of
    # rescan, and putting it in the loop would have made it O(spans^2).
    agents = learn_agents(spans)
    grouped = defaultdict(list)
    for s in spans:
        if is_tool_span(s):
            grouped[(s["op_id"], s["d"].get(K_AGENT))].append(
                tool_step(s, agents))
    for steps in grouped.values():
        steps.sort(key=lambda st: st["timestamp"])
    return grouped


def steps_for(spans, op_id, agent):
    """One run's tool steps. Prefer steps_by_run when iterating runs."""
    return steps_by_run(spans).get((op_id, agent), [])


def flatten_text(value):
    """Foundry telemetry nests text under `parts`/`content`; the evaluation
    dataset schema wants a plain string for a text message."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("type") in (None, "text"):
            return flatten_text(value.get("content") or value.get("text"))
        return ""
    if isinstance(value, list):
        return "\n".join(t for t in (flatten_text(v) for v in value) if t)
    return ""


def final_answer(response):
    """The agent's last text message.

    Everything else in `response` is the same tool_call / tool_call_response
    sequence already emitted from the spans, in telemetry's `parts` vocabulary
    rather than the dataset's `content` one. Appending it raw both duplicated
    the trajectory and failed validation with "Message at index 14
    (role='assistant') is missing 'content'" on every one.
    """
    if isinstance(response, str):
        return response
    if not isinstance(response, list):
        return ""
    texts = []
    for m in response:
        if not isinstance(m, dict) or m.get("role") == "tool":
            continue
        texts.append(flatten_text(m.get("content") or m.get("parts")))
    return next((t for t in reversed(texts) if t.strip()), "")


def build_messages(run, steps):
    """Standard Foundry message array: system, user, call/result pairs, then
    the final answer. Text messages are plain strings; tool messages use the
    typed `tool_call` / `tool_result` content items."""
    messages = []
    for m in run.get("query") or []:
        text = flatten_text(m.get("content") or m.get("parts"))
        if text:
            messages.append({"role": m.get("role", "user"), "content": text})

    for i, st in enumerate(steps):
        call_id = st.get("call_id") or f"call_{i}"
        messages.append({
            "role": "assistant",
            "content": [{"type": "tool_call", "tool_call_id": call_id,
                         "name": st["tool"],
                         "arguments": _maybe_json(st["arguments"])}],
        })
        messages.append({
            "role": "tool", "tool_call_id": call_id,
            "content": [{"type": "tool_result",
                         "tool_result": _maybe_json(st["result"])}],
        })

    answer = final_answer(run.get("response"))
    if answer:
        messages.append({"role": "assistant", "content": answer})
    return messages


def build_rows(spans, manifests, expected, budgets=None):
    runs, _, _ = convert(spans, manifests)
    budgets = budgets or {}
    grouped = steps_by_run(spans)
    rows = []
    for run in runs:
        steps = grouped.get(
            (run["orchestration_id"], run["run_agent"]), [])
        key = run.get("traj_key") or run["run_agent"]
        rows.append({
            "orchestration_id": run["orchestration_id"],
            "run_agent": run["run_agent"],
            # "" rather than null: a nullable column makes the item schema a
            # type union, and traj_key already carries the intent.
            "intent": run.get("intent") or "",
            "traj_key": key,
            "messages": build_messages(run, steps),
            "tool_definitions": run.get("tool_definitions") or [],
            # `messages` cannot distinguish "succeeded and returned nothing"
            # from "failed and returned nothing"; `empty_failed` is exactly
            # that distinction, so the span status rides along here.
            "tool_outcomes": [{
                "tool": st["tool"],
                "arguments": _maybe_json(st["arguments"]),
                # The head plus the original length is everything the checks
                # read: classification and emptiness look at the first 600
                # characters, truncation looks at the length. Carrying the
                # whole body duplicated what `messages` already holds and put
                # one row at 1.1 MB, which the evals service 500s on.
                "result_head": st["result"][:RESULT_HEAD],
                "result_len": st["result_len"],
                "success": st["success"],
            } for st in steps],
            "expected_actions": (expected.get(key)
                                 or expected.get(run["run_agent"]) or []),
            # Flattened, not nested. The evals datasource validator rejects
            # a non-string value inside a nested object whatever the declared
            # schema says — `usage.uncached_input_tokens` = 35756 failed with
            # "not of type 'string'" against a schema declaring it an
            # integer. Probed: top-level scalars pass, arrays shield their
            # contents, nested objects do not. See diagnose_schema.py.
            **{(k if k.startswith("usage") else f"usage_{k}"): v
               for k, v in (run.get("usage") or {}).items()},
            "duration_ms": run.get("duration_ms", 0),
            **budgets,
        })
    return rows


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spans", help="trace export (JSON or CSV)")
    ap.add_argument("-o", "--out", default="artifacts/foundry-dataset.jsonl")
    ap.add_argument("--expected", help="ground-truth trajectories")
    ap.add_argument("--tool-defs", action="append", metavar="PATH")
    ap.add_argument("--max-tokens", type=int,
                    help="budget for the cost_latency evaluator")
    ap.add_argument("--max-duration-ms", type=int)
    ap.add_argument("--no-messages", action="store_true",
                    help="drop the `messages` column. The registered custom "
                         "evaluators read tool_outcomes and do not need it; "
                         "only the built-in judged evaluators do. Cuts the "
                         "dataset by roughly 10x.")
    args = ap.parse_args()

    expected = {}
    if args.expected:
        expected = {k: v for k, v in
                    json.load(open(args.expected, encoding="utf-8")).items()
                    if not k.startswith("_")}

    budgets = {}
    if args.max_tokens:
        budgets["max_tokens"] = args.max_tokens
    if args.max_duration_ms:
        budgets["max_duration_ms"] = args.max_duration_ms

    rows = build_rows(load_spans(args.spans),
                      load_tool_manifests(args.tool_defs), expected, budgets)
    if args.no_messages:
        for row in rows:
            row.pop("messages", None)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # The datasource validator rejects any non-string value inside a nested
    # object, whatever the declared schema says. Arrays are fine — they are
    # not descended into. See diagnose_schema.py.
    nested = sorted({k for r in rows for k, v in r.items()
                     if isinstance(v, dict)})
    if nested:
        print(f"REFUSING: {len(nested)} nested object column(s) — the evals "
              "datasource validator rejects a non-string value inside one "
              "regardless of the declared schema. Flatten them to top-level "
              f"scalars: {', '.join(nested)}")
        return 1

    invalid = []
    for n, row in enumerate(rows):
        for i, m in enumerate(row.get("messages") or []):
            content = m.get("content")
            if content is None or content == "" or "parts" in m:
                invalid.append(f"row {n} message {i} (role={m.get('role')})")
    if invalid:
        print(f"REFUSING: {len(invalid)} message(s) would fail the service's "
              "validation — every message needs a non-empty `content` and no "
              "`parts`:")
        for entry in invalid[:10]:
            print(f"  {entry}")
        return 1

    # One row over a megabyte has 500'd the evals service. Say so before the
    # upload rather than after.
    biggest = max(((len(json.dumps(r, ensure_ascii=False)), r["run_agent"])
                   for r in rows), default=(0, ""))
    if biggest[0] > 1_000_000:
        print(f"WARNING largest row is {biggest[0] / 1e6:.1f} MB "
              f"({biggest[1]}). The evals service has returned a 500 on rows "
              "this size — try --no-messages if you are not running the "
              "built-in judged evaluators.\n")

    scored = sum(1 for r in rows if r["expected_actions"])
    # A traj_key carrying a pseudonym means a scrub swept an intent name, so
    # expected.json silently stops matching. Say so rather than report 3/7.
    mangled = [r["traj_key"] for r in rows
               if any(t in (r["traj_key"] or "")
                      for t in ("NAME?_", "PERSON_", "VALUE_", "COMPANY_"))]
    if mangled:
        print("WARNING: a redaction token appears in these traj_keys, so an "
              "intent name was scrubbed and expected.json cannot match:")
        for key in sorted(set(mangled)):
            print(f"  {key}")
        print("  Remove the intent from the redaction list and re-scrub.\n")
    with_defs = sum(1 for r in rows if r["tool_definitions"])
    calls = sum(len(r["tool_outcomes"]) for r in rows)
    print(f"{len(rows)} row(s), {calls} tool call(s) -> {args.out}")
    print(f"  with expected_actions : {scored}/{len(rows)}")
    print(f"  with tool_definitions : {with_defs}/{len(rows)}"
          + ("  (cw_valid_tool_args scores 1.0 without one)"
             if with_defs < len(rows) else ""))
    return 0


if __name__ == "__main__":
    from evalconfig import public_main
    sys.exit(public_main(main))
