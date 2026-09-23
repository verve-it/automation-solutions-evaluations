#!/usr/bin/env python3
"""
run_cloud_eval.py — upload the dataset and run the registered evaluators in
Foundry, so the results live in the project rather than in a CI artifact zip.

    python3 to_foundry_dataset.py traces/2026-09-03-full-triage.json \\
        --expected expected.json --tool-defs tool_manifests/ \\
        -o artifacts/foundry-dataset.jsonl
    python3 run_cloud_eval.py artifacts/foundry-dataset.jsonl \\
        --name full-triage --dataset-version 2026-09-17

Register the evaluators first (`register_evaluators.py`), or the run has
nothing to score with.

Why a dataset rather than the `azure_ai_traces` data source
-----------------------------------------------------------
`azure_ai_traces` reads only `invoke_agent` spans, and on these traces those
carry `tool_call` but no `tool_result` — every result is on an `execute_tool`
span. Four of the eight checks read results. Building the dataset ourselves
keeps them, and gives the per-agent decomposition Foundry's trace paths do not
do. See docs/FOUNDRY.md.
"""

from __future__ import annotations

# This script lives in a subdirectory but imports the converter and scorer
# from the repo root, so put the root on sys.path before those imports. Keeps
# `python3 foundry/run_cloud_eval.py` working from anywhere, with no package
# conversion and no editable install. REPO_ROOT is also how sibling
# directories such as foundry_evaluators/ are located.
import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import argparse, json, os, sys

sys.path.insert(0, os.path.join(REPO_ROOT, "foundry_evaluators"))
import checks                                          # noqa: E402

# Judged evaluators worth running on the same rows. Sampled, not swept.
BUILTINS = {
    "intent_resolution": "builtin.intent_resolution",
    "task_adherence": "builtin.task_adherence",
    "tool_call_accuracy": "builtin.tool_call_accuracy",
}


JSON_TYPES = [(bool, "boolean"), (int, "integer"), (float, "number"),
              (str, "string"), (list, "array"), (dict, "object")]


def json_type(value):
    if value is None:
        return "null"
    for py, name in JSON_TYPES:
        if isinstance(value, py):
            return name
    return "string"


def object_schema(values):
    """Describe a nested object from every instance of it seen.

    Nested properties must be declared too. An undeclared one defaults to
    string, and the run fails with "Error validating file against schema:
    35756 is not of type 'string'" — 35756 being
    `usage.uncached_input_tokens` under a `usage` declared as a bare
    `{"type": "object"}`.

    Arrays are left undescribed on purpose: `tool_outcomes` carries integers
    and booleans and validates fine without an `items` schema, so the
    validator does not descend into them. Declaring one would invite a
    stricter check for no benefit.
    """
    seen = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        for key, inner in value.items():
            seen.setdefault(key, []).append(inner)

    properties = {}
    for key, inners in seen.items():
        types = sorted({json_type(i) for i in inners})
        spec = {"type": types[0] if len(types) == 1 else types}
        if types == ["object"]:
            spec.update(object_schema(inners))
        properties[key] = spec
    return {"type": "object", "properties": properties,
            "required": sorted(k for k, v in seen.items()
                               if not any(i is None for i in v))}


def item_schema(rows):
    """Describe every column, derived from the data.

    A bare `{"type": "object"}` is not permissive — the service defaults
    undeclared properties to string. Deriving the schema also means a column
    added later cannot arrive undeclared.
    """
    return object_schema(rows)


def init_params(model_deployment, threshold):
    """`pass_threshold` is a NUMBER. The published sample declares it as a
    string but passes 0.5, and the service compares it against the float the
    evaluator returns — a string fails at run time with "'<=' not supported
    between instances of 'float' and 'str'"."""
    return {"deployment_name": model_deployment,
            "pass_threshold": float(threshold)}


# Columns every custom evaluator is offered. `usage_*` is discovered from the
# rows because it is flattened — see to_foundry_dataset.py.
BASE_COLUMNS = ("tool_outcomes", "tool_definitions", "expected_actions",
                "duration_ms", "max_tokens", "max_duration_ms")


