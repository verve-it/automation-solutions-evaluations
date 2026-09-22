#!/usr/bin/env python3
"""
run_evals.py — score converted agent runs with deterministic checks.

No judge model, no Foundry project, no network. Reads the JSONL produced by
trace_to_eval.py and prints pass/fail per run plus a summary.

    python3 trace_to_eval.py spans.json -o ./out
    python3 run_evals.py ./out/eval_runs.jsonl
    python3 run_evals.py ./out/eval_runs.jsonl --expected expected.json \
                         --baseline baselines/full-triage-2026-09-18.json \
                         --json artifacts/run.json

Every check here is computable from the trace alone. They exist because each
one caught something real in the first traces we looked at. Add checks as you
find new failure modes: write a function, add it to CHECKS, done.

`expected.json` (optional) holds ground-truth tool sequences keyed
"<agent>|<intent>"; a bare "<agent>" key applies to every intent:

    {
      "triage-analysis-agent|Full Triage": [
        "load_skill", "load_skill",
        "ConnectWise-PSA-ForAgents___cw_get_ticket",
        "ConnectWise-PSA-ForAgents___cw_follow_href",
        "ConnectWise-PSA-ForAgents___cw_query"
      ]
    }

Matching is in-order-with-extras (the documented `in_order_match` mode): every
expected step must appear in order; additional steps are allowed but counted.
"""

from __future__ import annotations
import argparse, json, os, sys
from collections import Counter, defaultdict

from trace_to_eval import base_tool_name

# ---------------------------------------------------------------- helpers

def _fail(msg, **extra):
    return {"passed": False, "reason": msg, **extra}


def _pass(msg="", **extra):
    return {"passed": True, "reason": msg, **extra}


def _skip(msg):
    return {"passed": None, "reason": msg}


# ---------------------------------------------------------------- checks

def check_no_wasted_calls(run, cfg):
    """
    Tool calls that could not have succeeded.

    Seen live: run_skill_script with script_name "?" and "none" against skills
    whose manifest shows <available_scripts /> empty. Three guaranteed failures
    and ~6s per triage.
    """
    bad = [t for t in run.get("tool_errors", [])
           if t.get("kind") in ("missing_script", "invalid_reference_type",
                                "invalid_entity", "invalid_projection_field",
                                "empty_failed")]
    if not bad:
        return _pass()
    kinds = Counter(t["kind"] for t in bad)
    detail = ", ".join(f"{k}x{v}" for k, v in kinds.most_common())
    return _fail(f"{len(bad)} avoidable call(s): {detail}", count=len(bad))


def check_no_tool_errors(run, cfg):
    """Any tool error at all. Broader than the above; tracks overall health."""
    errs = run.get("tool_errors", [])
    n, total = len(errs), run.get("tool_call_count", 0)
    if not total:
        return _skip("no tool calls")
    if not errs:
        return _pass()
    return _fail(f"{n}/{total} tool calls errored", count=n,
                 rate=round(n / total, 2))


def check_no_dead_ends(run, cfg):
    """
    Calls that succeed but return nothing — the hallucinated-entity signal.

    Seen live: the ops agent resolved company "A.S. Economou Development" and
    id 4597, both zero matches, then queried companies directly — also empty.
    The whole write failed because an upstream agent proposed a company that
    does not exist in ConnectWise.
    """
    empties = run.get("empty_results", [])
    total = run.get("tool_call_count", 0)
    if not total:
        return _skip("no tool calls")
    limit = cfg.get("max_empty_rate", 0.25)
    rate = len(empties) / total
    if rate <= limit:
        return _pass()
    tools = ", ".join(sorted({e["tool"] for e in empties})[:3])
    return _fail(f"{len(empties)}/{total} calls returned nothing ({tools})",
                 rate=round(rate, 2))


def check_no_search_cascade(run, cfg):
    """
    Repeated calls to one tool with degrading arguments.

    A distinct signature from one bad call, and the one that burns the most
    time: nine consecutive empty cw_resolve calls is an agent guessing at a
    vocabulary it has no schema for, not a single mistake. Informational until
    the tool manifest lands and the root cause is fixable.
    """
    cascades = run.get("search_cascades", [])
    if not run.get("tool_call_count"):
        return _skip("no tool calls")
    if not cascades:
        return _pass()
    worst = max(cascades, key=lambda c: c["length"])
    return _fail(
        f"{len(cascades)} cascade(s), longest {worst['length']}x "
        f"{base_tool_name(worst['tool'])}",
        count=len(cascades), longest=worst["length"])


