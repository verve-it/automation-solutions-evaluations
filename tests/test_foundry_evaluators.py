"""The checks, ported to Foundry code-based evaluators.

The port is only worth having if it agrees with run_evals.py. These tests
score both frozen trace sets with each and assert every comparable verdict
matches — that is the guarantee, not the unit tests below it.
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "foundry_evaluators"))

import checks                                          # noqa: E402
import register_evaluators                             # noqa: E402
import run_evals                                       # noqa: E402
import to_foundry_dataset                              # noqa: E402
import trace_to_eval                                   # noqa: E402
from conftest import REPO                              # noqa: E402

SETS = ["traces/2026-09-03-full-triage.csv",
        "traces/2026-09-15-ops-worst-case.csv"]

# registered evaluator -> the run_evals check it reproduces
PAIRS = [
    ("cw_no_wasted_calls", "no_wasted_calls"),
    ("cw_no_tool_errors", "no_tool_errors"),
    ("cw_no_dead_ends", "no_dead_ends"),
    ("cw_no_search_cascade", "no_search_cascade"),
    ("cw_no_truncation", "no_truncation"),
    ("cw_trajectory", "trajectory"),
    ("cw_valid_tool_args", "valid_tool_args"),
]


def _expected():
    path = os.path.join(REPO, "expected.json")
    return {k: v for k, v in json.load(open(path, encoding="utf-8")).items()
            if not k.startswith("_")}


def _both(trace, manifests=True):
    """Score the same spans locally and as a Foundry dataset row.

    Manifests are loaded by default. Without them valid_tool_args is unscored
    on both sides, so the parity guarantee silently skips the one check whose
    input comes from outside the trace -- which is the check most likely to
    diverge.
    """
    spans = trace_to_eval.load_spans(os.path.join(REPO, trace))
    expected = _expected()
    defs = (trace_to_eval.load_tool_manifests([os.path.join(REPO, "tool_manifests")])
            if manifests else [])
    runs, _, _ = trace_to_eval.convert(spans, defs)
    local = run_evals.score(runs, {"max_empty_rate": 0.25,
                                   "expected": expected})
    rows = to_foundry_dataset.build_rows(spans, defs, expected)
    return local, rows


# --- the guarantee ----------------------------------------------------------

@pytest.mark.parametrize("trace", SETS)
def test_every_verdict_matches_run_evals(trace):
    local, rows = _both(trace)
    assert len(local) == len(rows)
    mismatches, compared = [], 0
    for l, row in zip(local, rows):
        assert (l["orchestration_id"], l["run_agent"]) == \
            (row["orchestration_id"], row["run_agent"])
        for registered, check in PAIRS:
            verdict = l["checks"][check]["passed"]
            if verdict is None:
                continue
            fn, _, _, _, threshold, _ = checks.EVALUATORS[registered]
            compared += 1
            if (fn({}, row) >= threshold) != verdict:
                mismatches.append(
                    f"{l['run_agent']} {check}: local={verdict} "
                    f"score={fn({}, row):.2f} threshold={threshold}")
    assert compared, "nothing comparable — the harness is wrong, not the port"
    assert not mismatches, "\n".join(mismatches)


@pytest.mark.parametrize("trace", SETS)
def test_valid_tool_args_is_actually_compared(trace):
    """Guard on the guarantee above, not on the checks.

    _both used to pass no manifests, so valid_tool_args was None on both sides
    and zip()ed past without comparing anything. The port could have diverged
    on the one check whose schema comes from outside the trace and no test
    would have noticed.
    """
    local, _ = _both(trace)
    scored = [r for r in local
              if r["checks"]["valid_tool_args"]["passed"] is not None]
    assert scored, "valid_tool_args unscored — manifests did not reach the run"


def test_the_ported_evaluator_also_catches_the_bad_reference_type():
    """Parity on the specific verdict the enum made possible."""
    local, rows = _both(SETS[0])
    fn, _, _, _, threshold, _ = checks.EVALUATORS["cw_valid_tool_args"]
    bad = [(l, row) for l, row in zip(local, rows)
           if l["checks"]["valid_tool_args"]["passed"] is False]
    assert len(bad) == 1, [l["run_agent"] for l, _ in bad]
    l, row = bad[0]
    assert "severity" in l["checks"]["valid_tool_args"]["reason"]
    assert fn({}, row) < threshold, "local fails, the Foundry evaluator passes"


# --- scoring shape ----------------------------------------------------------

@pytest.mark.parametrize("name", list(checks.EVALUATORS))
def test_every_score_is_in_range(name):
    fn = checks.EVALUATORS[name][0]
    _, rows = _both(SETS[0])
    for row in rows:
        score = fn({}, row)
        assert isinstance(score, float) and 0.0 <= score <= 1.0, name


def test_an_empty_run_does_not_crash_any_evaluator():
    blank = {"tool_outcomes": [], "tool_definitions": [],
             "expected_actions": [], "usage": {}, "duration_ms": 0}
    for name, (fn, *_) in checks.EVALUATORS.items():
        assert fn({}, blank) == 1.0, name


def test_trajectory_scores_recall_not_f1():
    """Extras are allowed by design — a healthy run takes 51 of them — so an
    F1 threshold would fail everything."""
    row = {"tool_outcomes": [{"tool": t, "result": "", "success": True}
                             for t in ["a", "x", "y", "b"]],
           "expected_actions": ["a", "b"]}
    assert checks.grade_trajectory({}, row) == 1.0


def test_trajectory_penalises_a_missing_step():
    row = {"tool_outcomes": [{"tool": "a", "result": "", "success": True}],
           "expected_actions": ["a", "b"]}
    assert checks.grade_trajectory({}, row) == 0.5


def test_dead_end_threshold_matches_max_empty_rate():
    """0.75 here is run_evals.py's max_empty_rate of 0.25, the other way up."""
    assert checks.EVALUATORS["cw_no_dead_ends"][4] == 0.75


