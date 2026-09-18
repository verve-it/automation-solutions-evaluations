#!/usr/bin/env python3
"""
submit_to_foundry.py — run the Foundry judged evaluators over eval_runs.jsonl
and upload the results to the project for portal history.

This is a SECOND SURFACE over the same dataset, not a replacement for
run_evals.py. The split, deliberately:

  run_evals.py      deterministic, free, no judge variance    -> merge gate
  this              judged, costs inference, scores wobble    -> sampled

Do not run LLM judges per commit. Nightly or weekly over a sample.

What this buys that run_evals.py cannot: Task Adherence, Intent Resolution and
Relevance are LLM-judged and we cannot write them ourselves, plus portal run
history and comparison that non-engineers can look at.

One caveat worth knowing before you trust a green:
ToolCallAccuracyEvaluator returns **pass** for tool types it does not support,
with a reason string saying so. If the ConnectWise MCP tools do not register as
Function Tools it will report success without evaluating anything. Read the
reason strings, and keep `valid_tool_args` in run_evals.py as the actual gate.

    python3 submit_to_foundry.py out/eval_runs.jsonl \\
        --project-endpoint $AZURE_AI_PROJECT_ENDPOINT \\
        --model-deployment gpt-4o \\
        --sample 20
"""

from __future__ import annotations

# This script lives in a subdirectory but imports the converter and scorer
# from the repo root, so put the root on sys.path before those imports. Keeps
# `python3 foundry/submit_to_foundry.py` working from anywhere, with no package
# conversion and no editable install. REPO_ROOT is also how sibling
# directories such as foundry_evaluators/ are located.
import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import argparse, json, os, random, sys

# Foundry evaluators that accept the converter schema (query / response /
# tool_calls / tool_definitions).
AVAILABLE = ["intent_resolution", "task_adherence", "tool_call_accuracy",
             "relevance"]
DEFAULT = ["intent_resolution", "task_adherence", "tool_call_accuracy"]


def to_foundry_rows(runs):
    """eval_runs.jsonl -> the field names the Foundry agent evaluators expect.

    Our rows already carry `query`, `response` and `tool_definitions` in the
    right shape; the evaluators want the calls under `tool_calls` rather than
    the `actions` name Task Navigation Efficiency uses.
    """
    rows = []
    for r in runs:
        calls = []
        for action in r.get("actions", []):
            for part in action.get("content", []):
                calls.append({
                    "type": "tool_call",
                    "name": part.get("name"),
                    "arguments": _maybe_json(part.get("arguments")),
                })
        rows.append({
            "query": r.get("query") or [],
            "response": r.get("response") or [],
            "tool_calls": calls,
            "tool_definitions": r.get("tool_definitions") or [],
            # carried through so a portal row can be traced back
            "orchestration_id": r.get("orchestration_id"),
            "run_agent": r.get("run_agent"),
            "intent": r.get("intent"),
        })
    return rows


def _maybe_json(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def select(rows, sample, seed):
    """Judged evaluation costs inference per row. Sample, do not sweep."""
    if not sample or sample >= len(rows):
        return rows
    return random.Random(seed).sample(rows, sample)


def build_evaluators(names, endpoint, deployment):
    from azure.ai.evaluation import (IntentResolutionEvaluator,
                                     RelevanceEvaluator, TaskAdherenceEvaluator,
                                     ToolCallAccuracyEvaluator)
    model_config = {
        "azure_endpoint": endpoint,
        "azure_deployment": deployment,
    }
    catalog = {
        "intent_resolution": IntentResolutionEvaluator,
        "task_adherence": TaskAdherenceEvaluator,
        "tool_call_accuracy": ToolCallAccuracyEvaluator,
        "relevance": RelevanceEvaluator,
    }
    return {n: catalog[n](model_config=model_config) for n in names}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsonl", help="eval_runs.jsonl from trace_to_eval.py")
    ap.add_argument("--project-endpoint",
                    default=os.environ.get("AZURE_AI_PROJECT_ENDPOINT"))
    ap.add_argument("--model-endpoint",
                    default=os.environ.get("AZURE_OPENAI_ENDPOINT"),
                    help="judge model endpoint (defaults to the project's)")
    ap.add_argument("--model-deployment",
                    default=os.environ.get("AZURE_OPENAI_DEPLOYMENT"),
                    help="judge model deployment, e.g. gpt-4o")
    ap.add_argument("--evaluators", default=",".join(DEFAULT),
                    help=f"any of: {', '.join(AVAILABLE)}")
    ap.add_argument("--sample", type=int, default=20,
                    help="rows to judge; 0 for all. Judged evaluation costs "
                         "inference per row.")
    ap.add_argument("--seed", type=int, default=0,
                    help="sampling seed, so a run is reproducible")
    ap.add_argument("--name", default="triage-evals",
                    help="evaluation name shown in the portal")
    ap.add_argument("--out", default="artifacts/foundry-input.jsonl")
    ap.add_argument("--dry-run", action="store_true",
                    help="write the converted dataset and stop; no judge "
                         "calls, no spend, no SDK needed")
    args = ap.parse_args()

    with open(args.jsonl, encoding="utf-8") as fh:
        runs = [json.loads(l) for l in fh if l.strip()]
    rows = select(to_foundry_rows(runs), args.sample, args.seed)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{len(rows)} of {len(runs)} run(s) -> {args.out}")

    without_defs = sum(1 for r in rows if not r["tool_definitions"])
    if without_defs:
        print(f"WARNING {without_defs} row(s) carry no tool_definitions. "
              "ToolCallAccuracy cannot evaluate those and reports pass. "
              "Fill tool_manifests/ first — see tool_manifests/README.md.")

    if args.dry_run:
        return 0
    if not (args.project_endpoint and args.model_deployment):
        sys.exit("--project-endpoint and --model-deployment are required "
                 "without --dry-run")

    names = [n.strip() for n in args.evaluators.split(",") if n.strip()]
    unknown = [n for n in names if n not in AVAILABLE]
    if unknown:
        sys.exit(f"unknown evaluator(s): {', '.join(unknown)}")

    from azure.ai.evaluation import evaluate
    result = evaluate(
        data=args.out,
        evaluation_name=args.name,
        evaluators=build_evaluators(
            names, args.model_endpoint or args.project_endpoint,
            args.model_deployment),
        azure_ai_project=args.project_endpoint,
    )
    print(json.dumps(result.get("metrics", {}), indent=1))
    if result.get("studio_url"):
        print(f"\nFoundry: {result['studio_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
