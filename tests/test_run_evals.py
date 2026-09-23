"""Scoring behaviour, especially the parts that decide whether CI goes red."""
import json

import pytest

import run_evals as e


def run(**kw):
    base = {"orchestration_id": "op1", "run_agent": "a", "tool_call_count": 0,
            "tool_errors": [], "empty_results": [], "search_cascades": [],
            "truncated_results": 0, "tool_names": [], "actions": [],
            "tool_definitions": [], "usage": {}}
    base.update(kw)
    return base


def action(name, args):
    return {"role": "assistant",
            "content": [{"type": "function_call", "name": name,
                         "arguments": json.dumps(args)}]}


SCHEMA = {
    "type": "object",
    "properties": {
        "reference_type": {"type": "string",
                           "enum": ["company", "contact"]},
        "query": {"type": "string"},
        "page": {"type": "integer"},
    },
    "required": ["reference_type", "query"],
    "additionalProperties": False,
}
DEFS = [{"type": "function", "name": "cw_resolve", "parameters": SCHEMA}]


# --- generated argument validation ------------------------------------------

def test_missing_required_argument():
    assert e.validate_args({"reference_type": "company"}, SCHEMA) == \
        ["missing required 'query'"]


def test_unexpected_argument_when_additional_properties_false():
    problems = e.validate_args(
        {"reference_type": "company", "query": "x", "oops": 1}, SCHEMA)
    assert "unexpected 'oops'" in problems


def test_type_mismatch():
    problems = e.validate_args(
        {"reference_type": "company", "query": "x", "page": "1"}, SCHEMA)
    assert any("should be integer" in p for p in problems)


def test_enum_violation_is_the_cw_resolve_bug():
    """`type`, `subtype`, `item`, `site`, `impact` and `urgency` are not
    supported reference types. With a schema, that is caught before the call
    is ever worth making."""
    problems = e.validate_args(
        {"reference_type": "impact", "query": "High"}, SCHEMA)
    assert any("not in [company, contact]" in p for p in problems)


def test_valid_arguments_produce_no_problems():
    assert e.validate_args({"reference_type": "company", "query": "x"},
                           SCHEMA) == []


def test_booleans_are_not_integers():
    assert e.validate_args({"reference_type": "company", "query": "x",
                            "page": True}, SCHEMA) != []


def test_check_skips_cleanly_with_no_schema():
    res = e.check_valid_tool_args(run(actions=[action("cw_resolve", {})]), {})
    assert res["passed"] is None


def test_check_skips_when_no_call_matches_a_known_schema():
    res = e.check_valid_tool_args(
        run(tool_definitions=DEFS, actions=[action("load_skill", {})]), {})
    assert res["passed"] is None


def test_check_matches_through_the_foundry_server_prefix():
    r = run(tool_definitions=DEFS,
            actions=[action("ConnectWise-PSA-ForAgents___cw_resolve",
                            {"reference_type": "site", "query": "Main"})])
    res = e.check_valid_tool_args(r, {})
    assert res["passed"] is False
    assert res["checked"] == 1


def test_check_passes_on_good_arguments():
    r = run(tool_definitions=DEFS,
            actions=[action("cw_resolve",
                            {"reference_type": "company", "query": "x"})])
    assert e.check_valid_tool_args(r, {})["passed"] is True


# --- trajectory -------------------------------------------------------------

def test_trajectory_allows_extra_steps_but_requires_order():
    cfg = {"expected": {"a|Full Triage": ["load_skill", "cw_query"]}}
    r = run(intent="Full Triage", traj_key="a|Full Triage",
            tool_names=["load_skill", "cw_get", "cw_query"])
    res = e.check_trajectory(r, cfg)
    assert res["passed"] is True
    assert res["recall"] == 1.0
    assert res["precision"] == round(2 / 3, 2)


def test_trajectory_fails_when_an_expected_step_is_out_of_order():
    cfg = {"expected": {"a": ["load_skill", "cw_query"]}}
    res = e.check_trajectory(run(traj_key="a",
                                 tool_names=["cw_query", "load_skill"]), cfg)
    assert res["passed"] is False


