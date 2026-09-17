#!/usr/bin/env python3
"""
trace_to_eval.py — convert Foundry agent traces (App Insights export) into
Foundry-evaluator-ready JSONL, one row per AI Run (one per agent).

INPUT   JSON or CSV exported from App Insights `dependencies` (or
        `AppDependencies`). Export the raw rows, not a projection — a
        flattening projection strips every `gen_ai.*` attribute:
            dependencies
            | where operation_Id in ("...","...")
            | project timestamp, name, id, operation_Id, operation_ParentId,
                      duration, success, customDimensions

        `export_traces.py` produces this shape unattended.

OUTPUT  <out>/eval_runs.jsonl   one row per agent run
        <out>/runs_summary.csv  flat index for sizing / triage

MODEL   operation_Id            -> AI Orchestration
        distinct gen_ai.agent.name -> AI Run (one each, orchestrator included)
        execute_tool spans      -> that run's tool trajectory
        execute_tool <agentname>-> A2A link: kept as a step in the CALLER's
                                   trajectory, and the callee is its own run

Run:  python3 trace_to_eval.py spans.json -o ./out
      python3 trace_to_eval.py spans.json -o ./out --tool-defs tool_manifests/
"""

from __future__ import annotations
import argparse, csv, glob, hashlib, json, os, re, sys
from collections import defaultdict
from datetime import datetime, timezone

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
K_TOOL_DESC  = "gen_ai.tool.description"
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
K_CACHE_R    = "gen_ai.usage.cache_read.input_tokens"
K_CACHE_W    = "gen_ai.usage.cache_creation.input_tokens"
K_REASON_TOK = "gen_ai.usage.reasoning.output_tokens"

TRUNC_BOUNDARY = 8192   # values landing exactly here are suspect

# Consecutive fruitless calls to one tool. Three in a row is ordinary
# enumeration — "any documents? any configurations? any associations?",
# each legitimately answered none. Four or more is an agent guessing at a
# vocabulary, and that is the signature worth a check. Seen live at 9.
CASCADE_MIN = 4

# `POST /api/projects/<p>/toolboxes/<toolbox>/versions/<v>/mcp` — the only
# place the toolbox binding appears.
#
# This number is a BINDING REVISION, not a schema version. Two agents in one
# orchestration can show different numbers for the same toolbox purely because
# their bindings were edited at different times: in the frozen traces the ops
# agent shows v1 and the analysis agent v5, and the `cw_resolve`,
# `cw_get_ticket` and `load_skill` descriptions are byte-identical across both.
# So a manifest normally declares `"versions": ["*"]`. Pin to specific
# revisions only when you have evidence the contract actually differs.
TOOLBOX_RE = re.compile(r"/toolboxes/([^/]+)/versions/([^/]+)/")

# Foundry prefixes MCP tools with the server name: `<server>___<tool>`.
TOOL_PREFIX_SEP = "___"

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


def base_tool_name(name):
    """`ConnectWise-PSA-ForAgents___cw_resolve` -> `cw_resolve`.

    Manifests come from the MCP server and use bare names; telemetry carries
    the Foundry-prefixed name. Match on the bare name.
    """
    return name.rsplit(TOOL_PREFIX_SEP, 1)[-1] if name else name


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

# customDimensions has been observed at 59,270 chars; the csv default field
# limit is 131,072 and a single wide row will blow it without warning.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


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
    """Tolerate App Insights vs Log Analytics vs portal-export column naming.

    The portal's CSV download renames the time column to `timestamp [UTC]`
    (or `timestamp [Local Time]`). Matching only the bare name silently
    yielded an empty `started` on every run and left ordering to input order.
    """
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    lowered = {k.strip().lower(): k for k in row if isinstance(k, str)}
    for n in names:
        target = n.strip().lower()
        for low, orig in lowered.items():
            if low == target or low.startswith(target + " ["):
                if row[orig] not in (None, ""):
                    return row[orig]
    return None


