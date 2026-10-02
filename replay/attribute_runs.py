#!/usr/bin/env python3
"""
attribute_runs.py — say which scored run is the replay of which recording, and
refuse any replay that cannot be proven to have run against the stub.

    python replay/attribute_runs.py "artifacts/manifest-*.json" \\
        --runs out/window/eval_runs.jsonl --tool-defs tool_manifests/ \\
        --spans out/raw/window-spans.json --out-spans out/raw/replay-spans.json \\
        --out-runs out/replay/eval_runs.jsonl \\
        --out-baseline artifacts/replay-baseline.json

Why this exists
---------------
A baseline row is keyed by `(orchestration_id, run_agent)`, and
`orchestration_id` is the App Insights `operation_Id` of the RECORDED run. A
replay is a new invocation, so it gets a new operation_Id. Scored as-is, every
replayed row is "new, not in baseline, not compared", no regression can be
found, and the gate exits 0 whatever the agent did. That was not
hypothetical: the two worst recorded runs, scored the way the gate scored
them, passed 0 of 2 runs (4 of 8 gating verdicts) and exited 0.

How a replayed row is found
---------------------------
By `(run_agent, agent_version) == (replay_agent, temp_version)`, and then by
the replay's own toolbox. The clone is a version of a separate replay agent
that nothing but run_replay.py calls, so other traffic in the window is never
scored. The version alone is not unique: the replay agent is created for each
run and deleted after it, so every clone is v1, and the gate's replays of one
agent all share that pair. Each replay binds a toolbox named for its own
session (`replay-<session>`), which is.

What is proven before it is scored
----------------------------------
The replay went through the stub, and only the stub:

  * no A2A call, and no other agent in its operation -- a child reached by
    name runs its production version against the live toolbox;
  * no toolbox but the replay's own in its trace;
  * the replay server saw this replay's session, and for every tool the
    recording did not run locally, no more calls in the trace than the server
    journalled. More means calls went somewhere else. "Local" is taken from
    the recording (load_skill, tool_search), never inferred from a missing
    prefix: a write through a client the agent built itself has none either;
  * the agent is the one the cassette recorded.

And it is complete: for every tool, at least as many calls in the trace as
the server journalled. Fewer means App Insights has not ingested them yet,
and a half-ingested run scores as a regression.

Then it is presented under the recording's identity -- run_agent, traj_key,
toolbox, tool definitions -- so the same converter output compares like for
like. What was actually observed is kept under the row's `replay` key.

Exit codes
----------
0 attributed. 3 every failure may be ingestion lag; the caller should export
again. 1 anything waiting will not fix. Nothing is written unless everything
attributes: a gate that quietly compares less than it replayed is the bug.

Stdlib only, plus trace_to_eval from this repository.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)

import argparse  # noqa: E402
import collections  # noqa: E402
import glob  # noqa: E402
import json  # noqa: E402

import trace_to_eval as tte  # noqa: E402

REQUIRED = ("agent", "replay_agent", "temp_version",
            "recorded_orchestration_id", "recorded_agent")

EXIT_FAILED = 1
EXIT_NOT_YET = 3          # every failure could be ingestion lag


def expand(patterns):
    """Expand globs here. PowerShell does not, and bash leaves an unmatched
    pattern as the literal string."""
    out = []
    for p in patterns:
        hits = sorted(glob.glob(p))
        if hits:
            out.extend(hits)
        elif not any(c in p for c in "*?["):
            out.append(p)            # a literal path: let opening it fail
    return list(dict.fromkeys(out))


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_rows(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def index_baselines(directory):
    """{(orchestration_id, run_agent): [(file, row), ...]} over every
    committed baseline. A key in two files is ambiguous, and reported so."""
    index = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        rows = load_json(path)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (row.get("orchestration_id"), row.get("run_agent"))
            index.setdefault(key, []).append((os.path.basename(path), row))
    return index


def _span_op(span):
    return span.get("operation_Id") or span.get("OperationId") or ""


def tool_counts(row, local_tools):
    """(every call, calls that must have reached the stub) per bare name.

    Local means a tool the RECORDING called unprefixed -- load_skill,
    tool_search -- which the agent runs itself. It is not inferred from a
    missing prefix: a write sent to ConnectWise through a client the agent
    built itself carries no prefix either, and would pass as local.
    """
    names = row.get("tool_names") or []
    every = collections.Counter(tte.base_tool_name(n) for n in names)
    remote = collections.Counter({t: n for t, n in every.items()
                                  if t not in set(local_tools)})
    return every, remote


def _fmt(counts):
    return ", ".join(f"{t} x{n}" for t, n in sorted(counts.items()))


def routing_failures(m, row, rows):
    """Why this replay cannot be shown to have run against the stub alone,
    as (message, retryable). Empty when it can."""
    out = []
    op = row.get("orchestration_id") or ""

    if m.get("agent") != m.get("recorded_agent"):
        out.append((f"the manifest replays {m.get('agent')} against a "
                    f"recording of {m.get('recorded_agent')}. run_replay.py "
                    "refuses that: an orchestrator on a single-agent cassette "
                    "reaches its children, unstubbed, by name.", False))
    if row.get("a2a_calls"):
        out.append((f"run {op[:12]} made {len(row['a2a_calls'])} A2A call(s). "
                    "A child reached by name runs its production version "
                    "against the live toolbox, so this replay was not fully "
                    "stubbed.", False))
    others = sorted({r.get("run_agent") for r in rows
                     if r.get("orchestration_id") == op and r is not row})
    if others:
        out.append((f"run {op[:12]} also contains {', '.join(others)}. Only "
                    "the replayed agent is stubbed; anything else in its "
                    "operation ran against real tools.", False))

    replay_toolbox = m.get("replay_toolbox")
    if replay_toolbox:
        expected = (str(replay_toolbox[0]), str(replay_toolbox[1]))
        foreign = [f"{t.get('toolbox')}@{t.get('version')}"
                   for t in row.get("mcp_toolboxes") or []
                   if (str(t.get("toolbox")), str(t.get("version"))) != expected]
        if foreign:
            out.append((f"run {op[:12]} called toolbox(es) "
                        f"{', '.join(foreign)}, not the replay's own "
                        f"{expected[0]}@{expected[1]}. Those calls did not "
                        "reach the stub.", False))

    honoured = m.get("session_honoured")
    journal = m.get("journal_tools")
    local = m.get("local_tools")
    if local is None:
        out.append(("manifest has no local_tools, so a call that never "
                    "reached the stub cannot be told from a local one. "
                    "Re-run the replay with this run_replay.py.", False))
        return out
    every, remote = tool_counts(row, local)
    if honoured is False:
        out.append(("the replay server saw calls, but not under this replay's "
                    "session, so its journal cannot be attributed to this "
                    "run and stub routing cannot be proven.", False))
        return out
    if journal is None:
        out.append(("manifest has no journal_tools, so the trace cannot be "
                    "checked against what reached the stub. Re-run the "
                    "replay with this run_replay.py.", False))
        return out
    if honoured is None and sum(remote.values()):
        out.append((f"run {op[:12]} made {sum(remote.values())} call(s) to "
                    "tools the recording reached through its MCP server, and "
                    "none reached the replay server under this replay's "
                    "session or its shared one. Either they went to "
                    "something other than the stub, or the server filed "
                    "them under a session nobody reads: list the state "
                    "container for blobs named after "
                    f"{m.get('replay_session') or 'this replay_session'}.",
                    False))
        return out

    excess = {t: n - journal.get(t, 0) for t, n in remote.items()
              if n > journal.get(t, 0)}
    if excess:
        out.append((f"run {op[:12]} made calls the replay server never saw: "
                    f"{_fmt(excess)}. They went somewhere other than the "
                    "stub. (A tool the recording never called locally counts "
                    "here too -- if the change added a local tool, re-record "
                    "the cassette. And a call the stub REFUSED is not "
                    "journalled: if the replay server's log has `REFUSED` "
                    "lines for this replay's session, its state store failed "
                    "mid-run; re-run the replay.)", False))
    missing = {t: n - every.get(t, 0) for t, n in journal.items()
               if every.get(t, 0) < n}
    if missing and not excess:
        out.append((f"run {op[:12]} is missing calls the replay server "
                    f"journalled: {_fmt(missing)}. Not fully ingested yet; "
                    "scoring it now would report them as a regression.", True))
    return out


def not_recorded(row, diverged):
    """Mark the calls the stub answered `not_recorded` as such, in place.

    The stub's answer says `not_recorded`, but the trace no longer carries
    it: a failed call has no ToolCallResult, so it arrives as `empty_failed`
    -- an avoidable call, which `no_wasted_calls` gates on. A divergence is
    the agent leaving the recorded path, which the stub cannot answer; it is
    still a tool error for `no_tool_errors`, which reports, and must not
    fail the build by itself. On 2026-10-02 every `empty_failed` of all
    seven replays was a call the journal had as diverged.

    Matched by count per tool, because the row keeps only 200 characters of
    each call's arguments. Returns how many were relabelled, per tool.
    """
    left = dict(diverged or {})
    done = collections.Counter()
    errors = []
    for e in row.get("tool_errors") or []:
        bare = tte.base_tool_name(e.get("tool"))
        if e.get("kind") == "empty_failed" and left.get(bare, 0) > 0:
            left[bare] -= 1
            done[bare] += 1
            e = dict(e, kind="not_recorded")
        errors.append(e)
    if done:
        row["tool_errors"] = errors
    return dict(done)


def normalise(row, m, key, baseline_row, tool_manifests):
    """The replayed row, presented under the recording's identity.

    Scored as observed, an unchanged agent cannot match its recording: the
    clone runs under the replay agent's name, which is baked into traj_key,
    and through a temporary toolbox, which no tool manifest covers. What was
    observed is kept under `replay`.
    """
    observed_agent = row.get("run_agent") or ""
    rekeyed = dict(row)
    rekeyed["orchestration_id"], rekeyed["run_agent"] = key

    traj = row.get("traj_key") or ""
    if traj == observed_agent or traj.startswith(observed_agent + "|"):
        rekeyed["traj_key"] = key[1] + traj[len(observed_agent):]

    recorded_toolboxes = baseline_row.get("mcp_toolboxes")
    if recorded_toolboxes is not None:
        rekeyed["mcp_toolboxes"] = recorded_toolboxes
    if tool_manifests:
        telemetry = (row.get("tool_definitions") or []
                     if "telemetry" in (row.get("tool_definitions_source") or "")
                     else [])
        defs, source = tte.resolve_tool_definitions(
            telemetry, [(t.get("toolbox"), t.get("version"))
                        for t in rekeyed.get("mcp_toolboxes") or []],
            tool_manifests)
        rekeyed.update(tool_definitions=defs, tool_definitions_source=source,
                       has_tool_definitions=bool(defs))

    relabelled = not_recorded(rekeyed, m.get("diverged_tools"))

    rekeyed["replay"] = {
        "not_recorded": relabelled,
        "operation_id": row.get("orchestration_id"),
        "agent": m["agent"],
        "replay_agent": observed_agent,
        "agent_version": str(row.get("agent_version")),
        "base_version": m.get("base_version"),
        "cassette": m.get("cassette"),
        "traj_key": traj,
        "toolboxes": row.get("mcp_toolboxes"),
    }
    return rekeyed


def attribute(manifests, rows, baselines, tool_manifests=()):
    """Pair each manifest with its one replayed row and its one baseline row.

    Returns (attributed, failures). `attributed` is a list of
    (manifest, normalised_row, baseline_row, baseline_file); `failures` a
    list of (message, retryable).
    """
    attributed, failures = [], []
    seen_in_window = sorted({(r.get("run_agent"), str(r.get("agent_version")))
                             for r in rows})
    claimed_rows, claimed_keys = {}, {}

    for path, m in manifests:
        name = os.path.basename(path)

        def fail(msg, retryable=False):
            failures.append((f"{name}: {msg}", retryable))

        absent = [k for k in REQUIRED if not m.get(k)]
        if absent:
            fail(f"manifest has no {', '.join(absent)}. It was written by a "
                 "run_replay.py older than this check, or the cassette could "
                 "not be read -- re-run the replay.")
            continue

        agent, version = m["replay_agent"], str(m["temp_version"])
        matches = [r for r in rows if r.get("run_agent") == agent
                   and str(r.get("agent_version")) == version]
        if not matches:
            shown = ", ".join(f"{a} v{v}" for a, v in seen_in_window) or "none"
            fail(f"no scored run of {agent} v{version} in the export. Runs in "
                 f"the window: {shown}. Either its spans are not ingested "
                 "yet, the replay never reached the agent, or the spans carry "
                 "a different version than the one run_replay.py created.",
                 retryable=True)
            continue
        if len(matches) > 1 and m.get("replay_toolbox"):
            own = (str(m["replay_toolbox"][0]), str(m["replay_toolbox"][1]))
            matches = [r for r in matches
                       if own in {(str(t.get("toolbox")), str(t.get("version")))
                                  for t in r.get("mcp_toolboxes") or []}]
            if not matches:
                fail(f"no scored run of {agent} v{version} called this "
                     f"replay's toolbox {own[0]}@{own[1]}. Either its spans "
                     "are not ingested yet or the replay never reached the "
                     "stub.", retryable=True)
                continue
        if len(matches) > 1:
            ops = ", ".join((r.get("orchestration_id") or "?")[:12]
                            for r in matches)
            fail(f"{len(matches)} scored runs of {agent} v{version} ({ops}). "
                 "One replay is one invocation; refusing to pick.")
            continue
        row = matches[0]
        op = row.get("orchestration_id") or ""

        problems = routing_failures(m, row, rows)
        if problems:
            for msg, retryable in problems:
                fail(msg, retryable)
            continue

        if op in claimed_rows:
            fail(f"run {op[:12]} is also attributed to {claimed_rows[op]}.")
            continue
        key = (m["recorded_orchestration_id"], m["recorded_agent"])
        if key in claimed_keys:
            fail(f"recording {key[0][:12]} ({key[1]}) is also replayed by "
                 f"{claimed_keys[key]}. Two replays of one recording collide "
                 "on one baseline key, and one would go uncompared.")
            continue
        hits = baselines.get(key, [])
        if not hits:
            fail(f"no committed baseline scores recording {key[0][:12]} "
                 f"({key[1]}), so there is nothing to compare the replay to. "
                 "Freeze one from the trace the cassette was made from "
                 "(`make baselines`, or `.\\tasks.ps1 baselines`).")
            continue
        if len(hits) > 1:
            fail(f"recording {key[0][:12]} ({key[1]}) is in {len(hits)} "
                 f"baselines ({', '.join(f for f, _ in hits)}). One baseline "
                 "per trace set; remove the superseded one.")
            continue

        claimed_rows[op], claimed_keys[key] = name, name
        baseline_file, baseline_row = hits[0]
        attributed.append((m, normalise(row, m, key, baseline_row,
                                        tool_manifests),
                           baseline_row, baseline_file))

    return attributed, failures


def normalise_spans(spans, attributed):
    """Only the replays' spans, renamed to the recording's agent and toolbox.

    For the Foundry dataset: to_foundry_dataset.py converts spans itself, and
    under the replay's names it would find no tool manifest and no
    trajectory expectation, so the detail view would score less than the
    gate did. Operation ids are left alone -- they are the replay's real
    trace, and that is what a link in Foundry should open.
    """
    plan = {}
    for m, row, _b, _f in attributed:
        recorded = row.get("mcp_toolboxes") or []
        toolbox_from = toolbox_to = None
        if m.get("replay_toolbox") and len(recorded) == 1:
            toolbox_from = (f"/toolboxes/{m['replay_toolbox'][0]}/versions/"
                            f"{m['replay_toolbox'][1]}/")
            toolbox_to = (f"/toolboxes/{recorded[0].get('toolbox')}/versions/"
                          f"{recorded[0].get('version')}/")
        plan[row["replay"]["operation_id"]] = (
            row["replay"]["replay_agent"], row["run_agent"],
            toolbox_from, toolbox_to)

    out = []
    for span in spans:
        op = _span_op(span)
        if op not in plan:
            continue
        replay_agent, recorded_agent, tb_from, tb_to = plan[op]
        span = dict(span)
        name = span.get("name") or ""
        if tb_from and tb_from in name:
            name = name.replace(tb_from, tb_to)
        if name == f"invoke_agent {replay_agent}":
            name = f"invoke_agent {recorded_agent}"
        span["name"] = name
        for field in ("customDimensions", "CustomDimensions"):
            if field not in span:
                continue
            raw = span[field]
            try:
                dims = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            except (ValueError, TypeError):
                continue                  # unparseable: leave it as exported
            if not isinstance(dims, dict):
                continue
            for k, v in dims.items():
                if v == replay_agent:
                    dims[k] = recorded_agent
            span[field] = json.dumps(dims) if isinstance(raw, str) else dims
        out.append(span)
    return out


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def report(attributed, failures, final=True, summary=None):
    """Print what was paired with what; on the last attempt, into the job
    summary too, so a red build names the replay that could not be judged."""
    lines = []
    if attributed:
        lines += ["| Cassette | Agent | Base → temp | Replay run | Baseline |",
                  "|---|---|---|---|---|"]
        for m, row, _b, bfile in attributed:
            lines.append(
                f"| `{m.get('cassette')}` | {m['agent']} | "
                f"v{m.get('base_version')} → v{m['temp_version']} | "
                f"`{(row['replay']['operation_id'] or '')[:12]}` | "
                f"`{bfile}` |")
    if failures:
        lines += ["", "**Could not attribute every replay, so nothing was "
                      "gated:**", ""]
        lines += [f"- {msg}" for msg, _ in failures]

    print("\n".join(lines).replace("**", "").replace("`", ""))
    if summary and final:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Replay attribution\n\n" + "\n".join(lines) + "\n\n")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifests", nargs="+",
                    help="run_replay.py --manifest files; globs are expanded "
                         "here, so a quoted pattern works on every shell")
    ap.add_argument("--runs", required=True,
                    help="eval_runs.jsonl converted from the export window")
    ap.add_argument("--baselines", default=os.path.join(REPO_ROOT, "baselines"),
                    help="directory of committed baselines")
    ap.add_argument("--tool-defs", action="append", metavar="PATH",
                    help="tool manifests, to resolve the recording's tool "
                         "definitions for the replayed row")
    ap.add_argument("--out-runs", required=True,
                    help="the replayed rows, under their recordings' identity")
    ap.add_argument("--out-baseline", required=True,
                    help="the baseline rows those recordings are compared to")
    ap.add_argument("--spans", help="the exported window")
    ap.add_argument("--out-spans",
                    help="only the replays' spans, renamed to the recordings' "
                         "agent and toolbox, for the Foundry dataset")
    ap.add_argument("--summary", help="append the table here too, e.g. "
                                      "$GITHUB_STEP_SUMMARY")
    ap.add_argument("--final", action="store_true",
                    help="the caller will not retry, so a retryable failure "
                         "is reported in --summary like any other")
    args = ap.parse_args(argv)
    if bool(args.spans) != bool(args.out_spans):
        ap.error("--spans and --out-spans go together")

    paths = expand(args.manifests)
    if not paths:
        print(f"no manifest matched {' '.join(args.manifests)} -- no replay "
              "ran, so nothing can be gated", file=sys.stderr)
        return EXIT_FAILED
    manifests = [(p, load_json(p)) for p in paths]
    rows = load_rows(args.runs)
    tool_manifests = (tte.load_tool_manifests(args.tool_defs)
                      if args.tool_defs else [])
    attributed, failures = attribute(manifests, rows,
                                     index_baselines(args.baselines),
                                     tool_manifests)
    retry = bool(failures) and all(r for _, r in failures)
    report(attributed, failures, final=args.final or not retry,
           summary=args.summary)
    if failures:
        return EXIT_NOT_YET if retry else EXIT_FAILED

    # Everything computed before anything is written: a half-written set of
    # outputs from an earlier attempt is exactly what a retry must not score.
    spans = (normalise_spans(load_json(args.spans), attributed)
             if args.spans else None)
    write_jsonl(args.out_runs, [row for _m, row, _b, _f in attributed])
    write_json(args.out_baseline, [b for _m, _r, b, _f in attributed])
    if args.spans:
        write_json(args.out_spans, spans)
        print(f"\n{len(spans)} span(s) from {len(attributed)} replay run(s) "
              f"-> {args.out_spans}")
    print(f"{len(attributed)} replay(s) attributed -> {args.out_runs}, "
          f"baseline -> {args.out_baseline}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
