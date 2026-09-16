#!/usr/bin/env python3
"""
export_traces.py — pull agent spans out of Log Analytics without a human in
the portal. The missing piece for CI and scheduled runs.

Output is the raw span shape `trace_to_eval.py` consumes: one JSON array of
rows with `customDimensions` intact. A flattening projection strips every
`gen_ai.*` attribute, so the projection below is deliberate — do not "tidy" it.

Two modes:

  lag        evaluate whatever production produced since the last run. No
             infrastructure needed; the gate runs after the change is live.
                 python3 export_traces.py --workspace $LAW_ID \\
                     --state .export-state.json -o traces/auto/nightly.json

  explicit   re-export named orchestrations, e.g. to refresh a frozen set.
                 python3 export_traces.py --workspace $LAW_ID \\
                     --operation-ids bed408b4...,4dda7f4f... -o traces/set.json

It selects whole orchestrations, never individual spans: filtering by agent up
front drops the sibling spans that carry the messages and token usage.

Auth is DefaultAzureCredential — `az login` locally, workload identity in CI.
The identity needs Log Analytics Reader on the workspace.
"""

from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, timedelta, timezone

from trace_to_eval import AGENT_NAMES

# App Insights resource-centric queries use the classic table and column
# names; workspace-centric queries use the Az Monitor ones.
TABLES = {
    "workspace": "AppDependencies",
    "resource": "dependencies",
}

# Canonical column names, so the converter never has to care which surface the
# export came from.
_RENAME = """
| extend timestamp = TimeGenerated, name = Name, id = Id,
         operation_Id = OperationId, operation_ParentId = ParentId,
         duration = DurationMs, success = Success,
         customDimensions = Properties
"""

PROJECT = """
| project timestamp, name, id, operation_Id, operation_ParentId,
          duration, success, customDimensions
| order by timestamp asc
"""


def _kusto_list(values):
    return ", ".join(json.dumps(v) for v in sorted(values))


def candidates_query(table, agents, min_tool_calls, limit):
    """Rank orchestrations by how much of them is evaluable.

    `has_tool_defs` stays in the ranking even though it is nearly always 0
    today — it is the signal that tells you the moment the MCP manifest gap
    closes at the source.
    """
    rename = _RENAME if table == TABLES["workspace"] else ""
    return f"""{table}{rename}
| extend d = customDimensions
| extend agent = tostring(d["gen_ai.agent.name"])
| where agent in ({_kusto_list(agents)})
| summarize started = min(timestamp), agents = make_set(agent),
            agent_count = dcount(agent), spans = count(),
            tool_calls = countif(name startswith "execute_tool"),
            has_tool_defs = countif(isnotempty(tostring(d["gen_ai.tool.definitions"])))
  by operation_Id
| where tool_calls >= {int(min_tool_calls)}
| extend usable = case(agent_count > 1 and tool_calls > 0 and has_tool_defs > 0, "1-full",
                       tool_calls > 0 and has_tool_defs > 0, "2-single agent",
                       tool_calls > 0, "3-no tool defs", "4-thin")
| order by usable asc, started desc
| limit {int(limit)}
"""


def spans_query(table, operation_ids):
    """Every span of the selected orchestrations, customDimensions intact."""
    rename = _RENAME if table == TABLES["workspace"] else ""
    return f"""let ids = dynamic([{_kusto_list(operation_ids)}]);
{table}{rename}
| where operation_Id in (ids)
{PROJECT}"""


# ------------------------------------------------------------------ window

def resolve_window(args, state):
    """(start, end) as aware UTC datetimes.

    Lag mode starts at the last exported span so a nightly run never
    re-evaluates yesterday's traces or skips an hour.
    """
    end = _parse_time(args.until) if args.until else datetime.now(timezone.utc)
    if args.since:
        return _parse_time(args.since), end
    if args.hours:
        return end - timedelta(hours=args.hours), end
    watermark = (state or {}).get("last_timestamp")
    if watermark:
        return _parse_time(watermark), end
    return end - timedelta(hours=24), end