# Portal CSV exports carry a locale-formatted timestamp; Log Analytics gives
# ISO-8601. Normalise both to sortable UTC ISO or ordering is wrong the first
# time a trace spans a month boundary ("10/1" sorts before "9/3").
_TS_FORMATS = (
    "%m/%d/%Y, %I:%M:%S.%f %p",
    "%m/%d/%Y, %I:%M:%S %p",
    "%m/%d/%Y %I:%M:%S.%f %p",
    "%m/%d/%Y %I:%M:%S %p",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
)


def normalise_timestamp(raw):
    """Return a sortable UTC ISO-8601 string, or the input unchanged."""
    if not raw:
        return ""
    if isinstance(raw, datetime):
        dt = raw
    else:
        s = str(raw).strip()
        dt = None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            for fmt in _TS_FORMATS:
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return s
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="milliseconds") + "Z"


# AppGenAIContent columns -> the gen_ai.* attribute they replace. From
# 2026-09-30 App Insights stops writing these seven values into the span
# property bag and leaves only a pointer (_MS.GenAIContentId), so a span
# export alone is empty of content. export_traces.py joins the table and
# emits these columns; merging them back here keeps every downstream check
# working either side of that change.
#
# It also undoes the 8192-character truncation: these are real columns, not
# property-bag entries, so the App Insights property cap never applied.
CONTENT_COLUMNS = {
    "c_input":       K_IN_MSGS,
    "c_output":      K_OUT_MSGS,
    "c_system":      K_SYS,
    "c_tool_defs":   K_TOOL_DEFS,
    "c_tool_args":   K_TOOL_ARGS,
    "c_tool_result": K_TOOL_RES,
}


def merge_content_columns(row, dims):
    """Overlay AppGenAIContent columns onto a span's customDimensions.

    The column wins when it has a value: after the migration the property bag
    holds a pointer rather than the payload, and before it the bag may hold a
    truncated copy of what the column has in full.
    """
    merged = dict(dims)
    for column, attr in CONTENT_COLUMNS.items():
        value = _col(row, column)
        if value not in (None, ""):
            merged[attr] = value
    return merged


def load_spans(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        head = fh.read(1)
        fh.seek(0)
        rows = json.load(fh) if head in "[{" else list(csv.DictReader(fh))
    if isinstance(rows, dict):                 # {"tables":[...]} or single row
        rows = rows.get("value") or rows.get("rows") or [rows]

    spans = []
    for r in rows:
        d = _as_dict(_col(r, "customDimensions", "CustomDimensions", "Properties",
                          "dims"))
        d = merge_content_columns(r, d)
        spans.append({
            "timestamp": normalise_timestamp(
                _col(r, "timestamp", "TimeGenerated")),
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


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------- tool manifests

def load_tool_manifests(paths):
    """Load `tool_manifests/*.json`.

    `gen_ai.tool.definitions` only ever covers A2A agent registrations, so no
    schema for any ConnectWise tool exists in telemetry. A manifest supplies
    it, once per toolbox, and declares which binding revisions it covers —
    `"versions": ["*"]` normally. See tool_manifests/README.md.
    """
    files = []
    for p in paths or []:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "*.json"))))
        else:
            files.append(p)

    manifests = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            m = json.load(fh)
        toolbox = m.get("toolbox")
        if not toolbox:
            raise ValueError(f"{f}: manifest needs 'toolbox'")
        versions = m.get("versions")
        if versions is None:
            versions = [m["version"]] if "version" in m else ["*"]
        manifests.append({
            "toolbox": toolbox,
            "versions": [str(v) for v in versions],
            "tools": [_as_definition(t) for t in m.get("tools", [])],
        })
    return manifests


def manifest_applies(manifest, toolbox, version):
    if manifest["toolbox"] != toolbox:
        return False
    return "*" in manifest["versions"] or str(version) in manifest["versions"]


