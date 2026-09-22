"""The converter's non-obvious behaviours. Each of these was a real bug found
against real data; do not simplify one away without reproducing the case."""
import json

import pytest

import trace_to_eval as t
from conftest import invoke, span, tool_call


# --- 1. duplicate tool spans ------------------------------------------------

def test_tools_call_child_does_not_double_count():
    spans = (tool_call("cw_query", "triage-analysis-agent")
             + [invoke("triage-analysis-agent")])
    runs, _, _ = t.convert(spans)
    assert runs[0]["tool_names"] == ["cw_query"]


def test_filtering_on_operation_name_would_double_count():
    """Both spans set gen_ai.operation.name=execute_tool. The span NAME is the
    only thing that separates them."""
    spans = tool_call("cw_query", "a")
    assert all(s["d"]["gen_ai.operation.name"] == "execute_tool" for s in spans)
    assert [t.is_tool_span(s) for s in spans] == [True, False]


# --- 2. grouping is by agent name, not the span tree ------------------------

def test_child_agent_spans_group_by_name_not_parent():
    spans = [
        span("chat", "triage-orchestrator", parent_hint=""),
        *tool_call("cw_query", "triage-analysis-agent"),
    ]
    spans[1]["parent"] = "somewhere-outside-the-subtree"
    runs, _, _ = t.convert(spans)
    assert {r["run_agent"] for r in runs} == {"triage-orchestrator",
                                              "triage-analysis-agent"}


# --- 3. call_tool is a dispatcher -------------------------------------------

def test_call_tool_is_unwrapped_to_the_real_tool():
    name, args, unwrapped = t.unwrap_call_tool(
        "call_tool",
        json.dumps({"name": "cw_update", "arguments": {"entity": "tickets"}}))
    assert (name, unwrapped) == ("cw_update", True)
    assert json.loads(args) == {"entity": "tickets"}


def test_call_tool_without_inner_name_is_left_alone():
    assert t.unwrap_call_tool("call_tool", '{"oops": 1}')[2] is False
    assert t.unwrap_call_tool("call_tool", "not json")[2] is False


# --- 4. A2A calls are run boundaries ----------------------------------------

def test_a2a_call_stays_in_caller_trajectory_and_callee_is_its_own_run():
    spans = [
        *tool_call("triage-analysis-agent", "triage-orchestrator",
                   args={"request": "intent=Full Triage; ticketId=1"}),
        *tool_call("cw_query", "triage-analysis-agent"),
        invoke("triage-orchestrator", user_text="Automated flow: triage 1"),
        invoke("triage-analysis-agent", user_text="intent=Full Triage"),
    ]
    runs, _, _ = t.convert(spans)
    by_agent = {r["run_agent"]: r for r in runs}
    assert by_agent["triage-orchestrator"]["tool_names"] == \
        ["triage-analysis-agent"]
    assert by_agent["triage-orchestrator"]["a2a_calls"] == \
        ["triage-analysis-agent"]
    assert by_agent["triage-analysis-agent"]["tool_names"] == ["cw_query"]


def test_agent_missing_from_AGENT_NAMES_is_not_treated_as_a_boundary():
    """Adding an agent without updating AGENT_NAMES silently collapses it into
    the caller's trajectory. This test documents that, so the failure mode has
    a name."""
    step = t.tool_step(tool_call("triage-future-agent", "triage-orchestrator")[0])
    assert step["is_a2a"] is False


# --- 5. empty result + failed span is a real failure ------------------------

@pytest.mark.parametrize("result,ok,expected", [
    ("", True, None),
    ("", False, "empty_failed"),
    ("Unknown reference type 'impact'", True, "invalid_reference_type"),
    ("x not found in registry", True, "invalid_entity"),
    ("Error: Script missing", True, "missing_script"),
    ('{"count":0}', True, None),
])
def test_classify_error(result, ok, expected):
    assert t.classify_error(result, ok) == expected


def test_empty_markers_are_dead_ends_not_errors():
    assert t.is_empty_result('{"count": 0, "data": []}') is True
    assert t.is_empty_result('{"count": 3}') is False


# --- intent extraction ------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("intent=Full Triage; ticketId=805392; mode=Automation", "Full Triage"),
    ("intent: Information Request", "Information Request"),
    ('{"intent":"Write Request","ticketId":1}', "Write Request"),
    ("intent=full triage", "Full Triage"),          # normalised casing
])
def test_declared_intent(text, expected):
    dims = {t.K_IN_MSGS: json.dumps(
        [{"role": "user", "parts": [{"type": "text", "content": text}]}])}
    assert t.extract_intent(dims, []) == (expected, "declared")


