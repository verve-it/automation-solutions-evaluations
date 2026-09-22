#!/usr/bin/env python3
"""
continuous_eval.py — have Foundry score live agent runs, automatically.

The nightly drift job exports 24h of traces, converts them and scores them in
CI. Foundry does that natively: an evaluation rule samples live agent
responses as they complete, runs evaluators against them, and lands the
results in the Observability dashboard where alert rules can fire on them.
GA since March 2026, and it takes **custom** evaluators — ours are registered
custom evaluators, so they qualify.

    # see the rule that would be created, touch nothing
    python3 foundry/continuous_eval.py --agent triage-orchestrator \\
        --eval-id <eval> --dry-run

    # create or update it
    python3 foundry/continuous_eval.py --agent triage-orchestrator \\
        --eval-id <eval> --sampling-percent 25

    python3 foundry/continuous_eval.py --list
    python3 foundry/continuous_eval.py --disable triage-orchestrator-continuous

Sampling is cheap here, and that is not the usual case
-----------------------------------------------------
Guidance on continuous evaluation warns about cost, because the usual
evaluators are LLM judges and every sampled interaction is an inference call.
Ours are **code-based** — `grade(sample, item) -> float`, no model — so a
sampled run costs compute, not tokens. Sample high. The default here is 25%
rather than the 5-10% the guidance suggests, and 100% is defensible for the
deterministic checks.

`--max-hourly-runs` is still set, as a guard against a runaway agent
generating far more traffic than expected, not as a cost control.

This does not replace the nightly
---------------------------------
Continuous evaluation scores an *interaction*. The converter decomposes an
orchestration into one row per agent run, and reads `execute_tool` spans for
tool results that `invoke_agent` spans do not carry — four of the eight
checks read results. So the two answer different questions, and both are
worth having:

  continuous  every day, live, dashboard and alerts, interaction-level
  nightly     per-agent decomposition, baseline diff, CI artifact

Same shape as judged-versus-deterministic: two surfaces, one truth.
"""

from __future__ import annotations

import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import argparse, json

DEFAULT_SAMPLING_PERCENT = 25.0
DEFAULT_MAX_HOURLY_RUNS = 500


def rule_id(agent):
    """Stable id, so re-running updates rather than accumulating rules."""
    return f"{agent}-continuous"


def rule_payload(agent, eval_id, sampling_percent, max_hourly_runs,
                 enabled=True):
    """The EvaluationRule, as a plain dict.

    Kept dict-shaped so --dry-run can print exactly what will be sent and so
    this is testable without the SDK installed. `create_or_update` accepts a
    MutableMapping as well as the model type.
    """
    return {
        "displayName": f"{agent} — deterministic checks",
        "description": ("Continuous evaluation with the cw_* custom "
                        "evaluators. Code-based, no judge inference."),
        "enabled": enabled,
        "eventType": "ResponseCompleted",
        "filter": {"agentName": agent},
        "action": {
            "type": "ContinuousEvaluation",
            "evalId": eval_id,
            "samplingRate": sampling_percent,
            "maxHourlyRuns": max_hourly_runs,
        },
    }


def validate_sampling(percent):
    """Reject a value that is ambiguous between a percent and a fraction.

    The docs speak of `samplingPercent` 0-100; the SDK field is
    `sampling_rate`. A bare 1 could mean 1% or 100% depending on which the
    service wants, and getting it wrong silently samples 100x too much or too
    little. Refuse the ambiguous range and make the caller say which.
    """
    if not 0 < percent <= 100:
        raise SystemExit(f"--sampling-percent must be in (0, 100]; got {percent}")
    if percent < 1:
        raise SystemExit(
            f"--sampling-percent {percent} is below 1%. If you meant a "
            "fraction, this flag takes a PERCENT: 25 means 25%. If you really "
            "want sub-1% sampling, the checks are code-based and cost no "
            "inference, so there is no reason to.")
    return float(percent)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", help="agent name the rule filters on")
    ap.add_argument("--eval-id",
                    help="an existing eval whose testing criteria are the "
                         "registered cw_* evaluators. Create one with "
                         "run_cloud_eval.py first.")
    ap.add_argument("--sampling-percent", type=float,
                    default=DEFAULT_SAMPLING_PERCENT)
    ap.add_argument("--max-hourly-runs", type=int,
                    default=DEFAULT_MAX_HOURLY_RUNS)
    ap.add_argument("--list", action="store_true",
                    help="list the rules on this project and exit")
    ap.add_argument("--disable", metavar="RULE_ID",
                    help="disable a rule without deleting it")
    ap.add_argument("--delete", metavar="RULE_ID")
    ap.add_argument("--project-endpoint",
                    default=os.environ.get("AZURE_AI_PROJECT_ENDPOINT"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.dry_run:
        if not (args.agent and args.eval_id):
            sys.exit("--agent and --eval-id are required")
        percent = validate_sampling(args.sampling_percent)
        print(json.dumps({
            "id": rule_id(args.agent),
            "rule": rule_payload(args.agent, args.eval_id, percent,
                                 args.max_hourly_runs),
        }, indent=1))
        print("\ndry run: nothing was created in the project.")
        return 0

    if not args.project_endpoint:
        sys.exit("--project-endpoint or AZURE_AI_PROJECT_ENDPOINT is required")

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    client = AIProjectClient(endpoint=args.project_endpoint,
                             credential=DefaultAzureCredential())
    rules = client.evaluation_rules

    if args.list:
        found = list(rules.list())
        if not found:
            print("no evaluation rules on this project")
            return 0
        for r in found:
            d = r.as_dict() if hasattr(r, "as_dict") else dict(r)
            action = d.get("action") or {}
            print(f"  {d.get('id'):40} enabled={d.get('enabled')} "
                  f"sampling={action.get('samplingRate')} "
                  f"eval={action.get('evalId')}")
        return 0

    if args.delete:
        rules.delete(args.delete)
        print(f"deleted {args.delete}")
        return 0

    if args.disable:
        existing = rules.get(args.disable)
        d = existing.as_dict() if hasattr(existing, "as_dict") else dict(existing)
        d["enabled"] = False
        rules.create_or_update(args.disable, d)
        print(f"disabled {args.disable} (not deleted — re-enable by re-running "
              f"without --disable)")
        return 0

    if not (args.agent and args.eval_id):
        sys.exit("--agent and --eval-id are required")
    percent = validate_sampling(args.sampling_percent)
    rid = rule_id(args.agent)
    payload = rule_payload(args.agent, args.eval_id, percent,
                           args.max_hourly_runs)
    result = rules.create_or_update(rid, payload)
    print(f"rule {rid} -> sampling {percent}% of {args.agent} responses, "
          f"max {args.max_hourly_runs}/hour")
    print("results land in the Foundry Observability dashboard; alert rules "
          "attach there, not here.")
    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(main())