def check_trajectory(run, cfg):
    """
    In-order match against a ground-truth tool sequence, with extras allowed.
    Reports precision / recall / F1 the same way Task Navigation Efficiency
    does, so swapping to the Foundry evaluator later changes the runner, not
    the numbers.

    Expectations are keyed "<agent>|<intent>" because the same agent has
    different correct paths per intent — the orchestrator routes to a child
    for Full Triage but goes straight to ConnectwiseMCP for an Information
    Request. A bare "<agent>" key still works and applies to every intent.
    """
    exp_map = cfg.get("expected") or {}
    agent, intent = run["run_agent"], run.get("intent")
    key = run.get("traj_key") or agent
    expected = exp_map.get(key) or exp_map.get(agent)
    if not expected:
        want = f"{agent}|{intent}" if intent else agent
        return _skip(f"no expected_actions for {want}")
    actual = run.get("tool_names", [])

    i, matched = 0, []
    for step in actual:
        if i < len(expected) and step == expected[i]:
            matched.append(step)
            i += 1
    recall = len(matched) / len(expected) if expected else 0.0
    precision = len(matched) / len(actual) if actual else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    stats = {"precision": round(precision, 2), "recall": round(recall, 2),
             "f1": round(f1, 2)}
    if recall == 1.0:
        extra = len(actual) - len(matched)
        note = f"matched, {extra} extra step(s)" if extra else "exact"
        return _pass(note, **stats)
    missing = expected[i:]
    return _fail(f"missing {missing[:3]}", **stats)


def check_no_truncation(run, cfg):
    """Results at exactly 8192 chars are cut mid-payload, so the agent
    reasoned over incomplete data. A data-quality gate, not a model gate.

    In the frozen sets every truncation is a `cw_query` result — not
    `load_skill`, which is the largest payload but survives intact past 8192
    because gen_ai.* attributes are largely exempt from the cap. A truncated
    *skill* would be worse than a truncated result, so it is called out
    separately if it ever happens.
    """
    n = run.get("truncated_results", 0)
    skills = run.get("truncated_skills", 0)
    if not n:
        return _pass()
    detail = f"{n} tool result(s) truncated at 8192 chars"
    if skills:
        detail += f" — {skills} of them a SKILL (incomplete rules, not data)"
    return _fail(detail, count=n, skills=skills)


def check_cost_latency(run, cfg):
    """Spend and wall-clock per run.

    Tracking, not gating: the numbers are printed every run and stored in the
    JSON artifact so they trend, and this only returns a verdict when a
    threshold is actually set. Gate once you know what normal looks like.
    """
    usage = run.get("usage", {})
    tokens = usage.get("uncached_input_tokens", 0) + usage.get("output_tokens", 0)
    duration = run.get("duration_ms", 0)
    max_tokens = cfg.get("max_tokens")
    max_ms = cfg.get("max_duration_ms")
    if not (max_tokens or max_ms):
        return _skip("tracking only — no --max-tokens/--max-duration-ms set")

    over = []
    if max_tokens and tokens > max_tokens:
        over.append(f"{tokens:,} tokens > {max_tokens:,}")
    if max_ms and duration > max_ms:
        over.append(f"{duration / 1000:.1f}s > {max_ms / 1000:.1f}s")
    if over:
        return _fail("; ".join(over), tokens=tokens, duration_ms=duration)
    return _pass(f"{tokens:,} tokens, {duration / 1000:.1f}s",
                 tokens=tokens, duration_ms=duration)


# ------------------------------------------------- generated arg validation

_TYPES = {
    "string": str, "number": (int, float), "integer": int,
    "boolean": bool, "object": dict, "array": list, "null": type(None),
}


