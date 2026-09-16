"""End-to-end replay of the committed trace sets against the committed
baselines. This is the merge gate for changes to the eval code itself: a
refactor that quietly changes what a check sees turns this red.

It needs no Azure and no network. Agent-side changes are gated by the
scheduled run in .github/workflows/evals.yml, which exports fresh traces.
"""
import json
import subprocess
import sys

import pytest

from conftest import REPO

SETS = [
    ("traces/2026-09-03-full-triage.csv",
     "baselines/full-triage-2026-09-16.json", 7, 5),
    ("traces/2026-09-15-ops-worst-case.csv",
     "baselines/ops-worst-case-2026-09-16.json", 2, 0),
]


def _convert(trace, out_dir, *extra):
    return subprocess.run(
        [sys.executable, "trace_to_eval.py", trace, "-o", str(out_dir), *extra],
        cwd=REPO, capture_output=True, text=True, check=True)


def _score(jsonl, *extra):
    return subprocess.run(
        [sys.executable, "run_evals.py", str(jsonl),
         "--expected", "expected.json", *extra],
        cwd=REPO, capture_output=True, text=True)


@pytest.mark.parametrize("trace,baseline,runs,passing", SETS)
def test_frozen_set_matches_its_baseline(tmp_path, trace, baseline, runs,
                                         passing):
    _convert(trace, tmp_path)
    jsonl = tmp_path / "eval_runs.jsonl"
    rows = [json.loads(l) for l in jsonl.read_text().splitlines() if l.strip()]
    assert len(rows) == runs

    result = _score(jsonl, "--baseline", baseline)
    assert "no change against baseline" in result.stdout, result.stdout
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("trace,baseline,runs,passing", SETS)
def test_gating_verdicts_are_stable(tmp_path, trace, baseline, runs, passing):
    _convert(trace, tmp_path)
    out = tmp_path / "results.json"
    _score(tmp_path / "eval_runs.jsonl", "--json", str(out))
    rows = json.loads(out.read_text())
    assert sum(1 for r in rows if r["passed"]) == passing


def test_without_a_baseline_a_gating_failure_exits_non_zero(tmp_path):
    _convert(SETS[1][0], tmp_path)
    assert _score(tmp_path / "eval_runs.jsonl").returncode == 1


def test_known_bad_set_still_fails_every_way_we_expect(tmp_path):
    """If this set starts passing, suspect the check before celebrating."""
    _convert(SETS[1][0], tmp_path)
    out = tmp_path / "results.json"
    _score(tmp_path / "eval_runs.jsonl", "--json", str(out))
    rows = json.loads(out.read_text())
    assert all(r["checks"]["no_wasted_calls"]["passed"] is False for r in rows)
    assert all(r["checks"]["no_search_cascade"]["passed"] is False
               for r in rows)


def test_manifest_turns_on_generated_argument_validation(tmp_path):
    """The ops runs are on ConnectwiseMCP v1. With a schema for that version,
    every unsupported cw_resolve reference type is caught before the call."""
    _convert(SETS[1][0], tmp_path,
             "--tool-defs", "tests/fixtures/connectwisemcp-v1-partial.json")
    out = tmp_path / "results.json"
    _score(tmp_path / "eval_runs.jsonl", "--json", str(out))
    rows = json.loads(out.read_text())
    args_checks = [r["checks"]["valid_tool_args"] for r in rows]
    assert all(c["passed"] is False for c in args_checks)
    assert all("reference_type" in c["reason"] for c in args_checks)
    # and the runs become scorable by the Foundry evaluators
    assert all(r["checks"]["evaluator_ready"]["passed"] is True for r in rows)


def test_intent_keying_still_resolves_on_the_full_triage_set(tmp_path):
    """Every trajectory verdict depends on this. When the hand-off format
    stopped matching, the checks skipped and the diff read 'no change'."""
    _convert(SETS[0][0], tmp_path)
    rows = [json.loads(l) for l in
            (tmp_path / "eval_runs.jsonl").read_text().splitlines() if l.strip()]
    resolved = [r for r in rows if r["intent"]]
    assert len(resolved) == 5
    assert {r["intent"] for r in resolved} == {"Full Triage", "Write Request"}
    assert all(r["started"].startswith("2026-09-03T") for r in rows)


def test_the_two_agents_are_on_different_toolbox_versions(tmp_path):
    """Not cosmetic: the ops agent writes to the system of record and is a
    major version behind the analysis agent."""
    _convert(SETS[0][0], tmp_path)
    rows = [json.loads(l) for l in
            (tmp_path / "eval_runs.jsonl").read_text().splitlines() if l.strip()]
    versions = {r["run_agent"]: {t["version"] for t in r["mcp_toolboxes"]}
                for r in rows if r["mcp_toolboxes"]}
    assert versions["triage-analysis-agent"] == {"5"}
    assert versions["connectwise-operations-agent"] == {"1"}