def _as_definition(tool):
    """Normalise a tools/list entry to the shape telemetry uses, so a run's
    tool_definitions reads the same whether it came from a span or a file."""
    params = tool.get("parameters")
    if params is None:
        params = tool.get("inputSchema")
    return {
        "type": "function",
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": params,
    }


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
# Real hand-offs are `intent=Full Triage; ticketId=805392; ...` — key=value
# pairs separated by semicolons. Accepting only `intent:` matched nothing and
# every run keyed as the bare agent name, which silently skipped every
# intent-keyed expectation in expected.json. The optional quotes also pick up
# the JSON form, `"intent": "Write Request"`.
_INTENT_RE = re.compile(r'intent"?\s*[:=]\s*"?([^\n\r;"\\,}]+)', re.IGNORECASE)


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


def extract_intent(run_dims, steps, inbound=None):
    """
    Resolve what this run was asked to do.

    Three sources, in order:
      declared  - the run's own input carries `intent=<x>` (child agents get
                  this from the orchestrator's structured hand-off)
      inbound   - a caller's A2A hand-off to this agent, in this orchestration,
                  declared one. Needed because an agent invoked twice in one
                  orchestration is a single AI Run, and only one of the two
                  hand-offs is the span we read messages from — the other
                  phrasing is free text and the intent would be lost.
      delegated - the run has no declared intent but issued an A2A call whose
                  arguments declare one; that is the orchestrator's case, since
                  its own input is free text like "Automated flow: triage 805392"

    Returns (intent, source). Intent is None when none applies, which keeps
    intent-keyed expectations from matching a run whose intent we guessed.
    """
    blob = " ".join(_flatten_text(_json_or_raw(run_dims.get(K_IN_MSGS))))
    m = _INTENT_RE.search(blob)
    if m:
        return _normalise_intent(m.group(1)), "declared"

    if inbound:
        return _normalise_intent(inbound), "inbound"

    for st in steps:
        if not st.get("is_a2a"):
            continue
        m = _INTENT_RE.search(st.get("arguments", "") or "")
        if m:
            return _normalise_intent(m.group(1)), "delegated"

    return None, "unknown"


def inbound_intents(steps_by_agent):
    """Map callee agent -> intent declared in the first hand-off that carries
    one, across every caller in this orchestration."""
    found = {}
    for steps in steps_by_agent.values():
        for st in steps:
            if not st.get("is_a2a") or st["tool"] in found:
                continue
            m = _INTENT_RE.search(st.get("arguments", "") or "")
            if m:
                found[st["tool"]] = m.group(1)
    return found


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


_FRONTMATTER_NAME = re.compile(r"^---\s*\nname:\s*([^\n]+)", re.MULTILINE)


def collect_skills(steps):
    """Which skill files were in force for this run, by content hash.

    `load_skill` returns raw markdown with YAML frontmatter carrying `name`
    and `description` but NO version, so "which rules were in force" is only
    answerable by hashing what came back. That works until the payload
    truncates — and `load_skill` is the largest payload in the traces
    (25,023 chars observed), so the agent reasoned over incomplete RULES,
    not merely incomplete data.

    The durable fix is agent-side: have load_skill return
    {skill_name, version, sha256} and rehydrate at eval time. Until then this
    records what it can and marks what it cannot trust.
    """
    skills = []
    for st in steps:
        if base_tool_name(st["tool"]) != "load_skill":
            continue
        try:
            name = (json.loads(st["arguments"] or "{}") or {}).get("skill_name")
        except json.JSONDecodeError:
            name = None
        body = st["result"] or ""
        m = _FRONTMATTER_NAME.search(body)
        skills.append({
            "skill_name": name or (m.group(1).strip() if m else ""),
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "bytes": len(body),
            # A truncated skill hashes to something that identifies the
            # truncation, not the skill. Never compare these across runs.
            "truncated": st["truncated"],
            "errored": st["errored"],
        })
    return skills