def _type_ok(value, declared):
    types = declared if isinstance(declared, list) else [declared]
    for t in types:
        py = _TYPES.get(t)
        if py is None:
            return True                      # unknown keyword: do not judge
        if t == "integer" and isinstance(value, bool):
            continue
        if t in ("number", "integer") and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def validate_args(args, schema):
    """A deliberately small JSON-Schema subset: the six things Tool Input
    Accuracy checks, done deterministically. Returns a list of problems."""
    problems = []
    if not isinstance(schema, dict):
        return problems
    props = schema.get("properties") or {}

    for name in schema.get("required") or []:
        if name not in args:
            problems.append(f"missing required '{name}'")

    if schema.get("additionalProperties") is False:
        for name in args:
            if name not in props:
                problems.append(f"unexpected '{name}'")

    for name, value in args.items():
        spec = props.get(name)
        if not isinstance(spec, dict):
            continue
        if "type" in spec and not _type_ok(value, spec["type"]):
            problems.append(
                f"'{name}' should be {spec['type']}, got "
                f"{type(value).__name__}")
        if "enum" in spec and value not in spec["enum"]:
            # Show a prefix, but never let it read as the whole enum: with 20
            # reference types a bare six-value list looks like the valid set.
            head = [str(v) for v in spec["enum"][:6]]
            rest = len(spec["enum"]) - len(head)
            allowed = ", ".join(head) + (f", +{rest} more" if rest > 0 else "")
            problems.append(f"'{name}'={value!r} not in [{allowed}]")
    return problems


def _schema_index(run):
    """tool name (bare) -> parameter schema, for every definition that has one."""
    index = {}
    defs = run.get("tool_definitions")
    if not isinstance(defs, list):
        return index
    for d in defs:
        if not isinstance(d, dict):
            continue
        params = d.get("parameters") or d.get("inputSchema")
        if isinstance(params, dict) and params.get("properties") is not None:
            index[base_tool_name(d.get("name", ""))] = params
    return index


def check_valid_tool_args(run, cfg):
    """
    Argument validation generated from the tool schema rather than written.

    This is the whole point of the MCP manifest: every cw_resolve failure in
    the baseline would have been caught here, and so would every future one,
    across every agent and every flow, with nobody writing a check. Skips
    cleanly until a manifest exists — see tool_manifests/README.md.
    """
    index = _schema_index(run)
    if not index:
        return _skip("no tool schemas (run the converter with --tool-defs)")

    checked, problems = 0, []
    for action in run.get("actions", []):
        for part in action.get("content", []):
            name = base_tool_name(part.get("name", ""))
            schema = index.get(name)
            if schema is None:
                continue
            try:
                args = json.loads(part.get("arguments") or "{}")
            except json.JSONDecodeError:
                problems.append(f"{name}: arguments are not JSON")
                continue
            if not isinstance(args, dict):
                problems.append(f"{name}: arguments are not an object")
                continue
            checked += 1
            for p in validate_args(args, schema):
                problems.append(f"{name}: {p}")

    if not checked:
        return _skip(f"no call matched a schema ({len(index)} tool(s) known)")
    if not problems:
        return _pass(f"{checked} call(s) validated", checked=checked)
    shown = "; ".join(problems[:3])
    return _fail(f"{len(problems)} bad argument(s) in {checked} validated "
                 f"call(s): {shown}", count=len(problems), checked=checked)


def check_has_evaluator_inputs(run, cfg):
    """Would this run even be scorable by the Foundry evaluators? Tracks
    dataset readiness rather than agent behaviour."""
    missing = [k for k in ("has_query", "has_response", "has_tool_definitions")
               if not run.get(k)]
    if not missing:
        return _pass()
    return _fail("missing " + ", ".join(m.replace("has_", "") for m in missing))


CHECKS = {
    "no_wasted_calls":      check_no_wasted_calls,
    "valid_tool_args":      check_valid_tool_args,
    "no_tool_errors":       check_no_tool_errors,
    "no_dead_ends":         check_no_dead_ends,
    "no_search_cascade":    check_no_search_cascade,
    "trajectory":           check_trajectory,
    "no_truncation":        check_no_truncation,
    "cost_latency":         check_cost_latency,
    "evaluator_ready":      check_has_evaluator_inputs,
}

# Checks that gate a release vs. checks that are informational for now.
# Move checks between the two as you learn what is actionable.
GATING = {"no_wasted_calls", "no_dead_ends", "trajectory", "valid_tool_args"}