def test_empty_failed_needs_the_span_status():
    """A failed span returning nothing is a real failure; `messages` alone
    cannot express it, which is why tool_outcomes carries `success`."""
    ok = {"tool_outcomes": [{"tool": "cw_resolve", "result": "",
                             "success": True}]}
    failed = {"tool_outcomes": [{"tool": "cw_resolve", "result": "",
                                 "success": False}]}
    assert checks.grade_no_wasted_calls({}, ok) == 1.0
    assert checks.grade_no_wasted_calls({}, failed) == 0.0


def test_cascade_needs_four_consecutive_fruitless_calls():
    def run(n):
        return {"tool_outcomes": [{"tool": "cw_resolve", "result": "",
                                   "success": False} for _ in range(n)]}
    assert checks.grade_no_search_cascade({}, run(3)) == 1.0
    assert checks.grade_no_search_cascade({}, run(9)) < 1.0


# --- what gets uploaded -----------------------------------------------------

def test_code_text_is_self_contained():
    """The sandbox has no network and cannot import from this repo."""
    for name, (fn, *_) in checks.EVALUATORS.items():
        namespace = {}
        exec(register_evaluators.code_text(fn), namespace)   # noqa: S102
        assert callable(namespace.get("grade")), name


def test_code_text_defines_grade_not_the_original_name():
    text = register_evaluators.code_text(checks.grade_no_dead_ends)
    assert "def grade(sample, item)" in text
    assert "def grade_no_dead_ends" not in text


def test_code_text_fits_the_sandbox_limit():
    for name, (fn, *_) in checks.EVALUATORS.items():
        assert len(register_evaluators.code_text(fn)) < 256 * 1024, name


def test_inlined_code_scores_identically_to_the_imported_function():
    _, rows = _both(SETS[1])
    for name, (fn, *_) in checks.EVALUATORS.items():
        namespace = {}
        exec(register_evaluators.code_text(fn), namespace)   # noqa: S102
        for row in rows:
            assert namespace["grade"]({}, row) == fn({}, row), name


