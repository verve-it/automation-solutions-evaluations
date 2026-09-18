"""The exporter builds KQL and picks a window. Neither needs Azure, and both
are where a mistake silently produces an empty or wrong dataset."""
import argparse
import json
from datetime import datetime, timedelta, timezone

import pytest

import export_traces as x


def args(**kw):
    base = dict(since=None, until=None, hours=None)
    base.update(kw)
    return argparse.Namespace(**base)


# --- KQL --------------------------------------------------------------------

def test_spans_query_keeps_custom_dimensions_intact():
    """A flattening projection strips every gen_ai.* attribute."""
    q = x.spans_query("dependencies", ["abc"])
    assert "customDimensions" in q
    assert "bag_unpack" not in q and "extend gen_ai" not in q


def test_workspace_table_is_renamed_to_canonical_columns():
    q = x.spans_query("AppDependencies", ["abc"])
    assert "customDimensions = Properties" in q
    assert "operation_Id = OperationId" in q


def test_resource_table_needs_no_rename():
    q = x.spans_query("dependencies", ["abc"])
    assert "Properties" not in q


def test_operation_ids_are_json_quoted_not_interpolated():
    q = x.spans_query("dependencies", ['a"b'])
    assert '"a\\"b"' in q


def test_candidate_query_filters_thin_traces_and_ranks():
    q = x.candidates_query("dependencies", ["triage-orchestrator"], 3, 10)
    assert "where tool_calls >= 3" in q
    assert "limit 10" in q
    assert '"triage-orchestrator"' in q
    assert "order by usable asc" in q


def test_candidate_query_tracks_tool_definitions_availability():
    """The signal that says the manifest gap has closed at the source."""
    q = x.candidates_query("dependencies", ["a"], 1, 5)
    assert "gen_ai.tool.definitions" in q


# --- window -----------------------------------------------------------------

def test_explicit_since_wins_over_state_and_hours():
    start, _ = x.resolve_window(
        args(since="2026-09-01T00:00:00Z", hours=1),
        {"last_timestamp": "2026-09-10T00:00:00Z"})
    assert start == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_hours_wins_over_state():
    start, end = x.resolve_window(args(hours=6),
                                  {"last_timestamp": "2020-01-01T00:00:00Z"})
    assert (end - start) == timedelta(hours=6)


def test_lag_mode_resumes_at_the_watermark():
    start, _ = x.resolve_window(args(),
                                {"last_timestamp": "2026-09-10T12:00:00Z"})
    assert start == datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


def test_no_state_and_no_flags_defaults_to_24h():
    start, end = x.resolve_window(args(), {})
    assert (end - start) == timedelta(hours=24)


def test_naive_timestamps_are_treated_as_utc():
    assert x._parse_time("2026-09-10T12:00:00").tzinfo == timezone.utc


# --- state ------------------------------------------------------------------

def test_watermark_advances_to_the_newest_span(tmp_path):
    state_file = tmp_path / "s.json"
    rows = [{"timestamp": "2026-09-10T01:00:00Z"},
            {"timestamp": "2026-09-10T03:00:00Z"}]
    window = (datetime(2026, 9, 10, tzinfo=timezone.utc),
              datetime(2026, 9, 11, tzinfo=timezone.utc))
    x.save_state(str(state_file), rows, window)
    assert json.loads(state_file.read_text())["last_timestamp"] == \
        "2026-09-10T03:00:00Z"


def test_empty_export_still_advances_the_watermark(tmp_path):
    """Otherwise an idle night makes the next run re-scan the same window for
    ever."""
    state_file = tmp_path / "s.json"
    end = datetime(2026, 9, 11, tzinfo=timezone.utc)
    x.save_state(str(state_file), [], (end - timedelta(days=1), end))
    assert json.loads(state_file.read_text())["last_timestamp"] == \
        end.isoformat()


def test_missing_state_file_is_not_an_error(tmp_path):
    assert x.load_state(str(tmp_path / "nope.json")) == {}


# --- row shaping ------------------------------------------------------------

def test_datetimes_are_serialised_for_the_converter():
    assert x._jsonable(datetime(2026, 9, 3, 17, tzinfo=timezone.utc)) == \
        "2026-09-03T17:00:00+00:00"