# ---------------------------------------------------------------- runner

def score(runs, cfg):
    rows = []
    for run in runs:
        res = {name: fn(run, cfg) for name, fn in CHECKS.items()}
        gating = [n for n in GATING if res[n]["passed"] is False]
        rows.append({
            "orchestration_id": run["orchestration_id"],
            "run_agent": run["run_agent"],
            "intent": run.get("intent"),
            "intent_source": run.get("intent_source", ""),
            "traj_key": run.get("traj_key") or run["run_agent"],
            "started": run.get("started", ""),
            "mcp_toolboxes": run.get("mcp_toolboxes", []),
            "tool_calls": run.get("tool_call_count", 0),
            "duration_ms": run.get("duration_ms", 0),
            "usage": run.get("usage", {}),
            "skills_in_force": run.get("skills_in_force", []),
            "checks": res,
            "passed": not gating,
            "failed_gating": gating,
        })
    return rows


def print_report(rows):
    if not rows:
        print("no runs to score")
        return
    w = max(len(r["traj_key"]) for r in rows) + 2
    print(f"\n{'AGENT [INTENT]':<{w}}{'TOOLS':>6}  {'RESULT':<8} CHECKS")
    print("-" * (w + 60))
    for r in rows:
        flags = [f"{name}:FAIL" for name, c in r["checks"].items()
                 if c["passed"] is False]
        verdict = "PASS" if r["passed"] else "FAIL"
        print(f"{r['traj_key']:<{w}}{r['tool_calls']:>6}  {verdict:<8} "
              f"{', '.join(flags) if flags else 'all clear'}")

    print("\nDETAIL")
    for r in rows:
        bad = {n: c for n, c in r["checks"].items() if c["passed"] is False}
        if not bad:
            continue
        print(f"\n  {r['traj_key']}  ({r['orchestration_id'][:12]})")
        for n, c in bad.items():
            gate = "gating" if n in GATING else "info"
            print(f"    [{gate:>6}] {n}: {c['reason']}")

    # Skips are how coverage goes missing without anything turning red: an
    # expectation keyed for an intent the converter no longer resolves reads
    # as "nothing to report" rather than as a gap. Print them.
    skipped = defaultdict(list)
    for r in rows:
        for n, c in r["checks"].items():
            if c["passed"] is None:
                skipped[f"{n}: {c['reason']}"].append(r["traj_key"])
    if skipped:
        print("\nNOT SCORED")
        for reason, who in sorted(skipped.items()):
            print(f"  {reason}  ({len(who)} run(s))")

    total = len(rows)
    passed = sum(1 for r in rows if r["passed"])
    print(f"\nSUMMARY  {passed}/{total} runs pass gating checks")
    tally = defaultdict(lambda: [0, 0])
    for r in rows:
        for n, c in r["checks"].items():
            if c["passed"] is None:
                continue
            tally[n][1] += 1
            if c["passed"]:
                tally[n][0] += 1
    for n in CHECKS:
        ok, tot = tally.get(n, [0, 0])
        gate = "*" if n in GATING else " "
        scored = f"{ok}/{tot}" if tot else "not scored"
        print(f"  {gate} {n:<20} {scored}")
    print("\n  * = gating check")

    print_tracking(rows)
    print_skill_drift(rows)


def print_tracking(rows):
    """Cost and latency per run. Not gated — see check_cost_latency — but
    always printed and always in the JSON artifact, so it trends from day one.
    Token figures are uncached input plus output; the sum of per-turn prompts
    is not a spend figure.
    """
    # Gate on there being usage at all, not on llm_calls. llm_calls counts
    # chat spans, and is 0 whenever the figures came from the invoke_agent
    # roll-up instead -- exactly the runs whose spend you still want to see.
    if not any(_has_usage(r) for r in rows):
        return
    w = max(len(r["traj_key"]) for r in rows) + 2
    print(f"\nTRACKING (not gated)\n{'AGENT [INTENT]':<{w}}"
          f"{'LLM':>5}{'UNCACHED IN':>13}{'CACHED':>11}{'OUT':>8}"
          f"{'PEAK CTX':>10}{'WALL':>8}")
    print("-" * (w + 55))
    tot_in = tot_out = tot_cached = 0
    for r in rows:
        u = r.get("usage", {})
        tot_in += u.get("uncached_input_tokens", 0)
        tot_out += u.get("output_tokens", 0)
        tot_cached += u.get("cache_read_tokens", 0)
        print(f"{r['traj_key']:<{w}}{u.get('llm_calls', 0):>5}"
              f"{u.get('uncached_input_tokens', 0):>13,}"
              f"{u.get('cache_read_tokens', 0):>11,}"
              f"{u.get('output_tokens', 0):>8,}"
              f"{u.get('peak_input_tokens', 0):>10,}"
              f"{r.get('duration_ms', 0) / 1000:>7.1f}s")
    print(f"{'TOTAL':<{w}}{'':>5}{tot_in:>13,}{tot_cached:>11,}{tot_out:>8,}")


