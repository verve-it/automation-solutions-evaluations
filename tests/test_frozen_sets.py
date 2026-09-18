"""End-to-end replay of the committed trace sets against the committed
baselines. This is the merge gate for changes to the eval code itself: a
refactor that quietly changes what a check sees turns this red.

It needs no Azure and no network. Agent-side changes are gated by the
scheduled run in .github/workflows/evals.yml, which exports fresh traces.
"""
import json
import os
import subprocess
import sys

import pytest

from conftest import REPO

SETS = [
    ("traces/2026-09-03-full-triage.json",
     "baselines/full-triage-2026-09-18.json", 7, 5),
    ("traces/2026-09-15-ops-worst-case.json",
     "baselines/ops-worst-case-2026-09-18.json", 2, 0),
]


# The baselines are frozen WITH the tool manifests (see the `baselines` target
# in the Makefile). Converting without them makes valid_tool_args unscored and
# evaluator_ready fail, which reads as a baseline diff on every run.
TOOL_DEFS = ["--tool-defs", "tool_manifests/"]


def _convert(trace, out_dir, *extra):
    return _convert_bare(trace, out_dir, *TOOL_DEFS, *extra)


def _convert_bare(trace, out_dir, *extra):
    """Convert with exactly the flags given — no manifests unless asked."""
    return subprocess.run(
        [sys.executable, "trace_to_eval.py", trace, "-o", str(out_dir),
         *extra],
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


def test_a_declared_enum_would_catch_the_reference_type_failures(tmp_path):
    """Mechanism test, NOT a measurement.

    tests/fixtures/connectwisemcp-v1-partial.json is two hand-written tools
    whose `reference_type` carries an `enum`. Given that, generated validation
    catches every unsupported value before the call. This proves the validator
    works; it says nothing about the real toolbox. See the next test.
    """
    _convert_bare(SETS[1][0], tmp_path,
                  "--tool-defs", "tests/fixtures/connectwisemcp-v1-partial.json")
    out = tmp_path / "results.json"
    _score(tmp_path / "eval_runs.jsonl", "--json", str(out))
    rows = json.loads(out.read_text())
    args_checks = [r["checks"]["valid_tool_args"] for r in rows]
    assert all(c["passed"] is False for c in args_checks)
    assert all("reference_type" in c["reason"] for c in args_checks)
    # and the runs become scorable by the Foundry evaluators
    assert all(r["checks"]["evaluator_ready"]["passed"] is True for r in rows)


def test_the_enum_catches_the_invalid_reference_type(tmp_path):
    """The positive assertion, replacing test_the_real_manifest_does_not_catch_them.

    cwpsa-mcp cbf4e2b types reference_type as a closed 20-value Literal, so
    FastMCP emits an enum and the manifest carries it. "severity" is now caught
    by the SCHEMA, before the call, instead of by string-matching the server's
    error text after it.

    Both matter, so both are asserted: no_wasted_calls reads the server's
    reply, valid_tool_args reads the contract. Only the second generalises to
    a tool nobody has written a check for.
    """
    _convert(SETS[0][0], tmp_path)          # "severity" is in the full-triage set
    out = tmp_path / "results.json"
    _score(tmp_path / "eval_runs.jsonl", "--json", str(out))
    rows = json.loads(out.read_text())

    bad = [r for r in rows if r["checks"]["valid_tool_args"]["passed"] is False]
    assert len(bad) == 1, [r["run_agent"] for r in bad]
    reason = bad[0]["checks"]["valid_tool_args"]["reason"]
    assert "'reference_type'='severity'" in reason, reason
    assert "not in [" in reason, reason
    # the enum is 20 long; the message shows a prefix and must say so rather
    # than reading as the whole valid set
    assert "more]" in reason, reason


def test_the_enum_does_not_fire_on_the_resolver_bugs(tmp_path):
    """The known-bad set still passes valid_tool_args, and that is correct.

    type/subtype/item/site are all valid reference types. Those runs failed
    because resolve_reference dropped `context` (fixed upstream in cbf4e2b),
    not because the arguments were wrong. No schema of any kind catches a
    behavioural bug, and a check that "caught" them would be reading tea
    leaves.

    This is the guard against tightening valid_tool_args until it turns green
    on the known-bad set for the wrong reason.
    """
    _convert(SETS[1][0], tmp_path)
    out = tmp_path / "results.json"
    _score(tmp_path / "eval_runs.jsonl", "--json", str(out))
    rows = json.loads(out.read_text())
    assert all(r["checks"]["valid_tool_args"]["passed"] is True for r in rows)
    assert all(r["checks"]["valid_tool_args"]["checked"] > 0 for r in rows)
    assert all(r["checks"]["evaluator_ready"]["passed"] is True for r in rows)
    # they are still caught, by the behavioural checks that should catch them
    assert all(r["passed"] is False for r in rows)


def test_the_manifest_declares_the_reference_type_enum(tmp_path):
    """Regression lock on the contract itself.

    If a re-extraction ever drops the enum -- a bad --from-source run, a
    revert upstream -- valid_tool_args goes quietly vacuous again. It passed
    100% of a trace set chosen for being full of bad calls for exactly this
    reason, and nothing in the output said so.
    """
    with open(os.path.join(REPO, "tool_manifests",
                           "connectwisemcp.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    resolve = next(t for t in manifest["tools"] if t["name"] == "cw_resolve")
    enum = resolve["parameters"]["properties"]["reference_type"].get("enum")
    assert enum, "cw_resolve.reference_type lost its enum"
    assert "severity" not in enum
    for expected in ("company", "site", "type", "subtype", "item", "status"):
        assert expected in enum, expected
    assert enum == sorted(enum), "enum order should be stable for diffs"


def test_the_manifest_covers_every_tool_the_agents_called(tmp_path):
    with open(os.path.join(REPO, "tool_manifests",
                           "connectwisemcp.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    declared = {t["name"] for t in manifest["tools"]}
    assert manifest["versions"] == ["*"]
    assert all(t.get("parameters") for t in manifest["tools"])

    called = set()
    for trace, *_ in SETS:
        _convert(trace, tmp_path / "cov")
        for line in (tmp_path / "cov" / "eval_runs.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            for name in json.loads(line)["tool_names"]:
                bare = name.split("___")[-1]
                if bare.startswith("cw_"):
                    called.add(bare)
    assert called, "no ConnectWise calls found in the frozen sets"
    assert called <= declared, f"not in the manifest: {sorted(called - declared)}"


def test_every_agent_now_has_a_trajectory_expectation(tmp_path):
    """Three of seven runs used to skip the trajectory check, including both
    ops runs — the agent that writes to the system of record."""
    _convert(SETS[0][0], tmp_path)
    out = tmp_path / "results.json"
    _score(tmp_path / "eval_runs.jsonl", "--json", str(out))
    rows = json.loads(out.read_text())
    assert all(r["checks"]["trajectory"]["passed"] is True for r in rows)


def test_ops_run_that_never_read_the_ticket_fails_trajectory(tmp_path):
    """73d29f4c updated a ticket without ever calling cw_get_ticket."""
    _convert(SETS[1][0], tmp_path)
    out = tmp_path / "results.json"
    _score(tmp_path / "eval_runs.jsonl", "--json", str(out))
    rows = {r["orchestration_id"][:8]: r for r in json.loads(out.read_text())}
    traj = rows["73d29f4c"]["checks"]["trajectory"]
    assert traj["passed"] is False
    assert "cw_get_ticket" in traj["reason"]


def test_every_truncation_in_the_frozen_sets_is_cw_query_not_load_skill(tmp_path):
    """The handoff expected load_skill to be the truncation victim because it
    is the largest payload. It is not — it survives past 8192 because gen_ai.*
    attributes are largely exempt. Storing skills by reference is worth doing
    for skill-version comparison, but it is not the truncation fix."""
    for trace, *_ in SETS:
        _convert(trace, tmp_path)
        rows = [json.loads(l) for l in
                (tmp_path / "eval_runs.jsonl").read_text().splitlines()
                if l.strip()]
        assert sum(r["truncated_skills"] for r in rows) == 0


def test_skills_in_force_are_stable_across_both_orchestrations(tmp_path):
    _convert(SETS[0][0], tmp_path)
    rows = [json.loads(l) for l in
            (tmp_path / "eval_runs.jsonl").read_text().splitlines() if l.strip()]
    by_name = {}
    for r in rows:
        for s in r["skills_in_force"]:
            by_name.setdefault(s["skill_name"], set()).add(s["sha256"])
    assert by_name, "no skills recorded"
    drifted = {k: v for k, v in by_name.items() if len(v) > 1}
    assert not drifted, f"same skill, different content: {drifted}"


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


def test_binding_revisions_differ_between_agents_but_the_contract_does_not(
        tmp_path):
    """The toolbox revision is a binding edit counter, not a schema version.
    The ops agent shows v1 and the analysis agent v5 for the same toolbox, and
    the tool descriptions are byte-identical across both — which is why a
    manifest normally declares "versions": ["*"]."""
    _convert(SETS[0][0], tmp_path)
    rows = [json.loads(l) for l in
            (tmp_path / "eval_runs.jsonl").read_text().splitlines() if l.strip()]
    revisions = {r["run_agent"]: {t["version"] for t in r["mcp_toolboxes"]}
                 for r in rows if r["mcp_toolboxes"]}
    assert revisions["triage-analysis-agent"] == {"5"}
    assert revisions["connectwise-operations-agent"] == {"1"}


def test_foundry_conversion_maps_actions_to_tool_calls(tmp_path):
    """submit_to_foundry.py renames `actions` to the `tool_calls` field the
    Foundry agent evaluators expect, and parses the argument strings."""
    import submit_to_foundry

    _convert(SETS[0][0], tmp_path)
    runs = [json.loads(l) for l in
            (tmp_path / "eval_runs.jsonl").read_text().splitlines() if l.strip()]
    rows = submit_to_foundry.to_foundry_rows(runs)
    assert len(rows) == len(runs)
    with_calls = [r for r in rows if r["tool_calls"]]
    assert with_calls
    call = with_calls[0]["tool_calls"][0]
    assert set(call) == {"type", "name", "arguments"}
    assert isinstance(call["arguments"], (dict, str))


def test_foundry_sampling_is_reproducible_for_a_seed(tmp_path):
    import submit_to_foundry

    rows = [{"n": i} for i in range(50)]
    a = submit_to_foundry.select(rows, 5, seed=7)
    b = submit_to_foundry.select(rows, 5, seed=7)
    assert a == b and len(a) == 5
    assert submit_to_foundry.select(rows, 0, seed=7) == rows