def test_intent_is_never_guessed():
    dims = {t.K_IN_MSGS: json.dumps([{"role": "user", "content": "triage 805392"}])}
    assert t.extract_intent(dims, []) == (None, "unknown")


def test_orchestrator_intent_comes_from_its_own_a2a_call():
    steps = [{"is_a2a": True,
              "arguments": '{"request": "intent=Full Triage; ticketId=1"}'}]
    assert t.extract_intent({}, steps) == ("Full Triage", "delegated")


def test_child_intent_falls_back_to_the_callers_handoff():
    """An agent invoked twice in one orchestration is one AI Run, and the
    hand-off we read messages from may be the free-text one."""
    spans = [
        *tool_call("triage-analysis-agent", "triage-orchestrator",
                   args={"request": "intent=Full Triage; ticketId=1"},
                   ts="2026-09-03T17:00:00.000Z"),
        *tool_call("triage-analysis-agent", "triage-orchestrator",
                   args={"request": "Full Triage for ticket 1, no keyword"},
                   ts="2026-09-03T17:00:05.000Z"),
        invoke("triage-analysis-agent",
               user_text="Full Triage for ticket 1, no keyword"),
    ]
    runs, _, _ = t.convert(spans)
    analysis = next(r for r in runs if r["run_agent"] == "triage-analysis-agent")
    assert analysis["intent"] == "Full Triage"
    assert analysis["intent_source"] == "inbound"
    assert analysis["traj_key"] == "triage-analysis-agent|Full Triage"


# --- timestamps -------------------------------------------------------------

def test_portal_locale_timestamp_is_normalised():
    assert t.normalise_timestamp("9/3/2026, 5:29:42.893 PM") == \
        "2026-09-03T17:29:42.893Z"


def test_normalised_timestamps_sort_chronologically_across_a_month_boundary():
    raw = ["10/1/2026, 1:00:00.000 AM", "9/30/2026, 1:00:00.000 AM"]
    assert sorted(raw) != raw[::-1]                      # raw strings mis-sort
    assert sorted(t.normalise_timestamp(r) for r in raw) == \
        [t.normalise_timestamp(raw[1]), t.normalise_timestamp(raw[0])]


def test_iso_timestamps_pass_through_as_utc():
    assert t.normalise_timestamp("2026-09-03T19:29:42.893+02:00") == \
        "2026-09-03T17:29:42.893Z"


def test_unparseable_timestamp_is_kept_rather_than_dropped():
    assert t.normalise_timestamp("whenever") == "whenever"


def test_portal_csv_time_column_alias_is_found():
    row = {"timestamp [UTC]": "9/3/2026, 5:29:42.893 PM", "name": "x"}
    assert t._col(row, "timestamp", "TimeGenerated") == "9/3/2026, 5:29:42.893 PM"


# --- cascades ---------------------------------------------------------------

def _fruitless(tool, n):
    return [{"tool": tool, "errored": True, "empty": False,
             "arguments": f'{{"q": "{i}"}}'} for i in range(n)]


def test_three_fruitless_calls_are_enumeration_not_a_cascade():
    assert t.find_cascades(_fruitless("cw_follow_href", 3)) == []


def test_four_or_more_fruitless_calls_to_one_tool_is_a_cascade():
    found = t.find_cascades(_fruitless("cw_resolve", 9))
    assert len(found) == 1
    assert found[0]["length"] == 9
    assert found[0]["tool"] == "cw_resolve"


def test_a_successful_call_breaks_the_run():
    steps = (_fruitless("cw_resolve", 3)
             + [{"tool": "cw_resolve", "errored": False, "empty": False,
                 "arguments": "{}"}]
             + _fruitless("cw_resolve", 3))
    assert t.find_cascades(steps) == []


# --- toolbox versions -------------------------------------------------------

def test_toolbox_version_is_read_from_the_span_name():
    s = span("POST /api/projects/p/toolboxes/ConnectwiseMCP/versions/5/mcp", "a")
    assert t.find_toolboxes([s]) == [("ConnectwiseMCP", "5")]


# --- token usage ------------------------------------------------------------

