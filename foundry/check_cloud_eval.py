#!/usr/bin/env python3
"""
check_cloud_eval.py — wait for a Foundry evaluation run, then diff its scores
against the local checks.

    python3 check_cloud_eval.py eval_dbe9953d... evalrun_c294d7d3... \\
        --trace traces/2026-09-03-full-triage.json --expected expected.json

This is the test that matters. The ported evaluators agree with
`run_evals.py` on this machine — `tests/test_foundry_evaluators.py` proves 56
verdicts match — but that says nothing about how the inlined `code_text`
behaves in Foundry's sandbox, or whether `data_mapping` resolved.

The failure to watch for is **a clean sweep of 1.0s**. If
`{{item.tool_outcomes}}` does not resolve, every evaluator sees an empty list
and scores 1.0 for "nothing wrong here". That looks like a perfect run and is
actually no evaluation at all, which is the same silent-pass hazard as
ToolCallAccuracy reporting success for a tool type it cannot read.

`--raw` dumps the first output item if the result shape is not what this
expects; the evals surface is preview and its shape moves.
"""

from __future__ import annotations

# This script lives in a subdirectory but imports the converter and scorer
# from the repo root, so put the root on sys.path before those imports. Keeps
# `python3 foundry/check_cloud_eval.py` working from anywhere, with no package
# conversion and no editable install. REPO_ROOT is also how sibling
# directories such as foundry_evaluators/ are located.
import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import argparse, json, os, sys, time

sys.path.insert(0, os.path.join(REPO_ROOT, "foundry_evaluators"))
import checks                                          # noqa: E402

TERMINAL = {"completed", "failed", "canceled", "cancelled", "error"}


def local_verdicts(trace, expected_path):
    import run_evals, to_foundry_dataset, trace_to_eval
    expected = {}
    if expected_path:
        expected = {k: v for k, v in
                    json.load(open(expected_path, encoding="utf-8")).items()
                    if not k.startswith("_")}
    spans = trace_to_eval.load_spans(trace)
    runs, _, _ = trace_to_eval.convert(spans)
    rows = run_evals.score(runs, {"max_empty_rate": 0.25,
                                  "expected": expected})
    dataset = to_foundry_dataset.build_rows(spans, [], expected)
    return rows, dataset


def _as_dict(obj):
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "as_dict", "to_dict"):
        if hasattr(obj, attr):
            try:
                return getattr(obj, attr)()
            except Exception:                        # noqa: BLE001
                pass
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")} \
        if hasattr(obj, "__dict__") else {}


def scores_from(item):
    """Pull {criterion: score} out of an output item, whatever it is called.

    The evals surface is preview; results have appeared under `results`,
    `testing_criteria_results` and `grades` across versions, so look for any
    of them rather than assume.
    """
    data = _as_dict(item)
    for key in ("results", "testing_criteria_results", "grades", "scores"):
        entries = data.get(key)
        if not entries:
            continue
        out = {}
        for entry in entries:
            e = _as_dict(entry)
            name = e.get("name") or e.get("criterion") or e.get("evaluator")
            value = e.get("score", e.get("value", e.get("result")))
            if name is not None and isinstance(value, (int, float)):
                out[str(name)] = float(value)
        if out:
            return out, data
    return {}, data


def item_index(data):
    """The dataset row this output item scored, or None.

    Foundry carries it as `datasource_item_id`; older shapes used
    `datasource_item` with an `index`, or a bare `index`.
    """
    for key in ("datasource_item_id", "item_id", "index"):
        value = data.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    nested = data.get("datasource_item")
    if isinstance(nested, dict):
        for key in ("index", "id", "item_id"):
            value = nested.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return None


