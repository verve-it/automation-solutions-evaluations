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