def test_invoke_agent_rollup_is_not_added_to_the_chat_spans():
    chat = [span("chat gpt", "a", gen_ai__usage__input_tokens="100",
                 gen_ai__usage__output_tokens="10",
                 gen_ai__usage__cache_read__input_tokens="40")]
    roll = [span("invoke_agent a", "a", gen_ai__usage__input_tokens="100",
                 gen_ai__usage__output_tokens="10")]
    usage = t.collect_usage(chat + roll)
    assert usage["prompt_tokens_sum"] == 100
    assert usage["uncached_input_tokens"] == 60
    assert usage["llm_calls"] == 1
    assert usage["usage_source"] == "chat_spans"


def test_rollup_is_used_when_no_chat_span_carries_usage():
    roll = [span("invoke_agent a", "a", gen_ai__usage__input_tokens="500",
                 gen_ai__usage__output_tokens="7")]
    usage = t.collect_usage(roll)
    assert usage["prompt_tokens_sum"] == 500
    assert usage["usage_source"] == "rollup"


# --- manifest injection -----------------------------------------------------

def test_manifest_is_injected_for_the_toolbox_version_the_run_used(tmp_path):
    man = tmp_path / "m.json"
    man.write_text(json.dumps({
        "toolbox": "ConnectwiseMCP", "version": "5",
        "tools": [{"name": "cw_resolve", "description": "",
                   "inputSchema": {"type": "object", "properties": {}}}],
    }))
    manifests = t.load_tool_manifests([str(man)])
    spans = [
        span("POST /api/projects/p/toolboxes/ConnectwiseMCP/versions/5/mcp", "a"),
        *tool_call("cw_resolve", "a"),
    ]
    runs, _, _ = t.convert(spans, manifests)
    assert runs[0]["tool_definitions"][0]["name"] == "cw_resolve"
    assert runs[0]["tool_definitions_source"] == "manifest:ConnectwiseMCP@5"
    assert runs[0]["has_tool_definitions"] is True


def test_manifest_pinned_to_another_revision_is_not_applied():
    """Pinning is for a proven contract change; otherwise use "*"."""
    manifests = [{"toolbox": "ConnectwiseMCP", "versions": ["5"],
                  "tools": [{"name": "cw_resolve"}]}]
    spans = [
        span("POST /api/projects/p/toolboxes/ConnectwiseMCP/versions/1/mcp", "a"),
        *tool_call("cw_resolve", "a"),
    ]
    runs, _, _ = t.convert(spans, manifests)
    assert runs[0]["tool_definitions"] == []


def test_wildcard_manifest_covers_every_binding_revision():
    """The revision in the toolbox URL is a binding edit counter, not a schema
    version: the ops agent shows v1 and the analysis agent v5 for the same
    toolbox, with byte-identical tool descriptions."""
    manifests = [{"toolbox": "ConnectwiseMCP", "versions": ["*"],
                  "tools": [{"name": "cw_resolve", "parameters": {}}]}]
    for revision in ("1", "5", "17"):
        spans = [
            span(f"POST /api/p/toolboxes/ConnectwiseMCP/versions/{revision}/mcp",
                 "a"),
            *tool_call("cw_resolve", "a"),
        ]
        runs, _, _ = t.convert(spans, manifests)
        assert runs[0]["tool_definitions"][0]["name"] == "cw_resolve"


def test_manifest_without_a_versions_field_defaults_to_wildcard(tmp_path):
    man = tmp_path / "m.json"
    man.write_text(json.dumps({"toolbox": "ConnectwiseMCP", "tools": []}))
    assert t.load_tool_manifests([str(man)])[0]["versions"] == ["*"]


def test_single_version_key_is_still_accepted(tmp_path):
    man = tmp_path / "m.json"
    man.write_text(json.dumps({"toolbox": "X", "version": 3, "tools": []}))
    assert t.load_tool_manifests([str(man)])[0]["versions"] == ["3"]


def test_base_tool_name_strips_the_foundry_server_prefix():
    assert t.base_tool_name("ConnectWise-PSA-ForAgents___cw_resolve") == \
        "cw_resolve"
    assert t.base_tool_name("load_skill") == "load_skill"


# --- skills in force --------------------------------------------------------