USAGE_FIELDS = ("llm_calls", "uncached_input_tokens", "cache_read_tokens",
                "output_tokens", "prompt_tokens_sum")


def _has_usage(row):
    u = row.get("usage") or {}
    return any(u.get(f) for f in USAGE_FIELDS)


def print_skill_drift(rows):
    """Same skill, different content, across runs being compared.

    Printed regardless of usage: it was inside print_tracking and shared its
    early return, so a set of runs whose tokens came from the roll-up
    reported no drift rather than no usage.
    """
    # A skill file cut mid-payload means the agent worked from incomplete
    # rules, which is a different failure from incomplete data.
    hashes = defaultdict(set)
    for r in rows:
        for s in r.get("skills_in_force", []):
            if not s.get("truncated"):
                hashes[s["skill_name"]].add(s["sha256"][:12])
    drifted = {k: v for k, v in hashes.items() if len(v) > 1}
    if drifted:
        print("\nSKILL DRIFT — same skill, different content across these runs")
        for name, digests in sorted(drifted.items()):
            print(f"  {name}: {', '.join(sorted(digests))}")


def _key(row):
    return (row["orchestration_id"], row["run_agent"])


def diff_baseline(rows, baseline):
    """Compare a scored run against a frozen baseline.

    Returns (regressions, fixes, lost, new_runs, missing_runs). A regression is
    a check that passed in the baseline and fails now — the thing that should
    stop a release. `lost` is a check that used to produce a verdict and now
    skips: coverage disappearing, which looks like silence rather than a
    failure and is exactly how the intent-keying break went unnoticed.
    """
    base = {_key(r): r for r in baseline}
    cur = {_key(r): r for r in rows}

    regressions, fixes, lost = [], [], []
    for k, r in cur.items():
        b = base.get(k)
        if not b:
            continue
        for name, c in r["checks"].items():
            was = b["checks"].get(name, {}).get("passed")
            now = c["passed"]
            if was is True and now is False:
                regressions.append((k, name, c["reason"]))
            elif was is False and now is True:
                fixes.append((k, name, b["checks"][name]["reason"]))
            elif was is not None and now is None:
                lost.append((k, name, c["reason"]))

    new_runs = [k for k in cur if k not in base]
    missing = [k for k in base if k not in cur]
    return regressions, fixes, lost, new_runs, missing


def print_diff(regressions, fixes, lost, new_runs, missing):
    print("\n" + "=" * 60)
    print("BASELINE DIFF")
    print("=" * 60)
    if regressions:
        print(f"\nREGRESSED ({len(regressions)})")
        for (op, agent), name, reason in regressions:
            print(f"  {agent} [{op[:12]}]")
            print(f"    {name}: {reason}")
    if lost:
        print(f"\nLOST COVERAGE ({len(lost)}) — scored in baseline, skipped now")
        for (op, agent), name, reason in lost:
            print(f"  {agent} [{op[:12]}]")
            print(f"    {name}: {reason}")
    if fixes:
        print(f"\nFIXED ({len(fixes)})")
        for (op, agent), name, reason in fixes:
            print(f"  {agent} [{op[:12]}]  {name}  (was: {reason})")
    if new_runs:
        print(f"\nNEW RUNS ({len(new_runs)}) — not in baseline, not compared")
        for op, agent in new_runs:
            print(f"  {agent} [{op[:12]}]")
    if missing:
        print(f"\nMISSING ({len(missing)}) — in baseline, absent now")
        for op, agent in missing:
            print(f"  {agent} [{op[:12]}]")
    if not (regressions or fixes or lost or new_runs or missing):
        print("\nno change against baseline")
    print()


