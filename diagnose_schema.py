#!/usr/bin/env python3
"""
diagnose_schema.py — find out what the evals datasource validator actually
accepts, instead of guessing one shape per round-trip.

    python3 diagnose_schema.py --project-endpoint $env:AZURE_AI_PROJECT_ENDPOINT \
        --model-deployment $env:AZURE_JUDGE_DEPLOYMENT

Five payload shapes have already been rejected in sequence, the last two with
the same message — "35756 is not of type 'string'" — under a schema that
demonstrably declared the field as an integer. So the schema may not be
reaching the validator: the AOAI eval id in the error differs from the Foundry
eval id, which suggests a shadow eval is created with a schema of its own.

This submits several one-row datasets that differ in exactly one way each and
reports which survive, so the next fix is based on evidence.

Nothing here writes to the project beyond throwaway evals and one-row
datasets, all named `schema-probe-*`.
"""

from __future__ import annotations
import argparse, json, os, sys, tempfile, time

PROBES = {
    # name: (one item, what it tells us)
    "all_strings": (
        {"tool_outcomes": "[]", "duration_ms": "1.0", "usage": "{}"},
        "everything stringified — if only this passes, send strings"),
    "top_level_int": (
        {"tool_outcomes": [], "duration_ms": 1},
        "a bare top-level integer"),
    "top_level_float": (
        {"tool_outcomes": [], "duration_ms": 1.5},
        "a bare top-level float"),
    "nested_int": (
        {"tool_outcomes": [], "usage": {"uncached_input_tokens": 35756}},
        "an integer inside a declared object — the failing case"),
    "array_of_objects_with_ints": (
        {"tool_outcomes": [{"tool": "cw_query", "result_len": 35756,
                            "success": True}]},
        "an integer inside an array — believed not descended into"),
    "strings_only_arrays": (
        {"tool_outcomes": [{"tool": "cw_query", "result_len": "35756",
                            "success": "true"}]},
        "the same array with its values stringified"),
}


def json_type(value):
    if value is None:
        return "null"
    for py, name in ((bool, "boolean"), (int, "integer"), (float, "number"),
                     (str, "string"), (list, "array"), (dict, "object")):
        if isinstance(value, py):
            return name
    return "string"


def schema_for(item):
    props = {}
    for key, value in item.items():
        spec = {"type": json_type(value)}
        if isinstance(value, dict):
            spec["properties"] = {k: {"type": json_type(v)}
                                  for k, v in value.items()}
        props[key] = spec
    return {"type": "object", "properties": props,
            "required": sorted(item)}


def run_probe(project, client, name, item, deployment, evaluator):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(json.dumps(item) + "\n")
        path = fh.name
    try:
        version = f"probe-{name}-{int(time.time())}"
        dataset = project.datasets.upload_file(
            name="schema-probe", version=version, file_path=path)
        evaluation = client.evals.create(
            name=f"schema-probe-{name}",
            data_source_config={"type": "custom",
                                "item_schema": schema_for(item),
                                "include_sample_schema": False},
            testing_criteria=[{
                "type": "azure_ai_evaluator",
                "name": evaluator,
                "evaluator_name": evaluator,
                "initialization_parameters": {"deployment_name": deployment,
                                              "pass_threshold": 1.0},
                "data_mapping": {"tool_outcomes": "{{item.tool_outcomes}}"},
            }],
        )
        run = client.evals.runs.create(
            eval_id=evaluation.id, name=f"probe-{name}",
            data_source={"type": "jsonl",
                         "source": {"type": "file_id", "id": dataset.id}})
        for _ in range(40):
            run = client.evals.runs.retrieve(run.id, eval_id=evaluation.id)
            status = str(getattr(run, "status", "")).lower()
            if status in ("completed", "failed", "canceled", "error"):
                break
            time.sleep(5)
        error = getattr(run, "error", None)
        message = ""
        if error is not None:
            message = str(getattr(error, "message", error))
        return status, message
    except Exception as exc:                         # noqa: BLE001
        return "exception", str(exc)
    finally:
        os.unlink(path)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project-endpoint",
                    default=os.environ.get("AZURE_AI_PROJECT_ENDPOINT"))
    ap.add_argument("--model-deployment",
                    default=os.environ.get("AZURE_JUDGE_DEPLOYMENT"))
    ap.add_argument("--evaluator", default="cw_no_wasted_calls",
                    help="an already-registered evaluator to attach")
    ap.add_argument("--only", help="comma-separated probe names")
    args = ap.parse_args()

    if not (args.project_endpoint and args.model_deployment):
        sys.exit("--project-endpoint and --model-deployment are required")

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(endpoint=args.project_endpoint,
                              credential=DefaultAzureCredential())
    client = project.get_openai_client()

    wanted = ([n.strip() for n in args.only.split(",")] if args.only
              else list(PROBES))
    print(f"{len(wanted)} probe(s)\n")
    results = {}
    for name in wanted:
        item, why = PROBES[name]
        print(f"{name}  — {why}")
        status, message = run_probe(project, client, name, item,
                                    args.model_deployment, args.evaluator)
        results[name] = (status, message)
        mark = "PASS" if status == "completed" else "fail"
        print(f"  {mark}  {status}")
        if message:
            print(f"        {message[:220]}")
        print()

    print("SUMMARY")
    for name, (status, _) in results.items():
        print(f"  {'PASS' if status == 'completed' else 'fail':<5} {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