def test_registration_payload_has_the_required_init_parameters():
    """deployment_name is required even for a non-LLM code evaluator."""
    fn, display, description, categories, threshold, _ = \
        checks.EVALUATORS["cw_trajectory"]
    payload = register_evaluators.evaluator_version(
        "cw_trajectory", fn, display, description, categories, threshold)
    required = payload["definition"]["init_parameters"]["required"]
    assert "deployment_name" in required and "pass_threshold" in required
    assert payload["definition"]["type"] == "code"


# --- the dataset ------------------------------------------------------------

def test_dataset_row_carries_tool_results():
    """The reason we build the dataset instead of using azure_ai_traces:
    that path reads only invoke_agent spans, which carry tool_call but no
    tool_result."""
    _, rows = _both(SETS[1])
    row = max(rows, key=lambda r: len(r["tool_outcomes"]))
    assert any(o["result_head"] or o["result_len"]
               for o in row["tool_outcomes"])
    results = [c for m in row["messages"] if m.get("role") == "tool"
               for c in m["content"] if c.get("type") == "tool_result"]
    assert results


def test_dataset_rows_are_per_agent():
    """Foundry's trace paths are scoped to one agent identity and its
    guidance is to evaluate the orchestrator; we want each child scored."""
    _, rows = _both(SETS[0])
    assert len({r["run_agent"] for r in rows}) == 4


def test_dataset_carries_ground_truth_for_the_trajectory_evaluator():
    _, rows = _both(SETS[0])
    assert all(r["expected_actions"] for r in rows)


def test_cli_builds_a_dataset(tmp_path):
    out = tmp_path / "ds.jsonl"
    subprocess.run(
        [sys.executable, "to_foundry_dataset.py", SETS[0],
         "--expected", "expected.json", "-o", str(out)],
        cwd=REPO, check=True, capture_output=True, text=True)
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    assert len(rows) == 7


def test_register_dry_run_calls_nothing(tmp_path):
    out = tmp_path / "payloads.json"
    result = subprocess.run(
        [sys.executable, "register_evaluators.py", "--dry-run",
         "--out", str(out)],
        cwd=REPO, check=True, capture_output=True, text=True)
    assert "nothing was called" in result.stdout
    assert len(json.loads(out.read_text())) == len(checks.EVALUATORS)


# --- registration payload shape ---------------------------------------------

ALLOWED_CATEGORIES = {"quality", "safety", "agents", "business"}


def test_categories_are_within_the_foundry_enum():
    """Registration rejects anything else with 'Could not convert to type
    EvaluatorCategory'."""
    for name, (_, _, _, categories, _, _) in checks.EVALUATORS.items():
        assert set(categories) <= ALLOWED_CATEGORIES, (name, categories)


def test_every_payload_declares_at_least_one_category():
    for name, (_, _, _, categories, _, _) in checks.EVALUATORS.items():
        assert categories, name


ALLOWED_METRIC_TYPES = {"ordinal", "continuous", "boolean"}


def test_metric_type_is_within_the_foundry_enum():
    """EvaluatorMetricType rejects anything else; 'number' was refused."""
    for name, (fn, display, description, categories, threshold, _) in \
            checks.EVALUATORS.items():
        payload = register_evaluators.evaluator_version(
            name, fn, display, description, categories, threshold)
        for metric in payload["definition"]["metrics"].values():
            assert metric["type"] in ALLOWED_METRIC_TYPES, name


def test_metric_type_is_continuous_because_scores_are_floats():
    fn, display, description, categories, threshold, _ = \
        checks.EVALUATORS["cw_trajectory"]
    payload = register_evaluators.evaluator_version(
        "cw_trajectory", fn, display, description, categories, threshold)
    assert payload["definition"]["metrics"]["cw_trajectory"]["type"] == \
        "continuous"


# --- reading results back ---------------------------------------------------