def test_skill_bodies_are_hashed_so_rules_are_identifiable():
    """load_skill returns raw markdown with `name` and `description` in the
    frontmatter but NO version, so the content hash is the only identity a
    historical run has."""
    body = "---\nname: normalization\ndescription: x\n---\n\n# Normalization\n"
    spans = (tool_call("load_skill", "a", args={"skill_name": "normalization"},
                       result=body)
             + [invoke("a")])
    runs, _, _ = t.convert(spans)
    skill = runs[0]["skills_in_force"][0]
    assert skill["skill_name"] == "normalization"
    assert skill["bytes"] == len(body)
    assert skill["truncated"] is False
    assert len(skill["sha256"]) == 64


def test_same_skill_content_hashes_identically_across_runs():
    body = "---\nname: classification\n---\nrules"
    digests = set()
    for op in ("op1", "op2"):
        spans = tool_call("load_skill", "a", op, args={"skill_name": "x"},
                          result=body) + [invoke("a", op)]
        runs, _, _ = t.convert(spans)
        digests.add(runs[0]["skills_in_force"][0]["sha256"])
    assert len(digests) == 1


def test_a_truncated_skill_is_flagged_separately_from_a_truncated_result():
    """A skill cut mid-payload means the agent worked from incomplete RULES."""
    spans = (tool_call("load_skill", "a", args={"skill_name": "big"},
                       result="x" * t.TRUNC_BOUNDARY)
             + [invoke("a")])
    runs, _, _ = t.convert(spans)
    assert runs[0]["truncated_skills"] == 1
    assert runs[0]["skills_in_force"][0]["truncated"] is True


def test_skill_name_falls_back_to_the_frontmatter():
    spans = tool_call("load_skill", "a", args={}, result="---\nname: orch\n---")
    runs, _, _ = t.convert(spans + [invoke("a")])
    assert runs[0]["skills_in_force"][0]["skill_name"] == "orch"


def test_registry_stores_each_body_once_and_never_a_truncated_one(tmp_path):
    good = "---\nname: ok\n---\nbody"
    spans = (tool_call("load_skill", "a", args={"skill_name": "ok"},
                       result=good, ts="2026-09-03T17:00:00.000Z")
             + tool_call("load_skill", "a", args={"skill_name": "cut"},
                         result="y" * t.TRUNC_BOUNDARY,
                         ts="2026-09-03T17:00:01.000Z")
             + [invoke("a")])
    _, _, bodies = t.convert(spans)
    added, total = t.write_skill_registry(bodies, str(tmp_path))
    assert (added, total) == (1, 1)
    # idempotent: a second pass adds nothing
    assert t.write_skill_registry(bodies, str(tmp_path)) == (0, 1)
    index = json.loads((tmp_path / "index.json").read_text())
    assert [v["skill_name"] for v in index.values()] == ["ok"]


# --- AppGenAIContent migration ---------------------------------------------

def test_content_columns_override_the_property_bag():
    """From 2026-09-30 the span tables carry a pointer, not the payload, so
    the AppGenAIContent column is the only real source."""
    row = {"customDimensions": json.dumps({t.K_TOOL_RES: "pointer:abc"}),
           "c_tool_result": '{"count": 3}'}
    merged = t.merge_content_columns(row, t._as_dict(row["customDimensions"]))
    assert merged[t.K_TOOL_RES] == '{"count": 3}'


def test_content_columns_undo_the_8192_truncation():
    """ToolCallResult is a real column, not a property-bag entry, so the
    App Insights property cap never applied to it."""
    truncated = "x" * t.TRUNC_BOUNDARY
    full = "x" * 20000
    row = {"customDimensions": json.dumps({t.K_TOOL_RES: truncated}),
           "c_tool_result": full}
    merged = t.merge_content_columns(row, t._as_dict(row["customDimensions"]))
    assert len(merged[t.K_TOOL_RES]) == 20000


def test_an_empty_content_column_leaves_the_property_bag_alone():
    """During the dual-write window a span may have the value inline and no
    joined row; the leftouter join then yields empty columns."""
    row = {"c_tool_result": "", "c_input": None}
    merged = t.merge_content_columns(row, {t.K_TOOL_RES: "inline value"})
    assert merged[t.K_TOOL_RES] == "inline value"


def test_every_sensitive_attribute_has_a_column_mapping():
    """The seven attributes App Insights moves to AppGenAIContent. Six are
    consumed here; gen_ai.evaluation.explanation is not read by any check."""
    assert set(t.CONTENT_COLUMNS.values()) == {
        t.K_IN_MSGS, t.K_OUT_MSGS, t.K_SYS,
        t.K_TOOL_DEFS, t.K_TOOL_ARGS, t.K_TOOL_RES,
    }


