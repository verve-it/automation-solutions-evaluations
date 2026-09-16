#!/usr/bin/env python3
"""
trace_to_eval.py — convert Foundry agent traces (App Insights export) into
Foundry-evaluator-ready JSONL, one row per AI Run (one per agent).

INPUT   JSON or CSV exported from App Insights `dependencies` (or
        `AppDependencies`). Export the raw rows, not a projection:
            dependencies
            | where operation_Id in ("...","...")
            | project timestamp, name, id, operation_Id, operation_ParentId,
                      duration, success, customDimensions

OUTPUT  <out>/eval_runs.jsonl   one row per agent run
        <out>/runs_summary.csv  flat index for sizing / triage

MODEL   operation_Id            -> AI Orchestration
        distinct gen_ai.agent.name -> AI Run (one each, orchestrator included)
        execute_tool spans      -> that run's tool trajectory
        execute_tool <agentname>-> A2A link: kept as a step in the CALLER's
                                   trajectory, and the callee is its own run

Run:  python3 trace_to_eval.py spans.json -o ./out
"""

from __future__ import annotations
import argparse, csv, json, os, re, sys
from collections import defaultdict

# Agents that are run boundaries. A tool call whose name matches one of these
# is an A2A hand-off, not an ordinary tool. Extend as you add agents.
AGENT_NAMES = {
    "triage-orchestrator",
    "triage-analysis-agent",
    "connectwise-operations-agent",
    "triage-evaluation-agent",
}

# gen_ai attribute keys
K_AGENT      = "gen_ai.agent.name"
K_AGENT_VER  = "gen_ai.agent.version"
K_AGENT_ID   = "gen_ai.agent.id"
K_OP         = "gen_ai.operation.name"
K_TOOL       = "gen_ai.tool.name"
K_TOOL_ID    = "gen_ai.tool.call.id"
K_TOOL_ARGS  = "gen_ai.tool.call.arguments"
K_TOOL_RES   = "gen_ai.tool.call.result"
K_TOOL_DEFS  = "gen_ai.tool.definitions"
K_SYS        = "gen_ai.system_instructions"
K_IN_MSGS    = "gen_ai.input.messages"
K_OUT_MSGS   = "gen_ai.output.messages"
K_REQ_MODEL  = "gen_ai.request.model"
K_RES_MODEL  = "gen_ai.response.model"
K_CONV       = "gen_ai.conversation.id"
K_IN_TOK     = "gen_ai.usage.input_tokens"
K_OUT_TOK    = "gen_ai.usage.output_tokens"

TRUNC_BOUNDARY = 8192   # values landing exactly here are suspect

# Failure signatures observed in real ConnectWise MCP / skill responses.
# Order matters: first match wins.
ERROR_PATTERNS = [
    ("invalid_entity",           "not found in registry"),
    ("invalid_reference_type",   "Unknown reference type"),
    ("invalid_projection_field", "not found on service"),
    ("missing_script",           "Error: Script"),
    ("circuit_open",             "circuit open"),
    ("validation_error",         "validation_error"),
    ("rate_limited",             "rate_limited"),
    ("quota_exceeded",           "quota_exceeded"),
]

# A call that succeeded but returned nothing.
EMPTY_MARKERS = ('"count":0', '"count_hint":0', '"matches":[]', '"data":[]',
                 '"count": 0', '"count_hint": 0', '"matches": []', '"data": []')


def classify_error(result, span_ok=True):
    """Return an error kind, or None if the call looks healthy.

    An empty result on a FAILED span is a real failure: cw_resolve returns
    nothing at all when asked for a reference type it does not support
    (type / subtype / item / site). Span status is the only signal there.
    """
    if not result:
        return None if span_ok else "empty_failed"
    head = result[:600]
    for kind, sig in ERROR_PATTERNS:
        if sig in head:
            return kind
    if head.startswith("Error:") or '"error"' in head:
        return "other_error"
    return None


def is_empty_result(result):
    if not result:
        return False
    return any(m in result[:600] for m in EMPTY_MARKERS)