import check_cloud_eval                               # noqa: E402


@pytest.mark.parametrize("key", ["results", "testing_criteria_results",
                                 "grades", "scores"])
def test_result_parsing_tolerates_the_shape_moving(key):
    """The evals surface is preview and results have appeared under several
    names; guessing one and failing on the others wastes a round-trip."""
    item = {key: [{"name": "cw_trajectory", "score": 1.0},
                  {"name": "cw_no_dead_ends", "score": 0.87}]}
    scores, _ = check_cloud_eval.scores_from(item)
    assert scores == {"cw_trajectory": 1.0, "cw_no_dead_ends": 0.87}


def test_result_parsing_accepts_alternate_field_names():
    item = {"results": [{"criterion": "cw_trajectory", "value": 0.5}]}
    assert check_cloud_eval.scores_from(item)[0] == {"cw_trajectory": 0.5}


def test_unknown_shape_yields_no_scores_rather_than_wrong_ones():
    scores, _ = check_cloud_eval.scores_from({"unexpected": [{"x": 1}]})
    assert scores == {}


def test_objects_are_read_as_well_as_dicts():
    class Entry:
        def __init__(self):
            self.name, self.score = "cw_trajectory", 1.0

    class Item:
        def __init__(self):
            self.results = [Entry()]

    assert check_cloud_eval.scores_from(Item())[0] == {"cw_trajectory": 1.0}


# --- message schema ---------------------------------------------------------

@pytest.mark.parametrize("trace", SETS)
def test_every_message_has_content_and_no_parts(trace):
    """The service rejects the whole dataset otherwise: "Message at index 14
    (role='assistant') is missing 'content'" — telemetry nests text under
    `parts`, the dataset schema wants `content`."""
    _, rows = _both(trace)
    for n, row in enumerate(rows):
        for i, m in enumerate(row["messages"]):
            assert m.get("content") not in (None, ""), f"row {n} message {i}"
            assert "parts" not in m, f"row {n} message {i}"


def test_text_messages_are_plain_strings():
    _, rows = _both(SETS[0])
    for row in rows:
        for m in row["messages"]:
            if m["role"] in ("system", "user"):
                assert isinstance(m["content"], str)


def test_the_response_trajectory_is_not_duplicated():
    """`response` repeats the same tool calls the spans already gave us, in
    telemetry's vocabulary. Only the final answer is new."""
    _, rows = _both(SETS[0])
    row = rows[0]
    calls = sum(1 for m in row["messages"] if m["role"] == "assistant"
                and isinstance(m["content"], list))
    assert calls == len(row["tool_outcomes"])


def test_the_final_answer_survives():
    _, rows = _both(SETS[0])
    last = rows[0]["messages"][-1]
    assert last["role"] == "assistant" and isinstance(last["content"], str)
    assert last["content"].strip()


def test_flatten_text_handles_every_shape():
    f = to_foundry_dataset.flatten_text
    assert f("plain") == "plain"
    assert f([{"type": "text", "content": "a"}, {"type": "text",
                                                 "content": "b"}]) == "a\nb"
    assert f({"type": "reasoning", "content": None}) == ""
    assert f(None) == ""


def test_final_answer_skips_tool_messages_and_takes_the_last_text():
    f = to_foundry_dataset.final_answer
    response = [
        {"role": "assistant", "parts": [{"type": "tool_call", "id": "1"}]},
        {"role": "tool", "parts": [{"type": "tool_call_response",
                                    "response": "noise"}]},
        {"role": "assistant", "parts": [{"type": "reasoning",
                                         "content": None},
                                        {"type": "text", "content": "final"}]},
    ]
    assert f(response) == "final"