def score_rates(rows):
    """Overall pass rate, and each check's rate over the runs it applied to.

    A check is tri-state: True, False, or None for "did not apply" --
    `cost_latency` with no thresholds set, `valid_tool_args` with no schema
    for the tool. Counting None as either verdict is wrong in both
    directions, so it is excluded from that check's denominator and reported
    separately.

    The row-level verdict comes from GATING only; the other checks report
    without blocking. A summary that mixed them would show a red build
    alongside a check that had nothing to do with it.
    """
    total = len(rows)
    overall = (sum(1 for r in rows if r["passed"]) / total) if total else 0.0

    tally = {}
    for row in rows:
        for name, result in (row.get("checks") or {}).items():
            verdict = (result or {}).get("passed")
            passed, applied, skipped = tally.get(name, (0, 0, 0))
            if verdict is None:
                tally[name] = (passed, applied, skipped + 1)
            else:
                tally[name] = (passed + (1 if verdict else 0), applied + 1,
                               skipped)

    per_check = {name: {"rate": (p / a if a else None), "applied": a,
                        "skipped": s, "gating": name in GATING}
                 for name, (p, a, s) in tally.items()}
    return overall, per_check


def print_thresholds(rows, min_score, min_check_score):
    """Absolute floors. Returns True if any was missed."""
    overall, per_check = score_rates(rows)
    failed = False

    if min_score is not None:
        ok = overall >= min_score
        failed |= not ok
        print(f"\nthreshold  overall {overall:.1%} "
              f"{'>=' if ok else '<'} {min_score:.1%}  "
              f"{'ok' if ok else 'FAIL'}")

    if min_check_score is not None:
        # Only gating checks can fail a build, so only they are held to the
        # floor. Holding a reporting check to it would fail a deploy for
        # something the gate itself does not treat as a failure.
        short = {n: c["rate"] for n, c in per_check.items()
                 if c["gating"] and c["rate"] is not None
                 and c["rate"] < min_check_score}
        failed |= bool(short)
        print(f"threshold  each gating check >= {min_check_score:.1%}  "
              f"{'ok' if not short else 'FAIL'}")
        for name, rate in sorted(short.items(), key=lambda kv: kv[1]):
            print(f"             {name:<22} {rate:.1%}")
    return failed