def write_skill_registry(runs, registry_dir):
    """Store each intact skill body once, by hash, so a historical run's rules
    can be read back. Truncated bodies are never stored."""
    os.makedirs(registry_dir, exist_ok=True)
    index_path = os.path.join(registry_dir, "index.json")
    index = {}
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as fh:
            index = json.load(fh)

    written = 0
    for run, bodies in runs:
        for skill, body in bodies:
            if skill["truncated"] or skill["errored"] or not body:
                continue
            digest = skill["sha256"]
            if digest in index:
                continue
            with open(os.path.join(registry_dir, f"{digest}.md"), "w",
                      encoding="utf-8") as fh:
                fh.write(body)
            index[digest] = {"skill_name": skill["skill_name"],
                             "bytes": skill["bytes"],
                             "first_seen": run["started"]}
            written += 1

    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=1, sort_keys=True)
    return written, len(index)


def find_cascades(steps, min_len=CASCADE_MIN):
    """Consecutive fruitless calls to the same tool — a distinct signature
    from one bad call, and the one that burns the most time.

    Seen live: the ops agent resolved "A.S. Economou Development" -> empty,
    "4597" -> empty, "A." -> empty, then queried companies directly -> also
    empty, ~34 calls to discover that an upstream agent proposed a company
    that does not exist in ConnectWise.

    Consecutive-and-same-tool only, so it stays deterministic and explainable.
    The recorded arguments are what shows the degradation; the check just
    counts.
    """
    cascades, run = [], []

    def flush():
        if len(run) >= min_len:
            cascades.append({
                "tool": run[0]["tool"],
                "length": len(run),
                "args": [st["arguments"][:120] for st in run],
            })
        run.clear()

    for st in steps:
        fruitless = st["errored"] or st["empty"]
        if fruitless and (not run or run[0]["tool"] == st["tool"]):
            run.append(st)
        else:
            flush()
            if fruitless:
                run.append(st)
    flush()
    return cascades


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


def find_toolboxes(spans):
    """Which toolbox bindings this run actually called, as (toolbox, revision).

    The revision is which edit of the binding the agent is pinned to, not a
    tool-contract version — see TOOLBOX_RE. Recorded so a genuine contract
    change can be pinned later, and so a binding drifting between agents is
    at least visible.
    """
    seen = []
    for s in spans:
        m = TOOLBOX_RE.search(s["name"])
        if m and list(m.groups()) not in [list(x) for x in seen]:
            seen.append((m.group(1), m.group(2)))
    return seen


def collect_usage(spans):
    """Token usage across the run's chat spans.

    The usage attributes sit on `chat` spans, not on the invoke_agent span the
    messages come from, so reading them off the run span alone returned blank
    every time and the token data in the traces went unused.

    `input_tokens` on a span is that turn's WHOLE prompt, which grows with the
    conversation, and `cache_read` is a subset of it. Summing input_tokens is
    therefore not a spend figure — it counts the same context once per turn
    (7.4M for one 55-call run). The fields worth gating on later:

      uncached_input_tokens  what was actually processed fresh
      cache_read_tokens      what the cache served
      peak_input_tokens      the largest single prompt; the context-size signal

    `invoke_agent` spans carry a ROLL-UP of the same numbers, so counting both
    them and the chat spans doubles every figure. Sum the chat spans; fall back
    to the roll-up only when no chat span carries usage. The roll-up is also
    incomplete when an agent is invoked twice in one orchestration — it covers
    one invocation, the chat spans cover both.
    """
    chat = [s for s in spans
            if (K_IN_TOK in s["d"] or K_OUT_TOK in s["d"])
            and not s["name"].startswith("invoke_agent")]
    rollup = [s for s in spans
              if (K_IN_TOK in s["d"] or K_OUT_TOK in s["d"])
              and s["name"].startswith("invoke_agent")]
    source = chat or rollup

    usage = {"llm_calls": len(chat), "prompt_tokens_sum": 0,
             "uncached_input_tokens": 0, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "output_tokens": 0,
             "reasoning_tokens": 0, "peak_input_tokens": 0,
             "usage_source": "chat_spans" if chat else
                             ("rollup" if rollup else "none")}
    for s in source:
        d = s["d"]
        prompt = _int(d.get(K_IN_TOK))
        cached = _int(d.get(K_CACHE_R))
        usage["prompt_tokens_sum"] += prompt
        usage["uncached_input_tokens"] += max(prompt - cached, 0)
        usage["cache_read_tokens"] += cached
        usage["cache_write_tokens"] += _int(d.get(K_CACHE_W))
        usage["output_tokens"] += _int(d.get(K_OUT_TOK))
        usage["reasoning_tokens"] += _int(d.get(K_REASON_TOK))
        usage["peak_input_tokens"] = max(usage["peak_input_tokens"], prompt)
    return usage


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