def test_trajectory_ignores_the_server_label_prefix():
    """expected.json was authored from `ConnectWise-PSA-ForAgents___` traces;
    the prod toolbox of 2026-09-23 labels the same server
    `CWPSA-ForAgents-prod___`. Same path, so it must still match."""
    cfg = {"expected": {"a": ["load_skill",
                              "ConnectWise-PSA-ForAgents___cw_get_ticket",
                              "ConnectWise-PSA-ForAgents___cw_query"]}}
    r = run(traj_key="a", tool_names=["load_skill",
                                      "CWPSA-ForAgents-prod___cw_get_ticket",
                                      "CWPSA-ForAgents-prod___cw_query"])
    res = e.check_trajectory(r, cfg)
    assert res["passed"] is True
    assert res["recall"] == 1.0 and res["precision"] == 1.0


def test_trajectory_prefix_tolerance_does_not_match_a_different_tool():
    cfg = {"expected": {"a": ["ConnectWise-PSA-ForAgents___cw_get_ticket",
                              "ConnectWise-PSA-ForAgents___cw_query"]}}
    r = run(traj_key="a", tool_names=["CWPSA-ForAgents-prod___cw_get_ticket",
                                      "CWPSA-ForAgents-prod___cw_search"])
    res = e.check_trajectory(r, cfg)
    assert res["passed"] is False
    assert "ConnectWise-PSA-ForAgents___cw_query" in res["reason"]


def test_bare_agent_key_applies_to_every_intent():
    cfg = {"expected": {"a": ["load_skill"]}}
    r = run(intent="Enrichment", traj_key="a|Enrichment",
            tool_names=["load_skill"])
    assert e.check_trajectory(r, cfg)["passed"] is True


def test_missing_expectation_skips_and_names_the_key_to_author():
    res = e.check_trajectory(run(intent="Enrichment", traj_key="a|Enrichment"),
                             {"expected": {}})
    assert res["passed"] is None
    assert "a|Enrichment" in res["reason"]


# --- cascades / dead ends ---------------------------------------------------

def test_cascade_check_reports_the_longest():
    r = run(tool_call_count=20,
            search_cascades=[{"tool": "x___cw_resolve", "length": 9,
                              "args": []},
                             {"tool": "x___cw_query", "length": 4, "args": []}])
    res = e.check_no_search_cascade(r, {})
    assert res["passed"] is False
    assert "9x cw_resolve" in res["reason"]


def test_dead_end_rate_is_configurable():
    r = run(tool_call_count=10, empty_results=[{"tool": "t", "args": ""}] * 3)
    assert e.check_no_dead_ends(r, {"max_empty_rate": 0.25})["passed"] is False
    assert e.check_no_dead_ends(r, {"max_empty_rate": 0.5})["passed"] is True


# --- baseline diff ----------------------------------------------------------

def _scored(checks):
    return {"orchestration_id": "op1", "run_agent": "a", "checks": checks}


def test_pass_to_fail_is_a_regression():
    base = [_scored({"trajectory": {"passed": True, "reason": ""}})]
    now = [_scored({"trajectory": {"passed": False, "reason": "missing"}})]
    regressions, fixes, lost, new, missing = e.diff_baseline(now, base)
    assert len(regressions) == 1 and not fixes and not lost


def test_fail_to_pass_is_reported_as_a_fix():
    base = [_scored({"trajectory": {"passed": False, "reason": "missing"}})]
    now = [_scored({"trajectory": {"passed": True, "reason": ""}})]
    regressions, fixes, lost, _, _ = e.diff_baseline(now, base)
    assert len(fixes) == 1 and not regressions


def test_scored_to_skipped_is_lost_coverage_not_silence():
    """The intent-keying break turned every trajectory verdict into a skip and
    the old diff called that 'no change against baseline'."""
    base = [_scored({"trajectory": {"passed": True, "reason": ""}})]
    now = [_scored({"trajectory": {"passed": None, "reason": "no expected"}})]
    regressions, fixes, lost, _, _ = e.diff_baseline(now, base)
    assert not regressions and len(lost) == 1