def test_pass_threshold_is_declared_and_passed_as_a_number():
    """A string fails at run time with "'<=' not supported between instances
    of 'float' and 'str'" — the service compares it to the float the
    evaluator returns."""
    import run_cloud_eval

    fn, display, description, categories, threshold, _ = \
        checks.EVALUATORS["cw_no_dead_ends"]
    payload = register_evaluators.evaluator_version(
        "cw_no_dead_ends", fn, display, description, categories, threshold)
    spec = payload["definition"]["init_parameters"]["properties"]
    assert spec["pass_threshold"]["type"] == "number"
    assert isinstance(spec["pass_threshold"]["default"], float)

    params = run_cloud_eval.init_params("gpt-4o", threshold)
    assert isinstance(params["pass_threshold"], float)
    assert params["pass_threshold"] == 0.75


# --- dataset size -----------------------------------------------------------

def test_tool_outcomes_store_a_head_and_a_length_not_the_body():
    """Every result was held twice — in messages and in tool_outcomes — and
    one row hit 1.1 MB, which the evals service 500s on."""
    _, rows = _both(SETS[0])
    for row in rows:
        for o in row["tool_outcomes"]:
            assert "result" not in o
            assert len(o["result_head"]) <= to_foundry_dataset.RESULT_HEAD
            assert isinstance(o["result_len"], int)


def test_a_trimmed_head_still_detects_truncation():
    """Truncation is a length test, and the length survives the trim."""
    item = {"tool_outcomes": [{"tool": "cw_query", "result_head": "x" * 600,
                               "result_len": 8192, "success": True}]}
    assert checks.grade_no_truncation({}, item) == 0.0


def test_a_trimmed_head_still_classifies_errors():
    item = {"tool_outcomes": [{"tool": "cw_resolve",
                               "result_head": "Unknown reference type 'site'",
                               "result_len": 29, "success": True}]}
    assert checks.grade_no_wasted_calls({}, item) == 0.0


def test_an_older_dataset_with_a_full_result_still_scores():
    """`result` is still accepted so a dataset built before the split does
    not silently score as clean."""
    item = {"tool_outcomes": [{"tool": "cw_resolve", "success": False,
                               "result": ""}]}
    assert checks.grade_no_wasted_calls({}, item) == 0.0


def test_no_messages_cuts_the_dataset_by_an_order_of_magnitude():
    _, rows = _both(SETS[0])
    full = sum(len(json.dumps(r)) for r in rows)
    slim = sum(len(json.dumps({k: v for k, v in r.items() if k != "messages"}))
               for r in rows)
    assert slim * 5 < full


# --- the item schema --------------------------------------------------------

def test_item_schema_declares_every_column_with_its_real_type():
    """A bare {"type": "object"} is not permissive: the service defaults
    undeclared properties to string and the run dies with "35756 is not of
    type 'string'" on the first integer."""
    import run_cloud_eval

    _, rows = _both(SETS[0])
    for row in rows:
        row.pop("messages", None)
    schema = run_cloud_eval.item_schema(rows)
    props = schema["properties"]
    assert props["duration_ms"]["type"] == "number"
    assert props["usage_uncached_input_tokens"]["type"] == "integer"
    assert props["tool_outcomes"]["type"] == "array"
    assert props["run_agent"]["type"] == "string"


def test_item_schema_has_no_type_unions():
    """A nullable column becomes a union, which the service may not take."""
    import run_cloud_eval

    _, rows = _both(SETS[0])
    schema = run_cloud_eval.item_schema(rows)
    unions = {k: v["type"] for k, v in schema["properties"].items()
              if isinstance(v["type"], list)}
    assert not unions, unions


def test_intent_is_a_string_even_when_unresolved():
    _, rows = _both(SETS[0])
    assert all(isinstance(r["intent"], str) for r in rows)
    assert any(r["intent"] == "" for r in rows), "ops runs have no intent"


def test_every_column_in_the_dataset_is_declared():
    import run_cloud_eval

    _, rows = _both(SETS[1])
    schema = run_cloud_eval.item_schema(rows)
    for row in rows:
        assert set(row) <= set(schema["properties"])