def resolve_tool_definitions(span_defs, toolboxes, manifests):
    """Telemetry definitions plus any manifest covering a toolbox this run
    actually used. Returns (definitions, source_label)."""
    defs = list(span_defs) if isinstance(span_defs, list) else []
    sources = ["telemetry"] if defs else []

    for toolbox, version in toolboxes:
        for m in manifests:
            if not manifest_applies(m, toolbox, version):
                continue
            have = {d.get("name") for d in defs}
            defs.extend(t for t in m["tools"] if t.get("name") not in have)
            sources.append(f"manifest:{toolbox}@{version}")

    if not defs and span_defs:
        return span_defs, "telemetry"
    return defs, "+".join(sources) if sources else ""


def convert(spans, manifests=None):
    manifests = manifests or []
    by_op = defaultdict(list)
    for s in spans:
        by_op[s["op_id"]].append(s)

    runs, summary, skill_bodies = [], [], []
    for op_id, op_spans in by_op.items():
        op_spans.sort(key=lambda s: s["timestamp"])

        by_agent = defaultdict(list)
        for s in op_spans:
            a = s["d"].get(K_AGENT)
            if a:
                by_agent[a].append(s)

        agents_present = sorted(by_agent)

        # Steps first for every agent in the orchestration, so a child run can
        # read the intent off its caller's hand-off.
        steps_by_agent = {}
        for agent, aspans in by_agent.items():
            steps = [tool_step(s) for s in aspans if is_tool_span(s)]
            steps.sort(key=lambda st: st["timestamp"])
            steps_by_agent[agent] = steps
        inbound = inbound_intents(steps_by_agent)

        for agent, aspans in by_agent.items():
            steps = steps_by_agent[agent]
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

            intent, intent_source = extract_intent(rd, steps,
                                                   inbound.get(agent))
            toolboxes = find_toolboxes(aspans)
            tool_defs, defs_source = resolve_tool_definitions(
                find_tool_defs(aspans), toolboxes, manifests)
            usage = collect_usage(aspans)
            cascades = find_cascades(steps)
            skills = collect_skills(steps)

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
                "mcp_toolboxes": [{"toolbox": t, "version": v}
                                  for t, v in toolboxes],

                # --- evaluator inputs ---
                "query": query,
                "response": _json_or_raw(rd.get(K_OUT_MSGS)) or [],
                "tool_definitions": tool_defs,
                "tool_definitions_source": defs_source,
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
                "search_cascades": cascades,
                "skills_in_force": skills,
                "truncated_skills": sum(1 for s in skills if s["truncated"]),
                "tool_ms": round(sum(st["duration_ms"] for st in steps), 1),
                "usage": usage,

                # --- evaluator viability, computed not assumed ---
                "has_query": bool(query),
                "has_response": bool(rd.get(K_OUT_MSGS)),
                "has_tool_definitions": bool(tool_defs),
            }
            runs.append(row)
            skill_bodies.append((row, [
                (sk, st["result"])
                for sk, st in zip(skills, [s for s in steps
                                           if base_tool_name(s["tool"])
                                           == "load_skill"])
            ]))
            summary.append({
                "orchestration_id": op_id,
                "run_agent": agent,
                "intent": intent or "",
                "intent_source": intent_source,
                "started": row["started"],
                "agents_in_trace": len(agents_present),
                "linked": "yes" if len(agents_present) > 1 else "NO",
                "mcp": ";".join(f"{t}@{v}" for t, v in toolboxes),
                "tool_calls": row["tool_call_count"],
                "errors": row["error_count"],
                "truncated": row["truncated_results"],
                "cascades": len(cascades),
                "skills": ";".join(s["skill_name"] for s in skills),
                "truncated_skills": sum(1 for s in skills if s["truncated"]),
                "llm_calls": usage["llm_calls"],
                "uncached_in": usage["uncached_input_tokens"],
                "out_tokens": usage["output_tokens"],
                "peak_ctx": usage["peak_input_tokens"],
                "has_query": row["has_query"],
                "has_response": row["has_response"],
                "has_tool_defs": row["has_tool_definitions"],
                "trajectory": " -> ".join(row["tool_names"]),
            })

    runs.sort(key=lambda r: (r["started"], r["run_agent"]))
    summary.sort(key=lambda r: (r["started"], r["run_agent"]))
    return runs, summary, skill_bodies