def _parse_time(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_state(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_state(path, rows, window):
    if not path:
        return
    stamps = [str(r.get("timestamp")) for r in rows if r.get("timestamp")]
    state = {
        "last_timestamp": max(stamps) if stamps else window[1].isoformat(),
        "last_run": datetime.now(timezone.utc).isoformat(),
        "exported_spans": len(rows),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    return state


# ------------------------------------------------------------------ client

def _client():
    """Imported lazily so --dry-run and the tests work without the SDK."""
    from azure.identity import DefaultAzureCredential
    from azure.monitor.query import LogsQueryClient
    return LogsQueryClient(DefaultAzureCredential())


def run_query(client, args, query, timespan):
    from azure.monitor.query import LogsQueryStatus

    if args.resource_id:
        response = client.query_resource(args.resource_id, query,
                                         timespan=timespan)
    else:
        response = client.query_workspace(args.workspace, query,
                                          timespan=timespan)

    if response.status == LogsQueryStatus.FAILURE:
        raise RuntimeError(f"Log Analytics query failed: {response}")
    if response.status == LogsQueryStatus.PARTIAL:
        print("WARNING: partial result — narrow the window or raise the "
              "service limit", file=sys.stderr)
        tables = response.partial_data
    else:
        tables = response.tables
    return [t for t in tables]


def rows_from(tables):
    out = []
    for table in tables:
        cols = list(table.columns)
        for row in table.rows:
            out.append(dict(zip(cols, row)))
    return out


def _jsonable(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--workspace", help="Log Analytics workspace GUID")
    target.add_argument("--resource-id",
                        help="App Insights ARM resource id (resource-centric)")

    ap.add_argument("-o", "--out", help="output JSON (default traces/auto/...)")
    ap.add_argument("--state", help="watermark file for lag mode")
    ap.add_argument("--since", help="ISO-8601 start (overrides --state)")
    ap.add_argument("--until", help="ISO-8601 end (default now)")
    ap.add_argument("--hours", type=float, help="look back this many hours")
    ap.add_argument("--operation-ids",
                    help="comma-separated operation_Ids; skips discovery")
    ap.add_argument("--ids-file", help="file with one operation_Id per line")
    ap.add_argument("--agents", default=",".join(sorted(AGENT_NAMES)),
                    help="comma-separated agent names to look for")
    ap.add_argument("--min-tool-calls", type=int, default=1,
                    help="drop thin traces with fewer tool calls than this")
    ap.add_argument("--max-orchestrations", type=int, default=25)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the KQL and exit; no credentials needed")
    args = ap.parse_args()

    table = TABLES["resource"] if args.resource_id else TABLES["workspace"]
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    state = load_state(args.state)
    start, end = resolve_window(args, state)

    ids = []
    if args.operation_ids:
        ids = [i.strip() for i in args.operation_ids.split(",") if i.strip()]
    if args.ids_file:
        with open(args.ids_file, encoding="utf-8") as fh:
            ids += [l.strip() for l in fh if l.strip()]

    if args.dry_run:
        print(f"-- window {start.isoformat()} .. {end.isoformat()}\n")
        if not ids:
            print("-- 1. discover orchestrations")
            print(candidates_query(table, agents, args.min_tool_calls,
                                   args.max_orchestrations))
        print("-- 2. export spans")
        print(spans_query(table, ids or ["<operation_id>"]))
        return 0

    client = _client()
    timespan = (start, end)

    if not ids:
        tables = run_query(client, args, candidates_query(
            table, agents, args.min_tool_calls, args.max_orchestrations),
            timespan)
        found = rows_from(tables)
        ids = [r["operation_Id"] for r in found]
        for r in found:
            print(f"  {r.get('usable','?'):<16} {r['operation_Id']}  "
                  f"agents={r.get('agent_count')} tools={r.get('tool_calls')}")

    if not ids:
        print("no orchestrations in window — nothing to export")
        save_state(args.state, [], (start, end))
        return 0

    tables = run_query(client, args, spans_query(table, ids), timespan)
    rows = [{k: _jsonable(v) for k, v in r.items()}
            for r in rows_from(tables)]

    out = args.out or os.path.join(
        "traces", "auto",
        f"spans-{end.strftime('%Y%m%dT%H%M%SZ')}.json")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False)

    meta = {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "operation_ids": ids,
        "spans": len(rows),
        "table": table,
        "agents": agents,
    }
    with open(out + ".meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)

    save_state(args.state, rows, (start, end))
    print(f"\n{len(rows)} spans from {len(ids)} orchestration(s)")
    print(f"wrote {out}")
    print(f"wrote {out}.meta.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