def align(local, scored):
    """Pair local rows with Foundry output items. None when it cannot be done.

    Joins on the dataset index when the items carry one. Output items are not
    guaranteed to arrive in dataset order -- they are paged and scored
    concurrently -- so zipping by position produces confident, wrong
    MISMATCH lines whenever the order differs. Position is the fallback, and
    only when every item is accounted for.
    """
    indexed = [(item_index(data), s) for s, data in scored]
    if all(i is not None for i, _ in indexed):
        by_index = {}
        for i, s in indexed:
            if i in by_index or not 0 <= i < len(local):
                return None                       # duplicate or out of range
            by_index[i] = s
        if len(by_index) != len(local):
            return None
        return [(local[i], by_index[i]) for i in sorted(by_index)]

    if len(local) != len(scored):
        return None
    print("\nNo datasource_item_id on the output items — pairing by position. "
          "Verify a MISMATCH by hand before believing it.")
    return list(zip(local, [s for s, _ in scored]))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("eval_id")
    ap.add_argument("run_id")
    ap.add_argument("--project-endpoint",
                    default=os.environ.get("AZURE_AI_PROJECT_ENDPOINT"))
    ap.add_argument("--trace", help="the trace the dataset was built from")
    ap.add_argument("--expected", default="expected.json")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--raw", action="store_true",
                    help="dump the first output item and exit")
    args = ap.parse_args(argv)

    if not args.project_endpoint:
        sys.exit("--project-endpoint or $AZURE_AI_PROJECT_ENDPOINT required")

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(endpoint=args.project_endpoint,
                              credential=DefaultAzureCredential())
    client = project.get_openai_client()

    deadline = time.time() + args.timeout
    status = None
    while time.time() < deadline:
        run = client.evals.runs.retrieve(args.run_id, eval_id=args.eval_id)
        status = str(getattr(run, "status", "") or "").lower()
        print(f"  status={status}")
        if status in TERMINAL:
            break
        time.sleep(15)
    if status not in TERMINAL:
        sys.exit(f"still {status} after {args.timeout}s")

    url = getattr(run, "report_url", None)
    if url:
        print(f"\nFoundry: {url}")

    error = _as_dict(getattr(run, "error", None) or {})
    if error:
        print(f"\nRUN ERROR  {error.get('code', '?')}")
        print(f"  {error.get('message', '')}")
    counts = _as_dict(getattr(run, "result_counts", None) or {})
    if counts:
        print(f"  passed={counts.get('passed')} failed={counts.get('failed')} "
              f"errored={counts.get('errored')} total={counts.get('total')}")
    if status != "completed":
        return 1

    items = list(client.evals.runs.output_items.list(args.run_id,
                                                     eval_id=args.eval_id))
    print(f"{len(items)} output item(s)")
    if args.raw:
        print(json.dumps(_as_dict(items[0]), indent=1, default=str)[:4000])
        return 0

    parsed = [scores_from(i) for i in items]
    # Keep the item payload beside the scores: unparsed items must not be
    # silently dropped, because that shifts every later item's position and
    # the length check below would still pass.
    scored = [(s, d) for s, d in parsed if s]
    unparsed = len(parsed) - len(scored)
    all_scores = [s for s, _ in scored]
    if not all_scores:
        print("\nNo scores parsed. The result shape is not one this knows; "
              "re-run with --raw and the shape can be added.")
        return 1

    names = sorted({n for s in all_scores for n in s})
    print("\nFOUNDRY SCORES")
    print(f"{'row':<5}" + "".join(f"{n.replace('cw_', ''):>22}" for n in names))
    for i, scores in enumerate(all_scores):
        print(f"{i:<5}" + "".join(
            f"{scores.get(n, float('nan')):>22.2f}" for n in names))

    # A clean sweep of 1.0 means data_mapping did not resolve and every
    # evaluator scored an empty input, not that the agents were flawless.
    if all(v == 1.0 for s in all_scores for v in s.values()):
        print("\nEVERY SCORE IS 1.0. That is the signature of data_mapping "
              "not resolving — each evaluator saw an empty tool_outcomes and "
              "reported nothing wrong. Treat it as a failed run, not a "
              "perfect one.")
        return 1

    if not args.trace:
        return 0

    local, _ = local_verdicts(args.trace, args.expected)

    aligned = align(local, scored)
    if aligned is None:
        print(f"\n{len(local)} local run(s) vs {len(all_scores)} scored "
              f"item(s)"
              + (f" ({unparsed} unparsed)" if unparsed else "")
              + " — cannot align, skipping the diff")
        return 0

    pairs = [(f"cw_{c}", c) for c in
             ("no_wasted_calls", "no_tool_errors", "no_dead_ends",
              "no_search_cascade", "no_truncation", "trajectory",
              "valid_tool_args")]
    print("\nDIFF AGAINST LOCAL")
    mismatches = 0
    for row, scores in aligned:
        for registered, check in pairs:
            verdict = row["checks"].get(check, {}).get("passed")
            score = scores.get(registered)
            if verdict is None or score is None:
                continue
            threshold = checks.EVALUATORS[registered][4]
            if (score >= threshold) != verdict:
                mismatches += 1
                print(f"  MISMATCH {row['run_agent']:<30}{check:<20}"
                      f"local={verdict} foundry={score:.2f} "
                      f"threshold={threshold}")
    print(f"\n{mismatches} mismatch(es)")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