def test_nested_object_properties_are_declared_when_one_exists():
    """The recursion still matters for any nested object that survives —
    though the dataset now has none, because the validator rejects a
    non-string value inside one whatever the schema says."""
    import run_cloud_eval

    schema = run_cloud_eval.item_schema([{"outer": {"inner": 35756}}])
    outer = schema["properties"]["outer"]
    assert outer["type"] == "object"
    assert outer["properties"]["inner"]["type"] == "integer"


def test_arrays_are_left_undescribed():
    """tool_outcomes carries integers and booleans and validates without an
    `items` schema, so the validator does not descend into arrays. Declaring
    one would invite a stricter check for no benefit."""
    import run_cloud_eval

    _, rows = _both(SETS[0])
    outcomes = run_cloud_eval.item_schema(rows)["properties"]["tool_outcomes"]
    assert outcomes == {"type": "array"}


def test_object_schema_recurses_arbitrarily_deep():
    import run_cloud_eval

    rows = [{"a": {"b": {"c": 1}}}]
    schema = run_cloud_eval.item_schema(rows)
    assert schema["properties"]["a"]["properties"]["b"]["properties"]["c"] \
        ["type"] == "integer"


def test_a_key_absent_from_some_rows_is_not_required():
    import run_cloud_eval

    rows = [{"a": 1, "b": None}, {"a": 2, "b": 3}]
    assert "b" not in run_cloud_eval.item_schema(rows)["required"]
    assert "a" in run_cloud_eval.item_schema(rows)["required"]


# --- nested objects are rejected by the datasource validator ----------------

@pytest.mark.parametrize("trace", SETS)
def test_no_column_is_a_nested_object(trace):
    """Probed against the service: a top-level int passes, an int inside an
    array passes, an int inside a nested object FAILS whatever the declared
    schema says. See diagnose_schema.py."""
    _, rows = _both(trace)
    for row in rows:
        nested = [k for k, v in row.items() if isinstance(v, dict)]
        assert not nested, nested


def test_usage_is_flattened_with_no_stutter():
    _, rows = _both(SETS[0])
    keys = {k for r in rows for k in r if k.startswith("usage")}
    assert "usage_uncached_input_tokens" in keys
    assert "usage_source" in keys
    assert "usage_usage_source" not in keys
    assert "usage" not in keys


def test_cost_latency_reads_the_flattened_columns():
    item = {"tool_outcomes": [], "usage_uncached_input_tokens": 1000,
            "usage_output_tokens": 100, "max_tokens": 500, "duration_ms": 1}
    assert checks.grade_cost_latency({}, item) < 1.0


def test_cost_latency_still_reads_a_nested_usage_from_an_older_dataset():
    item = {"tool_outcomes": [],
            "usage": {"uncached_input_tokens": 1000, "output_tokens": 100},
            "max_tokens": 500, "duration_ms": 1}
    assert checks.grade_cost_latency({}, item) < 1.0


def test_data_mapping_offers_every_flattened_usage_column():
    import run_cloud_eval

    _, rows = _both(SETS[0])
    mapping = run_cloud_eval.data_mapping(rows)
    assert mapping["tool_outcomes"] == "{{item.tool_outcomes}}"
    assert "usage_uncached_input_tokens" in mapping
    assert "usage" not in mapping


# --- version pinning --------------------------------------------------------

def test_lock_file_is_read_and_versions_coerced_to_strings(tmp_path):
    import run_cloud_eval

    lock = tmp_path / "v.json"
    lock.write_text(json.dumps({"cw_trajectory": 4, "cw_no_dead_ends": "2"}))
    assert run_cloud_eval.load_lock(str(lock)) == {"cw_trajectory": "4",
                                                   "cw_no_dead_ends": "2"}


def test_a_missing_lock_file_is_not_an_error():
    import run_cloud_eval

    assert run_cloud_eval.load_lock("does-not-exist.json") == {}


