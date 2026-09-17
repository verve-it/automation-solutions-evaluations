"""The deterministic checks, as Foundry code-based evaluators.

Each `grade(sample, item) -> float` is registered separately, because a
code-based evaluator returns exactly one float between 0.0 and 1.0. That is
the real cost of the port: `run_evals.py` returns a verdict AND a reason
("4 avoidable call(s): empty_failedx2, missing_scriptx1"), and a score alone
cannot say why. Keep run_evals.py as the local gate that explains itself; use
these for catalog membership, portal history and continuous evaluation.

Scoring convention, so a threshold means the same thing everywhere:

    1.0            clean
    0.0 < s < 1.0  degraded in proportion to how much
    0.0            the failure this check exists to catch

`pass_threshold` is an init parameter on every registered evaluator. 1.0
reproduces `run_evals.py` gating exactly; anything lower tolerates a fraction.

`sample` is always empty for a dataset evaluation — all data is in `item`.
"""

from _shared import (AVOIDABLE, CASCADE_MIN, TRUNC_BOUNDARY, base_tool_name,
                     outcomes, ratio_score)


def grade_no_wasted_calls(sample, item):
    """Calls that could not have succeeded."""
    steps = outcomes(item)
    bad = sum(1 for s in steps if s["error_kind"] in AVOIDABLE)
    return ratio_score(bad, len(steps))


def grade_no_tool_errors(sample, item):
    """Any tool error at all. Broader than the above; tracks overall health."""
    steps = outcomes(item)
    return ratio_score(sum(1 for s in steps if s["errored"]), len(steps))


def grade_no_dead_ends(sample, item):
    """Calls that succeed but return nothing — the hallucinated-entity
    signal."""
    steps = outcomes(item)
    return ratio_score(sum(1 for s in steps if s["empty"]), len(steps))


def grade_no_search_cascade(sample, item):
    """Four or more consecutive fruitless calls to one tool.

    Three in a row is ordinary enumeration. Four is an agent guessing at a
    vocabulary it has no schema for. Scored by the worst cascade's share of
    the run, so one long cascade in a short run scores worse than one in a
    long run.
    """
    steps = outcomes(item)
    if not steps:
        return 1.0
    worst, run_len, run_tool = 0, 0, None
    for s in steps:
        if (s["errored"] or s["empty"]) and (run_tool is None
                                             or run_tool == s["tool"]):
            run_tool = s["tool"]
            run_len += 1
        else:
            worst = max(worst, run_len)
            run_len = 1 if (s["errored"] or s["empty"]) else 0
            run_tool = s["tool"] if run_len else None
    worst = max(worst, run_len)
    if worst < CASCADE_MIN:
        return 1.0
    return ratio_score(worst, len(steps))


def grade_no_truncation(sample, item):
    """Results at exactly 8192 chars are cut mid-payload, so the agent
    reasoned over incomplete data."""
    steps = outcomes(item)
    return ratio_score(sum(1 for s in steps if s["truncated"]), len(steps))


def grade_trajectory(sample, item):
    """In-order match against ground truth, extras allowed.

    Returns RECALL, not F1. Extra steps are allowed by design — a correct run
    routinely takes 51 of them — so precision is low on healthy runs and an
    F1 threshold would fail everything. Recall at 1.0 is exactly the gate
    `check_trajectory` applies. run_evals.py still reports precision and F1
    locally, in the shape Task Navigation Efficiency uses.
    """
    expected = item.get("expected_actions") or []
    if not expected:
        return 1.0                       # nothing to score against
    actual = [o["tool"] for o in outcomes(item)]
    i = matched = 0
    for step in actual:
        if i < len(expected) and step == expected[i]:
            matched += 1
            i += 1
    return matched / float(len(expected))