def write_summary(path, rows, baseline_path, min_score, min_check_score):
    """A table a builder can read when the build is red.

    An exit code says a gate failed; it does not say which check, on how many
    runs, or whether it was already failing. That is the difference between a
    red build someone fixes and a red build someone reruns.

    Appends, because $GITHUB_STEP_SUMMARY accumulates across steps.
    """
    overall, per_check = score_rates(rows)
    passing = sum(1 for r in rows if r["passed"])
    lines = ["## Agent evaluation", ""]
    lines.append(f"**{passing} of {len(rows)} runs passed every gating check "
                 f"— {overall:.0%}**")
    if min_score is not None:
        lines.append(f"Threshold {min_score:.0%} — "
                     f"**{'PASS' if overall >= min_score else 'FAIL'}**")

    lines += ["", "| Check | Gates? | Pass rate | Failing | N/A |",
              "|---|---|---|---|---|"]
    for name, c in sorted(per_check.items(),
                          key=lambda kv: (not kv[1]["gating"],
                                          kv[1]["rate"] is None,
                                          kv[1]["rate"] or 0)):
        rate = "n/a" if c["rate"] is None else f"{c['rate']:.0%}"
        if (min_check_score is not None and c["gating"]
                and c["rate"] is not None and c["rate"] < min_check_score):
            rate += " :warning:"
        failing = sum(1 for r in rows
                      if ((r.get("checks") or {}).get(name)
                          or {}).get("passed") is False)
        lines.append(f"| `{name}` | {'yes' if c['gating'] else 'no'} | "
                     f"{rate} | {failing} | {c['skipped']} |")

    failing_rows = [r for r in rows if not r["passed"]]
    if failing_rows:
        lines += ["", "### Runs that failed a gating check", "",
                  "| Agent | Intent | Failed |", "|---|---|---|"]
        for row in failing_rows[:25]:
            bad = ", ".join(f"`{n}`" for n in (row.get("failed_gating") or []))
            lines.append(f"| {row.get('run_agent', '?')} | "
                         f"{row.get('intent') or '?'} | {bad} |")
        if len(failing_rows) > 25:
            lines.append(f"| … | | {len(failing_rows) - 25} more |")

    lines += ["", "Checks marked *no* report but never fail the build. "
                  "`n/a` means the check did not apply to that run — no "
                  "schema for the tool, or no threshold configured."]
    if baseline_path:
        lines.append(f"Compared against `{os.path.basename(baseline_path)}`: "
                     "a run that was already failing does not fail the build, "
                     "a regression does.")

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", help="eval_runs.jsonl from trace_to_eval.py")
    ap.add_argument("--expected", help="JSON map of traj_key -> expected tools")
    ap.add_argument("--json", help="write full results here")
    ap.add_argument("--baseline", help="frozen results to diff against")
    ap.add_argument("--max-empty-rate", type=float, default=0.25)
    ap.add_argument("--max-tokens", type=int,
                    help="uncached input + output per run; unset = track only")
    ap.add_argument("--max-duration-ms", type=int,
                    help="wall clock per run; unset = track only")
    ap.add_argument("--min-score", type=float, metavar="FRACTION",
                    help="fail unless at least this fraction of runs pass "
                         "every check. An absolute floor, for gating a "
                         "deployment; --baseline gates on regression instead, "
                         "and the two compose.")
    ap.add_argument("--min-check-score", type=float, metavar="FRACTION",
                    help="fail unless EACH check passes on at least this "
                         "fraction of runs. Catches one check collapsing "
                         "while the overall rate stays high.")
    ap.add_argument("--summary", metavar="PATH",
                    help="write a markdown summary here — point it at "
                         "$GITHUB_STEP_SUMMARY so a red build shows the "
                         "scores instead of only the exit code.")
    ap.add_argument("--allow-lost-coverage", action="store_true",
                    help="do not fail when a check that used to score now "
                         "skips (use when intentionally retiring a check)")
    args = ap.parse_args()

    runs = [json.loads(l) for l in open(args.jsonl, encoding="utf-8") if l.strip()]
    cfg = {"max_empty_rate": args.max_empty_rate, "expected": {},
           "max_tokens": args.max_tokens,
           "max_duration_ms": args.max_duration_ms}
    if args.expected:
        cfg["expected"] = {
            k: v for k, v in
            json.load(open(args.expected, encoding="utf-8")).items()
            if not k.startswith("_")
        }

    rows = score(runs, cfg)
    print_report(rows)

    failed_diff = False
    if args.baseline:
        baseline = json.load(open(args.baseline, encoding="utf-8"))
        regressions, fixes, lost, new_runs, missing = diff_baseline(rows, baseline)
        print_diff(regressions, fixes, lost, new_runs, missing)
        failed_diff = bool(regressions) or bool(lost and
                                                not args.allow_lost_coverage)

    failed_threshold = False
    if args.min_score is not None or args.min_check_score is not None:
        failed_threshold = print_thresholds(rows, args.min_score,
                                            args.min_check_score)

    if args.summary:
        write_summary(args.summary, rows, args.baseline, args.min_score,
                      args.min_check_score)

    if args.json:
        parent = os.path.dirname(os.path.abspath(args.json))
        os.makedirs(parent, exist_ok=True)
        json.dump(rows, open(args.json, "w", encoding="utf-8"),
                  indent=1, ensure_ascii=False)
        print(f"wrote {args.json}")

    # With a baseline, only regressions (and lost coverage) fail the build — a
    # run that was already failing stays failing without blocking unrelated
    # work. Gate on delta while known issues are open, or the suite is red
    # permanently and people route around it.
    # A threshold and a baseline answer different questions -- "is this good
    # enough to ship" and "is this worse than last time" -- so either can fail
    # the build and neither overrides the other.
    if failed_threshold:
        return 1
    if args.baseline:
        return 1 if failed_diff else 0
    return 0 if all(r["passed"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