# ----------------------------------------------------------------- loading

def _as_dict(v):
    """customDimensions arrives as dict (JSON export) or JSON string (CSV)."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {}
    return {}


def _col(row, *names):
    """Tolerate App Insights vs Log Analytics column naming."""
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


def load_spans(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        head = fh.read(1)
        fh.seek(0)
        rows = json.load(fh) if head in "[{" else list(csv.DictReader(fh))
    if isinstance(rows, dict):                 # {"tables":[...]} or single row
        rows = rows.get("value") or rows.get("rows") or [rows]

    spans = []
    for r in rows:
        d = _as_dict(_col(r, "customDimensions", "CustomDimensions", "dims"))
        spans.append({
            "timestamp": _col(r, "timestamp", "TimeGenerated") or "",
            "name":      _col(r, "name", "Name") or "",
            "id":        _col(r, "id", "Id") or "",
            "op_id":     _col(r, "operation_Id", "OperationId") or "",
            "parent":    _col(r, "operation_ParentId", "ParentId") or "",
            "duration":  float(_col(r, "duration", "DurationMs") or 0),
            "success":   str(_col(r, "success", "Success")).lower() != "false",
            "d":         d,
        })
    return spans


def _json_or_raw(s):
    """Message/definition attrs are JSON strings; keep raw if unparseable."""
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s


# ----------------------------------------------------------------- shaping

def is_tool_span(s):
    """
    Every MCP call emits BOTH `execute_tool <x>` (parent, carries args/result)
    and `tools/call <x>` (child, empty). Both set operation.name=execute_tool,
    so filter on the span NAME or you double-count the trajectory.
    """
    return s["name"].startswith("execute_tool")


# Intents declared by the orchestrator contract. Used to normalise casing and
# to tell a declared intent from an arbitrary string that happens to match.
SUPPORTED_INTENTS = [
    "Full Triage", "Normalization Only", "Classification Only", "Enrichment",
    "Resolved Ticket Review", "Information Request", "Write Request",
    "Explanation", "Detail Expansion",
]
_INTENT_RE = re.compile(r"intent\s*:\s*([^\n\r\"\\]+)", re.IGNORECASE)


def _flatten_text(obj, out=None):
    """Pull every text-ish leaf out of a message structure, whatever its
    shape (OpenAI `content`, Foundry `parts`, plain strings)."""
    if out is None:
        out = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("content", "text", "parts", "messages", "request"):
                _flatten_text(v, out)
            elif isinstance(v, (dict, list)):
                _flatten_text(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _flatten_text(v, out)
    return out


def _normalise_intent(raw):
    r = raw.strip().strip('.,;')
    for known in SUPPORTED_INTENTS:
        if r.lower() == known.lower():
            return known
    return r or None


def extract_intent(run_dims, steps):
    """
    Resolve what this run was asked to do.

    Two sources, in order:
      declared  - the run's own input carries `intent: <x>` (child agents get
                  this from the orchestrator's structured hand-off)
      delegated - the run has no declared intent but issued an A2A call whose
                  arguments declare one; that is the orchestrator's case, since
                  its own input is free text like "Automated flow: triage 805392"

    Returns (intent, source). Intent is None when neither applies, which keeps
    intent-keyed expectations from matching a run whose intent we guessed.
    """
    blob = " ".join(_flatten_text(_json_or_raw(run_dims.get(K_IN_MSGS))))
    m = _INTENT_RE.search(blob)
    if m:
        return _normalise_intent(m.group(1)), "declared"

    for st in steps:
        if not st.get("is_a2a"):
            continue
        m = _INTENT_RE.search(st.get("arguments", "") or "")
        if m:
            return _normalise_intent(m.group(1)), "delegated"

    return None, "unknown"


def unwrap_call_tool(name, args):
    """
    connectwise-operations-agent dispatches through a generic `call_tool`
    wrapper: {"name": "<real tool>", "arguments": {...}}. Without unwrapping,
    its whole trajectory reads as `call_tool` and no evaluator can see which
    ConnectWise operation ran.
    """
    if name != "call_tool" or not args:
        return name, args, False
    try:
        outer = json.loads(args)
    except json.JSONDecodeError:
        return name, args, False
    inner_name = outer.get("name")
    if not inner_name:
        return name, args, False
    inner_args = outer.get("arguments", {})
    if not isinstance(inner_args, str):
        inner_args = json.dumps(inner_args, ensure_ascii=False)
    return inner_name, inner_args, True


def tool_step(s):
    d = s["d"]
    args = d.get(K_TOOL_ARGS, "") or ""
    res = d.get(K_TOOL_RES, "") or ""
    name = d.get(K_TOOL, "") or s["name"].replace("execute_tool ", "", 1)
    name, args, unwrapped = unwrap_call_tool(name, args)
    kind = classify_error(res, s["success"])
    return {
        "unwrapped": unwrapped,
        "timestamp": s["timestamp"],
        "tool": name,
        "call_id": d.get(K_TOOL_ID, ""),
        "arguments": args,            # JSON *string* — correct for `actions`
        "result": res,
        "result_len": len(res),
        "truncated": len(res) == TRUNC_BOUNDARY,
        "error_kind": kind,
        "empty": is_empty_result(res),
        "errored": kind is not None,
        "is_a2a": name in AGENT_NAMES,
        "success": s["success"],
        "duration_ms": s["duration"],
    }


def pick_run_span(spans):
    """
    The agent's own LLM loop carries the full messages. Prefer an invoke_agent
    span with input messages and the richest output; fall back to any span
    that has messages at all.
    """
    cands = [s for s in spans if s["d"].get(K_IN_MSGS)]
    if not cands:
        return None
    invoke = [s for s in cands if s["name"].startswith("invoke_agent")]
    pool = invoke or cands
    return max(pool, key=lambda s: len(s["d"].get(K_OUT_MSGS, "") or ""))


def find_tool_defs(spans):
    """tool_definitions live on chat/invoke spans, not execute_tool spans."""
    best = ""
    for s in spans:
        v = s["d"].get(K_TOOL_DEFS, "") or ""
        if len(v) > len(best):
            best = v
    return _json_or_raw(best)


def build_actions(steps):
    """OpenAI message-schema array for Task Navigation Efficiency `actions`.
    `arguments` stays a JSON string — that is what this evaluator expects."""
    return [{
        "role": "assistant",
        "content": [{
            "type": "function_call",
            "name": st["tool"],
            "arguments": st["arguments"],
        }],
    } for st in steps]


def convert(spans):
    by_op = defaultdict(list)
    for s in spans:
        by_op[s["op_id"]].append(s)

    runs, summary = [], []
    for op_id, op_spans in by_op.items():
        op_spans.sort(key=lambda s: s["timestamp"])

        by_agent = defaultdict(list)
        for s in op_spans:
            a = s["d"].get(K_AGENT)
            if a:
                by_agent[a].append(s)

        agents_present = sorted(by_agent)
        for agent, aspans in by_agent.items():
            steps = [tool_step(s) for s in aspans if is_tool_span(s)]
            steps.sort(key=lambda st: st["timestamp"])
            run_span = pick_run_span(aspans)
            rd = run_span["d"] if run_span else {}

            query = []
            sys_i = _json_or_raw(rd.get(K_SYS))
            if sys_i:
                query.append({"role": "system", "content": sys_i})
            inp = _json_or_raw(rd.get(K_IN_MSGS))
            if isinstance(inp, list):
                query.extend(inp)
            elif inp:
                query.append({"role": "user", "content": inp})

            intent, intent_source = extract_intent(rd, steps)
            row = {
                "orchestration_id": op_id,
                "run_agent": agent,
                "intent": intent,
                "intent_source": intent_source,
                "traj_key": f"{agent}|{intent}" if intent else agent,
                "agent_version": rd.get(K_AGENT_VER, ""),
                "agent_id": rd.get(K_AGENT_ID, ""),
                "conversation_id": rd.get(K_CONV, ""),
                "model": rd.get(K_RES_MODEL) or rd.get(K_REQ_MODEL, ""),
                "started": aspans[0]["timestamp"],
                "duration_ms": max((s["duration"] for s in aspans), default=0),

                # --- evaluator inputs ---
                "query": query,
                "response": _json_or_raw(rd.get(K_OUT_MSGS)) or [],
                "tool_definitions": find_tool_defs(aspans),
                "actions": build_actions(steps),

                # --- authoring / QA helpers (not evaluator inputs) ---
                "tool_names": [st["tool"] for st in steps],
                "a2a_calls": [st["tool"] for st in steps if st["is_a2a"]],
                "tool_call_count": len(steps),
                "error_count": sum(1 for st in steps if st["errored"]),
                "truncated_results": sum(1 for st in steps if st["truncated"]),
                "unwrapped_calls": sum(1 for st in steps if st["unwrapped"]),
                "tool_errors": [
                    {"tool": st["tool"], "kind": st["error_kind"],
                     "args": st["arguments"][:200]}
                    for st in steps if st["error_kind"]
                ],
                "empty_results": [
                    {"tool": st["tool"], "args": st["arguments"][:200]}
                    for st in steps if st["empty"]
                ],
                "input_tokens": rd.get(K_IN_TOK, ""),
                "output_tokens": rd.get(K_OUT_TOK, ""),

                # --- evaluator viability, computed not assumed ---
                "has_query": bool(query),
                "has_response": bool(rd.get(K_OUT_MSGS)),
                "has_tool_definitions": bool(rd.get(K_TOOL_DEFS) or
                                             find_tool_defs(aspans)),
            }
            runs.append(row)
            summary.append({
                "orchestration_id": op_id,
                "run_agent": agent,
                "intent": intent or "",
                "intent_source": intent_source,
                "started": row["started"],
                "agents_in_trace": len(agents_present),
                "linked": "yes" if len(agents_present) > 1 else "NO",
                "tool_calls": row["tool_call_count"],
                "errors": row["error_count"],
                "truncated": row["truncated_results"],
                "has_query": row["has_query"],
                "has_response": row["has_response"],
                "has_tool_defs": row["has_tool_definitions"],
                "trajectory": " -> ".join(row["tool_names"]),
            })

    runs.sort(key=lambda r: (r["started"], r["run_agent"]))
    summary.sort(key=lambda r: (r["started"], r["run_agent"]))
    return runs, summary


# ----------------------------------------------------------------- output

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spans", help="JSON or CSV export of dependencies spans")
    ap.add_argument("-o", "--out", default="./out", help="output directory")
    args = ap.parse_args()

    spans = load_spans(args.spans)
    runs, summary = convert(spans)
    os.makedirs(args.out, exist_ok=True)

    jsonl = os.path.join(args.out, "eval_runs.jsonl")
    with open(jsonl, "w", encoding="utf-8") as fh:
        for r in runs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    csv_path = os.path.join(args.out, "runs_summary.csv")
    if summary:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(summary[0]))
            w.writeheader()
            w.writerows(summary)

    orchs = len({r["orchestration_id"] for r in runs})
    unlinked = sum(1 for s in summary if s["linked"] == "NO")
    evaluable = sum(1 for r in runs
                    if r["has_query"] and r["has_response"]
                    and r["has_tool_definitions"] and r["tool_call_count"] > 0)
    print(f"spans in           : {len(spans)}")
    print(f"orchestrations     : {orchs}")
    print(f"AI Run rows        : {len(runs)}")
    print(f"  fragmented       : {unlinked}  (single-agent traces)")
    print(f"  fully evaluable  : {evaluable}  (query+response+tool_defs+tools)")
    print(f"  with errors      : {sum(1 for r in runs if r['error_count'])}")
    print(f"  truncated results: {sum(r['truncated_results'] for r in runs)}")
    print(f"\nwrote {jsonl}")
    print(f"wrote {csv_path}")


if __name__ == "__main__":
    sys.exit(main())