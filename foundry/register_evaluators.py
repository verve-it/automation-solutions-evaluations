#!/usr/bin/env python3
"""
register_evaluators.py — publish the deterministic checks to the Foundry
evaluator catalog as versioned, reusable code-based evaluators.

    python3 register_evaluators.py --dry-run          # print, call nothing
    python3 register_evaluators.py --project-endpoint $AZURE_AI_PROJECT_ENDPOINT \\
        --model-deployment $AZURE_JUDGE_DEPLOYMENT

Once registered they sit in the catalog beside Microsoft's built-ins, can be
versioned, reused by any agent on the same tool surface, and — per the
Foundry docs — used in continuous evaluation as well as batch runs.

How the code gets there
-----------------------
A code-based evaluator runs in a sandbox with no network and no way to import
from this repo, so each one is shipped as a single self-contained `code_text`:
`foundry_evaluators/_shared.py` (docstring stripped) followed by its own
`grade_*` function renamed to `grade`. That is why the helpers live in one
small stdlib-only module.

What is lost in the port
------------------------
A code-based evaluator returns exactly one float, 0.0-1.0. `run_evals.py`
returns a verdict *and* a reason ("4 avoidable call(s): empty_failedx2,
missing_scriptx1"). A score cannot say why. Keep run_evals.py as the local
gate that explains itself; these are for catalog membership, portal history
and continuous evaluation.

Fidelity is checked, not assumed: tests/test_foundry_evaluators.py scores both
frozen trace sets with run_evals.py and with these functions and asserts every
comparable verdict matches. 56 verdicts, 0 mismatches at the time of writing.
"""

from __future__ import annotations

# This script lives in a subdirectory but imports the converter and scorer
# from the repo root, so put the root on sys.path before those imports. Keeps
# `python3 foundry/register_evaluators.py` working from anywhere, with no package
# conversion and no editable install. REPO_ROOT is also how sibling
# directories such as foundry_evaluators/ are located.
import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import argparse, inspect, json, os, re, sys

sys.path.insert(0, os.path.join(REPO_ROOT, "foundry_evaluators"))
import checks                                          # noqa: E402

SHARED = os.path.join(REPO_ROOT, "foundry_evaluators", "_shared.py")


def shared_source():
    """_shared.py without its module docstring — the sandbox does not need it
    and every byte counts against the 256 KB code limit."""
    src = open(SHARED, encoding="utf-8").read()
    return re.sub(r"^\"\"\".*?\"\"\"\n", "", src, count=1, flags=re.S).lstrip()


def code_text(fn):
    """One self-contained module: shared helpers plus this check, renamed to
    the `grade` entry point Foundry calls."""
    body = inspect.getsource(fn)
    body = re.sub(r"^def grade_\w+\(", "def grade(", body, count=1,
                  flags=re.M)
    return f"{shared_source()}\n\n{body}"


def evaluator_version(name, fn, display, description, categories, threshold):
    return {
        "name": name,
        "categories": categories,
        "display_name": display,
        "description": description,
        "definition": {
            "type": "code",
            "code_text": code_text(fn),
            "init_parameters": {
                "type": "object",
                # deployment_name is required even for a non-LLM evaluator.
                "required": ["deployment_name", "pass_threshold"],
                "properties": {
                    "deployment_name": {"type": "string"},
                    # A number, not a string. The published sample declares
                    # this as a string but passes 0.5, and the service
                    # compares it against the float the evaluator returns:
                    # a string value fails at run time with
                    # "'<=' not supported between instances of 'float' and
                    # 'str'".
                    "pass_threshold": {"type": "number",
                                       "default": float(threshold)},
                },
            },
            # EvaluatorMetricType is an enum: ordinal, continuous, boolean.
            # Every check returns a float 0.0-1.0, which is `continuous`.
            "metrics": {name: {"type": "continuous"}},
            "data_schema": {
                "type": "object",
                "properties": {
                    "tool_outcomes": {"type": "array"},
                    "tool_definitions": {"type": "array"},
                    "expected_actions": {"type": "array"},
                    "duration_ms": {"type": "number"},
                },
            },
        },
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project-endpoint",
                    default=os.environ.get("AZURE_AI_PROJECT_ENDPOINT"))
    ap.add_argument("--model-deployment",
                    default=os.environ.get("AZURE_JUDGE_DEPLOYMENT"))
    ap.add_argument("--only", help="comma-separated evaluator names")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the payloads and exit; no credentials, no "
                         "SDK, no calls")
    ap.add_argument("--out", help="also write the payloads here as JSON")
    ap.add_argument("--lock", default="evaluator-versions.json",
                    help="record the registered versions here, so a run pins "
                         "them instead of floating to whatever is latest")
    args = ap.parse_args()

    wanted = ([n.strip() for n in args.only.split(",")] if args.only
              else list(checks.EVALUATORS))
    unknown = [n for n in wanted if n not in checks.EVALUATORS]
    if unknown:
        sys.exit(f"unknown evaluator(s): {', '.join(unknown)}")

    payloads = {}
    for name in wanted:
        fn, display, description, categories, threshold, gating = \
            checks.EVALUATORS[name]
        payloads[name] = evaluator_version(name, fn, display, description,
                                           categories, threshold)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payloads, fh, indent=1)
        print(f"wrote {args.out}")

    for name, payload in payloads.items():
        size = len(payload["definition"]["code_text"])
        gate = "gating" if checks.EVALUATORS[name][5] else "info"
        print(f"  {name:<24} {size:>6} bytes  threshold="
              f"{checks.EVALUATORS[name][4]:<5} {gate}")
        if size > 256 * 1024:
            sys.exit(f"{name}: code_text exceeds the 256 KB sandbox limit")

    if args.dry_run:
        print(f"\n{len(payloads)} payload(s) built; nothing was called")
        return 0

    if not (args.project_endpoint and args.model_deployment):
        sys.exit("--project-endpoint and --model-deployment are required "
                 "without --dry-run")

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    client = AIProjectClient(endpoint=args.project_endpoint,
                             credential=DefaultAzureCredential())

    # Keep going on failure. The payload schema is preview and its enums are
    # only discoverable by rejection, so one round-trip per problem is a poor
    # trade; surface every one at once.
    registered, failures = [], []
    for name, payload in payloads.items():
        try:
            result = client.beta.evaluators.create_version(
                name=name, evaluator_version=payload)
            registered.append((name, getattr(result, "version", "?")))
            print(f"registered {name} v{registered[-1][1]}")
        except Exception as exc:                     # noqa: BLE001
            message = getattr(exc, "message", None) or str(exc)
            failures.append((name, message.split("\n")[0][:300]))
            print(f"FAILED     {name}")

    print(f"\n{len(registered)} registered, {len(failures)} failed")
    for name, message in failures:
        print(f"\n  {name}\n    {message}")

    # Without this a run leaves `evaluator_version` empty and floats to
    # whatever is latest, so two runs of the "same" baseline can be scored by
    # different code. Commit the lock file with the baseline it belongs to.
    if registered and args.lock:
        existing = {}
        if os.path.exists(args.lock):
            with open(args.lock, encoding="utf-8") as fh:
                existing = json.load(fh)
        existing.update({name: str(version) for name, version in registered})
        with open(args.lock, "w", encoding="utf-8") as fh:
            json.dump(dict(sorted(existing.items())), fh, indent=1)
            fh.write("\n")
        print(f"\nwrote {args.lock}: "
              + ", ".join(f"{n} v{v}" for n, v in sorted(existing.items())))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