def test_a_run_absent_from_the_baseline_is_not_compared():
    base = [_scored({"trajectory": {"passed": True, "reason": ""}})]
    now = [{"orchestration_id": "op2", "run_agent": "a", "checks": {}}]
    _, _, _, new, missing = e.diff_baseline(now, base)
    assert new == [("op2", "a")] and missing == [("op1", "a")]


# --- gating -----------------------------------------------------------------

def test_only_gating_checks_decide_the_verdict():
    rows = e.score([run(tool_call_count=1, truncated_results=1)], {})
    assert rows[0]["checks"]["no_truncation"]["passed"] is False
    assert rows[0]["passed"] is True


def test_every_gating_check_exists():
    assert e.GATING <= set(e.CHECKS)


# --- cost / latency: tracked, not gated -------------------------------------

def _usage(**kw):
    base = {"llm_calls": 3, "uncached_input_tokens": 1000,
            "output_tokens": 100, "cache_read_tokens": 5000,
            "peak_input_tokens": 900}
    base.update(kw)
    return base


def test_cost_latency_skips_when_no_threshold_is_set():
    """Tracking from day one, gating once we know what normal looks like."""
    res = e.check_cost_latency(run(usage=_usage(), duration_ms=1000), {})
    assert res["passed"] is None
    assert "tracking only" in res["reason"]


def test_cost_latency_fails_only_against_an_explicit_budget():
    r = run(usage=_usage(), duration_ms=600_000)
    assert e.check_cost_latency(r, {"max_tokens": 500})["passed"] is False
    assert e.check_cost_latency(r, {"max_duration_ms": 60_000})["passed"] is False
    assert e.check_cost_latency(r, {"max_tokens": 10_000,
                                    "max_duration_ms": 900_000})["passed"] is True


def test_cost_latency_counts_uncached_input_plus_output_not_cached():
    r = run(usage=_usage(uncached_input_tokens=100, output_tokens=10,
                         cache_read_tokens=10_000_000), duration_ms=1)
    assert e.check_cost_latency(r, {"max_tokens": 200})["passed"] is True


def test_cost_latency_is_not_a_gating_check():
    assert "cost_latency" not in e.GATING


def test_truncated_skill_is_called_out_in_the_truncation_reason():
    r = run(truncated_results=3, truncated_skills=1)
    assert "SKILL" in e.check_no_truncation(r, {})["reason"]


# --- tracking is not gated on llm_calls -------------------------------------

def _rollup_row(**kw):
    """A run whose tokens came from the invoke_agent roll-up: real spend,
    llm_calls 0 because no chat span carried usage."""
    row = {"traj_key": "agent|Full Triage", "duration_ms": 1000,
           "usage": {"llm_calls": 0, "uncached_input_tokens": 1234,
                     "cache_read_tokens": 99, "output_tokens": 56,
                     "peak_input_tokens": 1234, "usage_source": "rollup"},
           "skills_in_force": []}
    row.update(kw)
    return row


def test_tracking_prints_when_usage_came_from_the_rollup(capsys):
    """The gate was `if not any(usage.llm_calls)`, which is 0 for every
    roll-up run -- so the whole TRACKING table vanished for runs that had
    perfectly good token figures."""
    e.print_tracking([_rollup_row()])
    out = capsys.readouterr().out
    assert "TRACKING" in out
    assert "1,234" in out


def test_tracking_still_stays_quiet_with_no_usage_at_all(capsys):
    e.print_tracking([_rollup_row(usage={"llm_calls": 0,
                                         "usage_source": "none"})])
    assert "TRACKING" not in capsys.readouterr().out


def test_skill_drift_is_reported_independently_of_usage(capsys):
    """It shared print_tracking's early return, so a roll-up run set
    reported no drift rather than no usage."""
    rows = [_rollup_row(usage={"usage_source": "none"},
                        skills_in_force=[{"skill_name": "normalization",
                                          "sha256": "a" * 64,
                                          "truncated": False}]),
            _rollup_row(usage={"usage_source": "none"},
                        skills_in_force=[{"skill_name": "normalization",
                                          "sha256": "b" * 64,
                                          "truncated": False}])]
    e.print_skill_drift(rows)
    out = capsys.readouterr().out
    assert "SKILL DRIFT" in out and "normalization" in out