def test_dict_custom_dimensions_survive_unchanged():
    assert x._jsonable({"gen_ai.agent.name": "a"}) == {"gen_ai.agent.name": "a"}


# --- AppGenAIContent join ---------------------------------------------------

def test_spans_query_joins_the_content_table_by_default():
    q = x.spans_query("AppDependencies", ["abc"])
    assert "AppGenAIContent" in q
    assert "ToolCallResult" in q and "ToolDefinitions" in q
    assert "$left.id == $right.SpanId" in q


def test_content_join_can_be_disabled():
    q = x.spans_query("AppDependencies", ["abc"], content=False)
    assert "AppGenAIContent" not in q


def test_join_is_leftouter_so_spans_without_content_survive():
    """Most spans carry no gen_ai content at all; an inner join would drop
    them and lose the trajectory."""
    assert "kind=leftouter" in x.spans_query("dependencies", ["abc"])


def test_content_join_projects_every_column_the_converter_merges():
    import trace_to_eval
    q = x.spans_query("dependencies", ["abc"])
    for column in trace_to_eval.CONTENT_COLUMNS:
        assert column in q, f"{column} missing from the export projection"


# --- windowing --------------------------------------------------------------

def _timespans(monkeypatch, tmp_path, argv):
    """Run main() with Azure stubbed, returning the timespan of each query."""
    seen = []
    monkeypatch.setattr(x, "run_query",
                        lambda c, a, q, t: (seen.append(t), [])[1])
    monkeypatch.setattr(x, "_client", lambda: object())
    monkeypatch.setattr(x, "rows_from", lambda tables: [])
    monkeypatch.setattr("sys.argv",
                        ["export_traces.py", "--workspace", "ws",
                         "--out", str(tmp_path / "spans.json"),
                         "--state", str(tmp_path / "state.json")] + argv)
    assert x.main() == 0
    return seen


WINDOW = (datetime(2026, 9, 3, tzinfo=timezone.utc),
          datetime(2026, 9, 4, tzinfo=timezone.utc))


def test_the_span_fetch_widens_the_window(monkeypatch, tmp_path):
    """The discovery window must not bound the span fetch.

    Spans are selected by operation_Id, which names whole orchestrations. If
    the fetch is also clamped to the discovery window, an orchestration
    straddling the boundary exports partially: the root invoke_agent span
    drops, the run scores with empty query/response and unknown intent,
    check_trajectory skips, and CI goes red on LOST COVERAGE -- a windowing
    artifact read as an agent regression. Same mechanism loses spans
    ingested after the watermark.
    """
    monkeypatch.setattr(x, "resolve_window", lambda a, st: WINDOW)
    seen = _timespans(monkeypatch, tmp_path, ["--operation-ids", "abc123"])
    assert seen, "no query issued"
    fetch_start, fetch_end = seen[-1]
    assert fetch_start < WINDOW[0], (fetch_start, WINDOW[0])
    assert fetch_end > WINDOW[1], (fetch_end, WINDOW[1])


def test_pad_zero_restores_the_exact_window(monkeypatch, tmp_path):
    """The escape hatch has to actually do nothing."""
    monkeypatch.setattr(x, "resolve_window", lambda a, st: WINDOW)
    seen = _timespans(monkeypatch, tmp_path,
                      ["--operation-ids", "abc123",
                       "--span-window-pad-hours", "0"])
    assert seen[-1] == WINDOW


def test_discovery_still_uses_the_exact_window(monkeypatch, tmp_path):
    """Widening the fetch must not widen discovery — that would re-export
    orchestrations the watermark already covered."""
    monkeypatch.setattr(x, "resolve_window", lambda a, st: WINDOW)
    seen = _timespans(monkeypatch, tmp_path, [])   # no ids -> discovery runs
    assert seen[0] == WINDOW


def test_the_resource_id_guard_is_not_duplicated():
    """It was, verbatim — the second copy unreachable, so a change to the
    first would silently not apply."""
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "export_traces.py"),
        encoding="utf-8").read()
    assert src.count("cannot join AppGenAIContent") == 1
