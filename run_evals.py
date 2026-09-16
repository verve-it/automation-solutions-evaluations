#!/usr/bin/env python3
"""
run_evals.py — score converted agent runs with deterministic checks.

No judge model, no Foundry project, no network. Reads the JSONL produced by
trace_to_eval.py and prints pass/fail per run plus a summary.

    python3 trace_to_eval.py spans.json -o ./out
    python3 run_evals.py ./out/eval_runs.jsonl
    python3 run_evals.py ./out/eval_runs.jsonl --expected expected.json --json results.json

Every check here is computable from the trace alone. They exist because each
one caught something real in the first traces we looked at. Add checks as you
find new failure modes: write a function, add it to CHECKS, done.

`expected.json` (optional) holds ground-truth tool sequences per agent:

    {
      "triage-analysis-agent": [
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

# ---------------------------------------------------------------- helpers

def _results(run):
    """Tool results aren't on the run row; checks that need them read the
    trajectory fields the converter preserved."""
    return run.get("tool_results", [])


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
    reasoned over incomplete data. A data-quality gate, not a model gate."""
    n = run.get("truncated_results", 0)
    if not n:
        return _pass()
    return _fail(f"{n} tool result(s) truncated at 8192 chars", count=n)


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
    "no_tool_errors":       check_no_tool_errors,
    "no_dead_ends":         check_no_dead_ends,
    "trajectory":           check_trajectory,
    "no_truncation":        check_no_truncation,
    "evaluator_ready":      check_has_evaluator_inputs,
}

# Checks that gate a release vs. checks that are informational for now.
GATING = {"no_wasted_calls", "no_dead_ends", "trajectory"}


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
            "traj_key": run.get("traj_key") or run["run_agent"],
            "started": run.get("started", ""),
            "tool_calls": run.get("tool_call_count", 0),
            "checks": res,
            "passed": not gating,
            "failed_gating": gating,
        })
    return rows


def print_report(rows):
    if not rows:
        print("no runs to score")
        return
    w = max(len(r["run_agent"]) + (len(r.get("intent") or "") + 4
            if r.get("intent") else 0) for r in rows) + 2
    print(f"\n{'AGENT':<{w}}{'TOOLS':>6}  {'RESULT':<8} CHECKS")
    print("-" * (w + 60))
    for r in rows:
        flags = []
        for name, c in r["checks"].items():
            mark = {True: "ok", False: "FAIL", None: "--"}[c["passed"]]
            if c["passed"] is False:
                flags.append(f"{name}:{mark}")
        verdict = "PASS" if r["passed"] else "FAIL"
        label = r["run_agent"] + (f"  [{r['intent']}]" if r.get("intent") else "")
        print(f"{label:<{w}}{r['tool_calls']:>6}  {verdict:<8} "
              f"{', '.join(flags) if flags else 'all clear'}")

    print("\nDETAIL")
    for r in rows:
        bad = {n: c for n, c in r["checks"].items() if c["passed"] is False}
        if not bad:
            continue
        print(f"\n  {r['run_agent']}  ({r['orchestration_id'][:12]})")
        for n, c in bad.items():
            gate = "gating" if n in GATING else "info"
            print(f"    [{gate:>6}] {n}: {c['reason']}")

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
    for n, (ok, tot) in sorted(tally.items()):
        gate = "*" if n in GATING else " "
        print(f"  {gate} {n:<20} {ok}/{tot}")
    print("\n  * = gating check")


def _key(row):
    return (row["orchestration_id"], row["run_agent"])


def diff_baseline(rows, baseline):
    """Compare a scored run against a frozen baseline.

    Returns (regressions, fixes, new_runs, missing_runs). A regression is a
    check that passed in the baseline and fails now — that is the thing that
    should stop a release. Fixes are the reverse and worth reporting so an
    improvement is visible rather than silent.
    """
    base = {_key(r): r for r in baseline}
    cur = {_key(r): r for r in rows}

    regressions, fixes = [], []
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

    new_runs = [k for k in cur if k not in base]
    missing = [k for k in base if k not in cur]
    return regressions, fixes, new_runs, missing


def print_diff(regressions, fixes, new_runs, missing):
    print("\n" + "=" * 60)
    print("BASELINE DIFF")
    print("=" * 60)
    if regressions:
        print(f"\nREGRESSED ({len(regressions)})")
        for (op, agent), name, reason in regressions:
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
    if not (regressions or fixes or new_runs or missing):
        print("\nno change against baseline")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", help="eval_runs.jsonl from trace_to_eval.py")
    ap.add_argument("--expected", help="JSON map of agent -> expected tool list")
    ap.add_argument("--json", help="write full results here")
    ap.add_argument("--baseline", help="frozen results to diff against")
    ap.add_argument("--max-empty-rate", type=float, default=0.25)
    args = ap.parse_args()

    runs = [json.loads(l) for l in open(args.jsonl, encoding="utf-8") if l.strip()]
    cfg = {"max_empty_rate": args.max_empty_rate, "expected": {}}
    if args.expected:
        cfg["expected"] = json.load(open(args.expected, encoding="utf-8"))

    rows = score(runs, cfg)
    print_report(rows)

    regressed = False
    if args.baseline:
        baseline = json.load(open(args.baseline, encoding="utf-8"))
        regressions, fixes, new_runs, missing = diff_baseline(rows, baseline)
        print_diff(regressions, fixes, new_runs, missing)
        regressed = bool(regressions)

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        json.dump(rows, open(args.json, "w", encoding="utf-8"),
                  indent=1, ensure_ascii=False)
        print(f"wrote {args.json}")

    # With a baseline, only regressions fail the build — a run that was
    # already failing stays failing without blocking unrelated work.
    if args.baseline:
        return 1 if regressed else 0
    return 0 if all(r["passed"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())