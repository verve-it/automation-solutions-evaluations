#!/usr/bin/env python3
"""
extract_tool_manifest.py — build `tool_manifests/<toolbox>-v<n>.json`.

`gen_ai.tool.definitions` only ever covers A2A agent registrations, so no
schema exists in telemetry for any ConnectWise tool, and both Tool Input
Accuracy and the generated argument validation in run_evals.py have nothing to
run against. The toolbox is versioned, so this is a one-time extraction per
version, not a per-run capture.

Two inputs, best first:

  --from-tools-list <file>   the raw JSON-RPC `tools/list` response, or the
                             toolbox definition exported from the Foundry
                             portal. Produces a complete manifest.

  --from-trace <file>        a span export. Produces a SKELETON: every tool the
                             agents actually called, with the description
                             telemetry carries and the argument keys observed,
                             but `parameters: null`. Fill those in by hand or
                             re-run with --from-tools-list. A null schema is
                             skipped by the validator rather than guessed at.

    python3 extract_tool_manifest.py --from-trace traces/2026-09-03-full-triage.csv \\
        --toolbox ConnectwiseMCP --version 5 -o tool_manifests/connectwisemcp-v5.json
"""

from __future__ import annotations
import argparse, json, os, sys
from collections import defaultdict

from trace_to_eval import (K_TOOL, K_TOOL_ARGS, K_TOOL_DESC, base_tool_name,
                           find_toolboxes, is_tool_span, load_spans,
                           unwrap_call_tool, AGENT_NAMES)


def from_tools_list(payload):
    """Accept the JSON-RPC envelope, its `result`, or a bare tool array."""
    if isinstance(payload, dict):
        payload = payload.get("result", payload)
        payload = payload.get("tools", payload)
    if not isinstance(payload, list):
        raise ValueError("expected a list of tools, or {result:{tools:[...]}}")
    return [{
        "name": base_tool_name(t.get("name", "")),
        "description": t.get("description", ""),
        "parameters": t.get("inputSchema") or t.get("parameters"),
    } for t in payload]


def from_trace(spans):
    """Names, descriptions and observed argument keys. No schemas — telemetry
    does not carry them, and inventing one would score runs against a fiction.
    """
    seen, keys = {}, defaultdict(set)
    for s in spans:
        if not is_tool_span(s):
            continue
        d = s["d"]
        name = d.get(K_TOOL, "") or s["name"].replace("execute_tool ", "", 1)
        name, args, _ = unwrap_call_tool(name, d.get(K_TOOL_ARGS, "") or "")
        if name in AGENT_NAMES:            # A2A hand-off, not an MCP tool
            continue
        bare = base_tool_name(name)
        seen.setdefault(bare, d.get(K_TOOL_DESC, "") or "")
        if not seen[bare]:
            seen[bare] = d.get(K_TOOL_DESC, "") or ""
        try:
            parsed = json.loads(args) if args else {}
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            keys[bare].update(parsed)

    return [{
        "name": name,
        "description": desc,
        "parameters": None,
        "observed_argument_keys": sorted(keys[name]),
    } for name, desc in sorted(seen.items())]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-tools-list", metavar="FILE")
    src.add_argument("--from-trace", metavar="FILE")
    ap.add_argument("--toolbox", required=True, help="e.g. ConnectwiseMCP")
    ap.add_argument("--version", required=True,
                    help="toolbox version; scoring old behaviour against a "
                         "new schema silently corrupts results")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    if args.from_tools_list:
        with open(args.from_tools_list, encoding="utf-8") as fh:
            tools = from_tools_list(json.load(fh))
        source = f"tools/list: {os.path.basename(args.from_tools_list)}"
    else:
        spans = load_spans(args.from_trace)
        tools = from_trace(spans)
        source = f"trace skeleton: {os.path.basename(args.from_trace)}"
        boxes = {f"{t}@{v}" for s in spans for t, v in find_toolboxes([s])}
        if boxes:
            print(f"toolbox versions in this trace: {', '.join(sorted(boxes))}")

    manifest = {
        "toolbox": args.toolbox,
        "version": str(args.version),
        "source": source,
        "tools": tools,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, ensure_ascii=False)
        fh.write("\n")

    without = [t["name"] for t in tools if not t.get("parameters")]
    print(f"{len(tools)} tool(s) -> {args.out}")
    if without:
        print(f"{len(without)} without a schema (not validated until filled): "
              f"{', '.join(without)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