# ----------------------------------------------------------------- output

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spans", help="JSON or CSV export of dependencies spans")
    ap.add_argument("-o", "--out", default="./out", help="output directory")
    ap.add_argument("--skill-registry", metavar="DIR",
                    help="store each intact load_skill body once, by sha256, "
                         "so a historical run's rules can be read back")
    ap.add_argument("--tool-defs", action="append", metavar="PATH",
                    help="tool manifest JSON, or a directory of them. "
                         "Injected as tool_definitions for runs whose MCP "
                         "toolbox version matches. Repeatable.")
    args = ap.parse_args()

    manifests = load_tool_manifests(args.tool_defs)
    spans = load_spans(args.spans)

    # A span export taken after 2026-09-30 without the AppGenAIContent join
    # still has the attribute KEYS, holding pointers instead of payloads.
    # Every check would then quietly score empty runs, so say so loudly.
    pointered = sum(1 for s in spans
                    if s["d"].get("_MS.GenAIContentId")
                    and not s["d"].get(K_IN_MSGS)
                    and not s["d"].get(K_TOOL_RES)
                    and not s["d"].get(K_TOOL_ARGS))
    if pointered and pointered == sum(1 for s in spans
                                      if s["d"].get("_MS.GenAIContentId")):
        print(f"WARNING: {pointered} span(s) carry _MS.GenAIContentId with no "
              "gen_ai content. App Insights stopped writing these values into "
              "the span tables on 2026-09-30 — re-export with the "
              "AppGenAIContent join (export_traces.py does this by default) "
              "or every check will score an empty run.\n")

    runs, summary, skill_bodies = convert(spans, manifests)
    os.makedirs(args.out, exist_ok=True)

    if args.skill_registry:
        added, total = write_skill_registry(skill_bodies, args.skill_registry)
        print(f"skill registry     : +{added} new, {total} total in "
              f"{args.skill_registry}")

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
    toolboxes = sorted({f"{t['toolbox']}@{t['version']}"
                        for r in runs for t in r["mcp_toolboxes"]})
    print(f"spans in           : {len(spans)}")
    print(f"orchestrations     : {orchs}")
    print(f"AI Run rows        : {len(runs)}")
    print(f"  fragmented       : {unlinked}  (single-agent traces)")
    print(f"  fully evaluable  : {evaluable}  (query+response+tool_defs+tools)")
    print(f"  with errors      : {sum(1 for r in runs if r['error_count'])}")
    print(f"  truncated results: {sum(r['truncated_results'] for r in runs)}")
    print(f"  search cascades  : {sum(len(r['search_cascades']) for r in runs)}")
    trunc_skills = sum(r["truncated_skills"] for r in runs)
    if trunc_skills:
        print(f"  TRUNCATED SKILLS : {trunc_skills}  (agent reasoned over "
              f"incomplete rules)")
    print(f"  unknown intent   : {sum(1 for r in runs if not r['intent'])}")
    if manifests:
        print("  manifests loaded : " + ", ".join(
            f"{m['toolbox']}@{','.join(m['versions'])}" for m in manifests))
    if toolboxes:
        print(f"  toolboxes seen   : {', '.join(toolboxes)}")
    print(f"\nwrote {jsonl}")
    print(f"wrote {csv_path}")


if __name__ == "__main__":
    sys.exit(main())