def data_mapping(rows):
    columns = [c for c in BASE_COLUMNS if any(c in r for r in rows)]
    columns += sorted({k for r in rows for k in r if k.startswith("usage_")})
    return {c: "{{item.%s}}" % c for c in columns}


def load_lock(path):
    """{evaluator: version} written by register_evaluators.py."""
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return {k: str(v) for k, v in json.load(fh).items()}
    return {}


def testing_criteria(rows, model_deployment, judged=(), only=None, lock=None):
    """Our registered evaluators, plus any built-ins asked for.

    A custom evaluator is referenced exactly like a built-in — same criterion
    type, `evaluator_name` carrying the registered name instead of a
    `builtin.` one.
    """
    from azure.ai.projects.models import TestingCriterionAzureAIEvaluator

    criteria = []
    for name, (_, _, _, _, threshold, _) in checks.EVALUATORS.items():
        if only and name not in only:
            continue
        # A TypedDict: an instance is a plain dict, so the version is a key
        # given at construction. Assigning it as an attribute raised
        # AttributeError on every run that had a lock.
        version = (lock or {}).get(name)
        criteria.append(TestingCriterionAzureAIEvaluator(
            type="azure_ai_evaluator",
            name=name,
            evaluator_name=name,
            initialization_parameters=init_params(model_deployment,
                                                  threshold),
            data_mapping=data_mapping(rows),
            **({"evaluator_version": version} if version else {}),
        ))

    for short in judged:
        criteria.append(TestingCriterionAzureAIEvaluator(
            type="azure_ai_evaluator",
            name=short,
            evaluator_name=BUILTINS[short],
            initialization_parameters={"deployment_name": model_deployment},
            data_mapping={
                "query": "{{item.messages}}",
                "response": "{{item.messages}}",
                "tool_definitions": "{{item.tool_definitions}}",
            },
        ))
    return criteria


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", help="JSONL from to_foundry_dataset.py")
    ap.add_argument("--project-endpoint",
                    default=os.environ.get("AZURE_AI_PROJECT_ENDPOINT"))
    ap.add_argument("--model-deployment",
                    default=os.environ.get("AZURE_JUDGE_DEPLOYMENT"))
    ap.add_argument("--name", default="triage-evals")
    ap.add_argument("--dataset-name", default="triage-eval-runs")
    ap.add_argument("--dataset-version", required=True,
                    help="version the dataset explicitly; a run is only "
                         "comparable to another run on a known version")
    ap.add_argument("--judged", default="",
                    help=f"also run built-ins: {', '.join(BUILTINS)}")
    ap.add_argument("--only", help="comma-separated custom evaluator names")
    ap.add_argument("--lock", default="evaluator-versions.json",
                    help="pin evaluator versions from this file. Without it "
                         "a run floats to whatever is latest and two runs of "
                         "the same baseline can be scored by different code.")
    ap.add_argument("--no-lock", action="store_true",
                    help="deliberately run against the latest versions")
    ap.add_argument("--wait", action="store_true",
                    help="wait for the run and diff it against the local "
                         "checks, instead of printing ids to copy")
    ap.add_argument("--trace", help="with --wait, the trace the dataset was "
                                    "built from, for the local comparison")
    ap.add_argument("--expected", default="expected.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would run; no credentials, no calls")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.dataset, encoding="utf-8")
            if l.strip()]
    judged = [j.strip() for j in args.judged.split(",") if j.strip()]
    unknown = [j for j in judged if j not in BUILTINS]
    if unknown:
        sys.exit(f"unknown built-in(s): {', '.join(unknown)}")
    only = set(n.strip() for n in args.only.split(",")) if args.only else None

    custom = [n for n in checks.EVALUATORS if not only or n in only]

    # cw_valid_tool_args scores 1.0 when there is no schema to validate
    # against, and a 1.00 in the portal reads as a clean pass — green without
    # having checked anything, the same silent-pass shape as ToolCallAccuracy
    # reporting success for a tool type it cannot read. With no schemas at
    # all, drop it so its absence is visible; with some, say how many rows
    # are vacuous so the number is not mistaken for a result.
    if "cw_valid_tool_args" in custom:
        with_defs = sum(1 for r in rows if r.get("tool_definitions"))
        if not with_defs:
            custom.remove("cw_valid_tool_args")
            only = set(custom)
            print("SKIPPING cw_valid_tool_args: no row carries "
                  "tool_definitions, so it would score 1.0 without "
                  "validating anything. Fill tool_manifests/ — see "
                  "tool_manifests/README.md.")
        elif with_defs < len(rows):
            print(f"WARNING cw_valid_tool_args has schemas for "
                  f"{with_defs}/{len(rows)} row(s). The other "
                  f"{len(rows) - with_defs} will score 1.00 without "
                  "validating anything — that is missing coverage, not a "
                  "pass. Fill tool_manifests/.")
    lock = {} if args.no_lock else load_lock(args.lock)
    if lock:
        print("pinned evaluators  : "
              + ", ".join(f"{n} v{v}" for n, v in sorted(lock.items())
                          if n in custom))
    else:
        print("pinned evaluators  : none — this run floats to the latest "
              "registered version of each evaluator")
    # The built-in judged evaluators read `messages`; ours read tool_outcomes.
    if judged and not any(r.get("messages") for r in rows):
        sys.exit("--judged needs the `messages` column, and this dataset was "
                 "built with --no-messages. Rebuild without it, or drop "
                 "--judged.")
    print(f"{len(rows)} row(s) from {args.dataset}")
    print(f"custom evaluators : {', '.join(custom)}")
    print(f"judged evaluators : {', '.join(judged) or 'none'}")
    if args.dry_run:
        print("\ndry run; nothing was called")
        return 0

    if not (args.project_endpoint and args.model_deployment):
        sys.exit("--project-endpoint and --model-deployment are required")

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(endpoint=args.project_endpoint,
                              credential=DefaultAzureCredential())
    # Evaluations are the OpenAI-compatible surface: AIProjectClient itself
    # has .datasets, .agents, .beta.evaluators and so on, but `evals` hangs
    # off the client `get_openai_client()` returns.
    client = project.get_openai_client()

    # Check the catalog BEFORE uploading. A missing evaluator fails the run
    # after the dataset has landed, leaving an orphan version behind and
    # forcing a bump on the retry.
    try:
        known = {getattr(e, "name", None)
                 for e in project.beta.evaluators.list()}
    except Exception:                                # noqa: BLE001
        known = None                                 # listing is best-effort
    if known is not None:
        missing = [n for n in custom if n not in known]
        if missing:
            sys.exit("not registered in this project: "
                     + ", ".join(missing)
                     + "\nRun register_evaluators.py first.")

    dataset = project.datasets.upload_file(
        name=args.dataset_name, version=args.dataset_version,
        file_path=os.path.abspath(args.dataset))
    print(f"\ndataset {args.dataset_name} v{args.dataset_version} -> "
          f"{dataset.id}")

    evaluation = client.evals.create(
        name=args.name,
        data_source_config={
            "type": "custom",
            "item_schema": item_schema(rows),
            "include_sample_schema": False,
        },
        testing_criteria=testing_criteria(rows, args.model_deployment,
                                          judged, only, lock),
    )
    print(f"evaluation {evaluation.id}")

    run = client.evals.runs.create(
        eval_id=evaluation.id,
        name=f"{args.name}-{args.dataset_version}",
        data_source={"type": "jsonl",
                     "source": {"type": "file_id", "id": dataset.id}},
    )
    print(f"run {run.id}")
    url = getattr(run, "report_url", None) or getattr(run, "studio_url", None)
    if url:
        print(f"\nFoundry: {url}")

    if args.wait:
        import check_cloud_eval
        argv = [evaluation.id, run.id,
                "--project-endpoint", args.project_endpoint,
                "--expected", args.expected]
        if args.trace:
            argv += ["--trace", args.trace]
        print()
        return check_cloud_eval.main(argv)

    print(f"\nFollow it with:\n  python check_cloud_eval.py {evaluation.id} "
          f"{run.id} --trace <the trace the dataset was built from>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