def test_a_full_row_with_content_columns_converts_normally():
    rows = [{
        "timestamp [UTC]": "9/3/2026, 5:29:42.893 PM",
        "name": "execute_tool cw_query",
        "id": "s1", "operation_Id": "op1", "operation_ParentId": "",
        "duration": "10", "success": "True",
        "customDimensions": json.dumps({
            "gen_ai.agent.name": "triage-analysis-agent",
            "gen_ai.tool.name": "cw_query",
            "_MS.GenAIContentId": "abc",
        }),
        "c_tool_args": '{"entity": "service/tickets"}',
        "c_tool_result": '{"count": 0}',
    }]
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(rows, fh)
    try:
        spans = t.load_spans(path)
        runs, _, _ = t.convert(spans)
    finally:
        os.unlink(path)
    assert runs[0]["tool_names"] == ["cw_query"]
    assert runs[0]["empty_results"], "dead end should be detected from the column"


# --- intent must name a real intent -----------------------------------------

def _dims(text):
    return {t.K_IN_MSGS: json.dumps([{"role": "user", "content": text}])}


def test_a_doc_line_cannot_repoint_the_trajectory_key():
    """Skill files are pasted into the input messages.

    _INTENT_RE takes any `intent:`/`intent=` value, so the first match could
    be documentation rather than the hand-off. An arbitrary value used to be
    accepted, producing a traj_key no expectation uses -- the trajectory
    check then skipped, silently, on a run that had a perfectly good intent
    further down the same blob.
    """
    blob = ("# Orchestration skill\n"
            "Set intent: <the classified intent> before delegating.\n"
            "---\n"
            "intent=Full Triage; ticketId=805392")
    intent, source = t.extract_intent(_dims(blob), [])
    assert intent == "Full Triage", (intent, source)
    assert source == "declared"


def test_an_unknown_intent_is_not_an_intent():
    """None falls back to the bare agent name, which is the documented
    behaviour for an intent we cannot resolve. Returning the raw string
    instead produced a key nothing matches."""
    intent, source = t.extract_intent(_dims("intent=Sandwich Making"), [])
    assert intent is None and source == "unknown", (intent, source)


def test_a_real_intent_still_resolves():
    intent, source = t.extract_intent(
        _dims("intent=Write Request; ticketId=1"), [])
    assert intent == "Write Request" and source == "declared"


def test_inbound_and_delegated_are_constrained_too():
    assert t.extract_intent({}, [], inbound="Nonsense") == (None, "unknown")
    assert t.extract_intent({}, [], inbound="write request") == \
        ("Write Request", "inbound")
    steps = [{"is_a2a": True, "tool": "x", "arguments": "intent=Nonsense"}]
    assert t.extract_intent({}, steps) == (None, "unknown")
    steps = [{"is_a2a": True, "tool": "x", "arguments": "intent=Full Triage"}]
    assert t.extract_intent({}, steps) == ("Full Triage", "delegated")


def test_an_agent_only_the_trace_knows_about_is_still_a_boundary():
    """The silent failure this replaces.

    An agent missing from the configured set had its A2A hand-off scored as
    an ordinary tool call, so its run collapsed into its caller's and the
    trajectory it was supposed to cover simply vanished. Nothing said so.

    The trace names every agent that ran, so it is the source of truth and
    the config is only a supplement.
    """
    spans = tool_call("brand-new-agent", "triage-orchestrator")
    spans += [span("invoke_agent", "brand-new-agent",
                   gen_ai__operation__name="invoke_agent")]

    assert "brand-new-agent" not in t.AGENT_NAMES, \
        "pick a name that is not configured, or the test proves nothing"

    agents = t.learn_agents(spans)
    assert "brand-new-agent" in agents

    step = t.tool_step(spans[0], agents)
    assert step["is_a2a"] is True

    # Without it, the old behaviour -- scored as a tool call.
    assert t.tool_step(spans[0])["is_a2a"] is False


def test_learning_agents_does_not_mutate_the_configured_set():
    """A global rewritten by conversion makes every later read depend on
    which trace was converted first. scrub_trace reads this set to decide
    what must never be redacted; it should not matter what ran before it."""
    before = set(t.AGENT_NAMES)
    t.learn_agents(tool_call("some-other-agent", "triage-orchestrator"))
    assert set(t.AGENT_NAMES) == before