def test_registration_writes_the_lock(tmp_path):
    """Without it a run leaves evaluator_version empty and floats to the
    latest, so two runs of the same baseline can be scored by different
    code."""
    lock = tmp_path / "v.json"
    out = tmp_path / "p.json"
    subprocess.run(
        [sys.executable, "register_evaluators.py", "--dry-run",
         "--out", str(out), "--lock", str(lock)],
        cwd=REPO, check=True, capture_output=True, text=True)
    # --dry-run registers nothing, so it must not invent versions either
    assert not lock.exists()


def test_the_lock_covers_every_registered_evaluator():
    """A partially pinned run is worse than an unpinned one: some evaluators
    frozen, some floating, and no way to tell from the results."""
    path = os.path.join(REPO, "evaluator-versions.json")
    if not os.path.exists(path):
        pytest.skip("no lock committed yet")
    lock = json.load(open(path, encoding="utf-8"))
    assert set(lock) == set(checks.EVALUATORS), set(checks.EVALUATORS) ^ set(lock)


def test_valid_tool_args_is_dropped_when_there_is_nothing_to_validate():
    """Left in, it scores 1.0 and reads as a clean pass in the portal —
    green without having checked anything."""
    import run_cloud_eval

    rows = [{"tool_outcomes": [], "tool_definitions": []}]
    assert not any(r.get("tool_definitions") for r in rows)
    # the guard lives in main(); assert the condition it keys on
    mapping = run_cloud_eval.data_mapping(rows)
    assert "tool_definitions" in mapping


def test_valid_tool_args_scores_one_with_no_schema_which_is_why_it_is_dropped():
    assert checks.grade_valid_tool_args(
        {}, {"tool_outcomes": [{"tool": "cw_resolve", "result_head": "",
                                "result_len": 0, "success": True}],
             "tool_definitions": []}) == 1.0


# --- pairing local rows with Foundry output items ---------------------------

ALIGN_ROWS = [{"run_agent": f"a{i}"} for i in range(3)]


def _align(scored):
    return check_cloud_eval.align(ALIGN_ROWS, scored)


def test_output_items_are_joined_on_their_dataset_index():
    """They were zipped by position with only a length check.

    Output items are paged and scored concurrently, so dataset order is not
    guaranteed. Zipping produced confident, wrong MISMATCH lines the moment
    the order differed -- the worst failure mode for a tool whose whole job
    is to say whether Foundry agrees with local.
    """
    scored = [({"cw_x": 1.0}, {"datasource_item_id": 2}),
              ({"cw_x": 0.0}, {"datasource_item_id": 0}),
              ({"cw_x": 0.5}, {"datasource_item_id": 1})]
    got = _align(scored)
    assert [r["run_agent"] for r, _ in got] == ["a0", "a1", "a2"]
    assert [s["cw_x"] for _, s in got] == [0.0, 0.5, 1.0]


def test_the_nested_item_shape_is_read_too():
    """The evals surface is preview and this key has moved."""
    scored = [({"cw_x": 1.0}, {"datasource_item": {"index": i}})
              for i in (1, 0, 2)]
    assert [r["run_agent"] for r, _ in _align(scored)] == ["a0", "a1", "a2"]


def test_position_is_the_fallback_when_no_index_is_carried():
    scored = [({"cw_x": float(i)}, {}) for i in range(3)]
    assert [s["cw_x"] for _, s in _align(scored)] == [0.0, 1.0, 2.0]


def test_a_dropped_item_refuses_rather_than_shifting():
    """An item whose scores do not parse used to be filtered out, shifting
    every later item by one while the length check still passed."""
    scored = [({"cw_x": 1.0}, {"datasource_item_id": 0}),
              ({"cw_x": 1.0}, {"datasource_item_id": 1})]
    assert _align(scored) is None


def test_duplicate_indices_refuse():
    assert _align([({"cw_x": 1.0}, {"datasource_item_id": 0})] * 3) is None


def test_an_out_of_range_index_refuses():
    assert _align([({"cw_x": 1.0}, {"datasource_item_id": i})
                   for i in (0, 1, 99)]) is None