# ------------------------------------------------------- thresholds and summary

def _row(agent, gating_verdicts, extra=None):
    """A scored row shaped like run_evals writes them."""
    checks = {name: {"passed": verdict, "reason": ""}
              for name, verdict in {**(extra or {}), **gating_verdicts}.items()}
    failed = [n for n in e.GATING if checks.get(n, {}).get("passed") is False]
    return {"run_agent": agent, "intent": "Full Triage", "checks": checks,
            "failed_gating": failed, "passed": not failed}


def test_a_failing_check_lowers_its_rate():
    """The bug this replaces would have reported 100% for ever.

    A check is `{"passed": bool|None, "reason": str}`. Testing the dict for
    truthiness makes every check pass, always -- a check that cannot fail is
    not a check, and this one would have sat in a deployment gate.
    """
    rows = [_row("a", {"no_wasted_calls": True}),
            _row("b", {"no_wasted_calls": False}),
            _row("c", {"no_wasted_calls": True}),
            _row("d", {"no_wasted_calls": True})]
    overall, per_check = e.score_rates(rows)
    assert per_check["no_wasted_calls"]["rate"] == 0.75
    assert overall == 0.75


def test_a_check_that_did_not_apply_is_not_counted_either_way():
    """None means "no schema for this tool" or "no threshold set".

    Counting it as a pass inflates the score; as a failure, it fails a
    deployment for a check that never ran.
    """
    rows = [_row("a", {"valid_tool_args": True}),
            _row("b", {"valid_tool_args": None}),
            _row("c", {"valid_tool_args": False})]
    _overall, per_check = e.score_rates(rows)
    assert per_check["valid_tool_args"]["rate"] == 0.5     # 1 of 2 applicable
    assert per_check["valid_tool_args"]["applied"] == 2
    assert per_check["valid_tool_args"]["skipped"] == 1


def test_only_gating_checks_are_held_to_the_threshold(capsys):
    """A reporting check failing must not block a deploy.

    GATING decides whether a run passed. Holding a check outside it to the
    same floor would fail a deployment for something the gate itself does not
    treat as a failure.
    """
    rows = [_row("a", {"no_wasted_calls": True},
                 extra={"no_tool_errors": False}),
            _row("b", {"no_wasted_calls": True},
                 extra={"no_tool_errors": False})]
    _overall, per_check = e.score_rates(rows)
    assert per_check["no_tool_errors"]["rate"] == 0.0
    assert per_check["no_tool_errors"]["gating"] is False

    assert e.print_thresholds(rows, None, 0.9) is False
    assert "ok" in capsys.readouterr().out


def test_the_threshold_fails_when_a_gating_check_is_short(capsys):
    rows = [_row("a", {"trajectory": True}),
            _row("b", {"trajectory": False})]
    assert e.print_thresholds(rows, None, 0.9) is True
    assert "trajectory" in capsys.readouterr().out


def test_the_summary_names_what_failed(tmp_path):
    """A red build has to say which check, on which run.

    An exit code is the difference between a build someone fixes and a build
    someone reruns.
    """
    rows = [_row("triage-analysis-agent", {"no_wasted_calls": False}),
            _row("connectwise-operations-agent", {"trajectory": True})]
    path = tmp_path / "summary.md"
    text = e.write_summary(str(path), rows, "baselines/x.json", 0.9, 0.9)

    assert "1 of 2 runs passed" in text
    assert "**FAIL**" in text
    assert "triage-analysis-agent" in text
    assert "`no_wasted_calls`" in text
    # Appends: $GITHUB_STEP_SUMMARY accumulates across steps.
    e.write_summary(str(path), rows, None, None, None)
    assert path.read_text().count("## Agent evaluation") == 2