def grade_valid_tool_args(sample, item):
    """Argument validation generated from the tool schema rather than written.

    The whole point of the MCP manifest: every unsupported cw_resolve
    reference type is caught before the call is worth making.
    """
    schemas = {}
    for d in item.get("tool_definitions") or []:
        params = d.get("parameters") or d.get("inputSchema")
        if isinstance(params, dict) and params.get("properties") is not None:
            schemas[base_tool_name(d.get("name", ""))] = params
    if not schemas:
        return 1.0                       # nothing to validate against

    types = {"string": str, "number": (int, float), "integer": int,
             "boolean": bool, "object": dict, "array": list,
             "null": type(None)}
    checked = problems = 0
    for step in outcomes(item):
        schema = schemas.get(base_tool_name(step["tool"]))
        args = step["arguments"]
        if schema is None or not isinstance(args, dict):
            continue
        checked += 1
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in args:
                problems += 1
        if schema.get("additionalProperties") is False:
            problems += sum(1 for n in args if n not in props)
        for name, value in args.items():
            spec = props.get(name)
            if not isinstance(spec, dict):
                continue
            declared = spec.get("type")
            if declared:
                wanted = declared if isinstance(declared, list) else [declared]
                ok = False
                for t in wanted:
                    py = types.get(t)
                    if py is None:
                        ok = True
                    elif t in ("number", "integer") and isinstance(value, bool):
                        continue
                    elif isinstance(value, py):
                        ok = True
                if not ok:
                    problems += 1
            if "enum" in spec and value not in spec["enum"]:
                problems += 1
    return ratio_score(problems, checked) if checked else 1.0


def grade_cost_latency(sample, item):
    """Spend and wall clock against a declared budget.

    Unlike the others this needs a budget to mean anything, so it scores 1.0
    until `max_tokens` / `max_duration_ms` are supplied as init parameters.
    """
    # Usage arrives as flattened `usage_*` columns; a nested object would be
    # rejected by the datasource validator. `usage` is still accepted for a
    # dataset built before the flattening.
    usage = item.get("usage") or {}
    if not usage:
        usage = {k[len("usage_"):]: v for k, v in item.items()
                 if k.startswith("usage_")}
    budget_tokens = float(item.get("max_tokens") or 0)
    budget_ms = float(item.get("max_duration_ms") or 0)
    if not (budget_tokens or budget_ms):
        return 1.0
    scores = []
    if budget_tokens:
        spent = (usage.get("uncached_input_tokens", 0)
                 + usage.get("output_tokens", 0))
        scores.append(max(0.0, min(1.0, 1.0 - max(0.0, spent - budget_tokens)
                                   / budget_tokens)))
    if budget_ms:
        took = float(item.get("duration_ms") or 0)
        scores.append(max(0.0, min(1.0, 1.0 - max(0.0, took - budget_ms)
                                   / budget_ms)))
    return min(scores)


# name -> (function, display name, description, categories, pass_threshold,
#          gating)
#
# `categories` is an enum: quality, safety, agents, business. Anything else is
# rejected at registration with
# "Could not convert to type 'EvaluatorCategory'".
#
# `pass_threshold` is an init parameter on the registered evaluator, so the
# tolerance lives in Foundry rather than in the score. 0.75 on dead ends is
# run_evals.py's `max_empty_rate` of 0.25, expressed the other way up.
EVALUATORS = {
    "cw_no_wasted_calls": (
        grade_no_wasted_calls, "No wasted calls",
        "Tool calls that could not have succeeded: missing_script, "
        "invalid_reference_type, invalid_entity, invalid_projection_field, "
        "empty_failed.", ["agents", "quality"], 1.0, True),
    "cw_no_tool_errors": (
        grade_no_tool_errors, "No tool errors",
        "Any tool error. Broader than wasted calls; tracks overall health.",
        ["agents", "quality"], 1.0, False),
    "cw_no_dead_ends": (
        grade_no_dead_ends, "No dead ends",
        "Calls that succeeded but returned nothing — the hallucinated-entity "
        "signal.", ["agents", "quality"], 0.75, True),
    "cw_no_search_cascade": (
        grade_no_search_cascade, "No search cascade",
        "Four or more consecutive fruitless calls to one tool: an agent "
        "guessing at a vocabulary it has no schema for.", ["agents", "quality"], 1.0, False),
    "cw_no_truncation": (
        grade_no_truncation, "No truncation",
        "Tool results cut at exactly 8192 chars, so the agent reasoned over "
        "incomplete data.", ["agents", "quality"], 1.0, False),
    "cw_trajectory": (
        grade_trajectory, "Trajectory F1",
        "In-order match against ground-truth tool sequence, extras allowed. "
        "Reports F1.", ["agents", "quality"], 1.0, True),
    "cw_valid_tool_args": (
        grade_valid_tool_args, "Valid tool arguments",
        "Arguments validated against the tool's own JSON schema: required, "
        "type, enum, unexpected.", ["agents", "quality"], 1.0, True),
    "cw_cost_latency": (
        grade_cost_latency, "Cost and latency",
        "Tokens and wall clock against a declared budget. Scores 1.0 until a "
        "budget is set.", ["agents", "business"], 1.0, False),
}
