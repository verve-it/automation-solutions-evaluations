"""Cassette building and replay.

The point of a cassette is that a re-run sees exactly the world the recorded
run saw. Every test here defends one of the ways that quietly stops being true.
"""
import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import make_cassette as mc
import replay_server as rs
from conftest import tool_call, REPO


# --- keying -----------------------------------------------------------------

def test_argument_order_and_whitespace_do_not_change_the_key():
    """An agent that serialises its arguments differently must not diverge on
    every call for no reason."""
    a = mc.interaction_key("cw_resolve", '{"query": "x", "reference_type": "company"}')
    b = mc.interaction_key("cw_resolve", '{"reference_type":"company","query":"x"}')
    assert a == b


def test_key_ignores_the_foundry_server_prefix():
    assert mc.interaction_key("ConnectWise-PSA-ForAgents___cw_resolve", "{}") == \
        mc.interaction_key("cw_resolve", "{}")


def test_different_arguments_are_different_keys():
    assert mc.interaction_key("cw_get_ticket", '{"ticket_number": 1}') != \
        mc.interaction_key("cw_get_ticket", '{"ticket_number": 2}')


def test_unparseable_arguments_still_key_deterministically():
    assert mc.canonical_args("not json ") == mc.canonical_args(" not json")


# --- ordering ---------------------------------------------------------------

def _cassette(interactions, lossy=False):
    return {"cassette_version": 1, "orchestration_id": "op1",
            "recorded": "2026-09-03T17:00:00.000Z", "agents": ["a"],
            "interactions": interactions, "writes": 0, "lossy": lossy,
            "warnings": []}


def _interaction(seq, tool, args, result, **kw):
    base = {"seq": seq, "agent": "a", "tool": tool,
            "key": mc.interaction_key(tool, json.dumps(args)),
            "arguments": args, "result": result, "success": True,
            "is_write": False, "truncated": False, "error_kind": None,
            "duration_ms": 1.0}
    base.update(kw)
    return base


def test_the_same_call_replays_its_results_in_recorded_order():
    """cw_get_ticket for one ticket returns five DIFFERENT results inside one
    recorded orchestration, because the agents mutate it as they go. Keyed by
    (tool, arguments) alone they collapse into one and the agent never sees
    its own writes land."""
    args = {"ticket_number": 805392}
    cas = rs.Cassette(_cassette([
        _interaction(0, "cw_get_ticket", args, "before"),
        _interaction(1, "cw_get_ticket", args, "after-write"),
    ]))
    assert cas.call("cw_get_ticket", args)[0]["result"] == "before"
    assert cas.call("cw_get_ticket", args)[0]["result"] == "after-write"


def test_exhausted_responses_repeat_the_last_by_default():
    args = {"ticket_number": 1}
    cas = rs.Cassette(_cassette([_interaction(0, "cw_get_ticket", args, "only")]))
    assert cas.call("cw_get_ticket", args)[1]["outcome"] == "matched"
    rec, entry = cas.call("cw_get_ticket", args)
    assert entry["outcome"] == "repeated" and rec["result"] == "only"


def test_exhausted_responses_can_be_made_to_diverge_instead():
    args = {"ticket_number": 1}
    cas = rs.Cassette(_cassette([_interaction(0, "cw_get_ticket", args, "only")]),
                      on_exhausted="diverge")
    cas.call("cw_get_ticket", args)
    assert cas.call("cw_get_ticket", args)[0] is None


# --- divergence -------------------------------------------------------------

def test_an_unrecorded_call_diverges_and_is_never_fabricated():
    """Returning a plausible answer would have the agent reason over a fiction
    and the result scored as real behaviour."""
    cas = rs.Cassette(_cassette([_interaction(0, "cw_get_ticket",
                                              {"ticket_number": 1}, "x")]))
    rec, entry = cas.call("cw_get_ticket", {"ticket_number": 999})
    assert rec is None
    assert entry["outcome"] == "diverged"


def test_matched_prefix_stops_at_the_first_divergence():
    args = {"ticket_number": 1}
    cas = rs.Cassette(_cassette([
        _interaction(0, "cw_get_ticket", args, "a"),
        _interaction(1, "cw_query", {"entity": "x"}, "b"),
    ]))
    cas.call("cw_get_ticket", args)
    cas.call("cw_describe", {"entity": "nope"})     # never recorded
    cas.call("cw_query", {"entity": "x"})
    s = cas.summary()
    assert s["matched"] == 2 and s["diverged"] == 1
    assert s["matched_prefix"] == 1
    assert s["first_divergence"]["tool"] == "cw_describe"


def test_a_failed_recorded_call_replays_as_failed():
    cas = rs.Cassette(_cassette([
        _interaction(0, "cw_resolve", {"reference_type": "site"}, "",
                     success=False, error_kind="empty_failed"),
    ]))
    rec, _ = cas.call("cw_resolve", {"reference_type": "site"})
    assert rec["success"] is False and rec["result"] == ""


# --- writes -----------------------------------------------------------------

def test_a_write_returns_its_recorded_response_and_is_counted():
    cas = rs.Cassette(_cassette([
        _interaction(0, "cw_update", {"id": 1}, '{"ok":true}', is_write=True),
    ]))
    rec, entry = cas.call("cw_update", {"id": 1})
    assert rec["result"] == '{"ok":true}'
    assert entry["is_write"] is True
    assert cas.summary()["writes_attempted"] == 1


def test_write_tools_are_recognised_when_building():
    spans = (tool_call("ConnectWise-PSA-ForAgents___cw_update", "a",
                       args={"id": 1}, result="ok")
             + tool_call("ConnectWise-PSA-ForAgents___cw_query", "a",
                         args={"entity": "x"}, result="ok",
                         ts="2026-09-03T17:00:01.000Z"))
    cas = mc.build(spans)[0]
    assert cas["writes"] == 1
    assert [i["is_write"] for i in cas["interactions"]] == [True, False]


# --- lossiness --------------------------------------------------------------

def test_a_truncated_result_makes_the_cassette_lossy():
    """Replaying a truncated result feeds the agent less than the original
    saw, and the difference gets scored as the agent's fault."""
    spans = tool_call("cw_query", "a", args={"entity": "x"},
                      result="y" * 8192)
    cas = mc.build(spans)[0]
    assert cas["lossy"] is True
    assert "truncated" in cas["warnings"][0]


def test_a_clean_cassette_is_not_lossy():
    spans = tool_call("cw_query", "a", args={"entity": "x"}, result="short")
    assert mc.build(spans)[0]["lossy"] is False


def test_a2a_handoffs_are_not_part_of_the_toolbox_cassette():
    """The callee is replayed as its own agent run; the toolbox must not
    answer for it."""
    spans = (tool_call("triage-analysis-agent", "triage-orchestrator",
                       args={"request": "x"}, result="y")
             + tool_call("cw_query", "triage-orchestrator",
                         args={"entity": "x"}, result="z",
                         ts="2026-09-03T17:00:01.000Z"))
    cas = mc.build(spans)[0]
    assert [i["tool"] for i in cas["interactions"]] == ["cw_query"]


# --- tool definitions -------------------------------------------------------

def test_tools_are_advertised_with_production_schemas_when_available():
    cas = rs.Cassette(_cassette([
        _interaction(0, "ConnectWise-PSA-ForAgents___cw_resolve", {}, "x"),
    ]))
    manifests = [{"toolbox": "ConnectwiseMCP", "versions": ["*"], "tools": [
        {"name": "cw_resolve", "description": "d",
         "parameters": {"type": "object", "properties": {"q": {}}}}]}]
    tools, missing = rs.tool_definitions(cas, manifests)
    assert tools[0]["name"] == "cw_resolve"
    assert tools[0]["inputSchema"]["properties"] == {"q": {}}
    assert missing == []


def test_missing_schemas_are_reported_not_silently_empty():
    """Without a manifest the agent is told it may send anything, so the
    replay is no longer a faithful stand-in."""
    cas = rs.Cassette(_cassette([_interaction(0, "cw_resolve", {}, "x")]))
    tools, missing = rs.tool_definitions(cas, [])
    assert missing == ["cw_resolve"]
    assert tools[0]["inputSchema"] == {"type": "object", "properties": {}}


# --- over the wire ----------------------------------------------------------

@pytest.fixture
def server():
    cas = rs.Cassette(_cassette([
        _interaction(0, "cw_get_ticket", {"ticket_number": 1}, "recorded body"),
        _interaction(1, "cw_update", {"id": 1}, '{"written":true}',
                     is_write=True),
    ]))
    rs.Handler.cassette = cas
    rs.Handler.tools, _ = rs.tool_definitions(cas, [])
    rs.Handler.token = None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), rs.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", cas
    httpd.shutdown()


def _rpc(url, method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(url + "/mcp", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def test_mcp_handshake_and_tools_list(server):
    url, _ = server
    assert _rpc(url, "initialize")["result"]["protocolVersion"] == \
        rs.PROTOCOL_VERSION
    names = [t["name"] for t in _rpc(url, "tools/list")["result"]["tools"]]
    assert names == ["cw_get_ticket", "cw_update"]


def test_recorded_call_returns_its_recorded_body(server):
    url, _ = server
    r = _rpc(url, "tools/call", {"name": "cw_get_ticket",
                                 "arguments": {"ticket_number": 1}})
    assert r["result"]["content"][0]["text"] == "recorded body"
    assert r["result"]["isError"] is False


def test_recorded_write_replays_without_writing(server):
    url, cas = server
    r = _rpc(url, "tools/call", {"name": "cw_update", "arguments": {"id": 1}})
    assert json.loads(r["result"]["content"][0]["text"]) == {"written": True}
    assert cas.summary()["writes_attempted"] == 1


def test_unrecorded_call_returns_a_typed_error_over_the_wire(server):
    url, _ = server
    r = _rpc(url, "tools/call", {"name": "cw_get_ticket",
                                 "arguments": {"ticket_number": 42}})
    assert r["result"]["isError"] is True
    assert json.loads(r["result"]["content"][0]["text"])["error"] == \
        "not_recorded"


def test_summary_endpoint_reports_the_replay(server):
    url, _ = server
    _rpc(url, "tools/call", {"name": "cw_get_ticket",
                             "arguments": {"ticket_number": 1}})
    s = json.loads(urllib.request.urlopen(url + "/summary").read())
    assert s["matched"] == 1 and s["matched_prefix"] == 1


def test_a_token_is_enforced_when_set(server):
    url, _ = server
    rs.Handler.token = "s3cret"
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _rpc(url, "tools/list")
        assert exc.value.code == 401
    finally:
        rs.Handler.token = None


def test_the_token_also_guards_the_summary(server):
    """do_GET used to skip the check entirely.

    /summary is the journal — tool names, canonicalised arguments carrying
    ticket and company identifiers, every attempted write — on a server
    docs/REPLAY.md tells you to expose to Foundry.
    """
    url, _ = server
    rs.Handler.token = "s3cret"
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url + "/summary")
        assert exc.value.code == 401

        req = urllib.request.Request(
            url + "/summary", headers={"Authorization": "Bearer s3cret"})
        assert "matched" in json.loads(urllib.request.urlopen(req).read())
    finally:
        rs.Handler.token = None


def test_the_health_check_stays_open(server):
    """A readiness probe carries no token and leaks nothing."""
    url, _ = server
    rs.Handler.token = "s3cret"
    try:
        body = json.loads(urllib.request.urlopen(url + "/").read())
        assert body == {"status": "ok", "mode": "replay"}
    finally:
        rs.Handler.token = None


# --- the driver -------------------------------------------------------------

import importlib.util as _ilu                                  # noqa: E402
_spec = _ilu.spec_from_file_location(
    "run_replay", os.path.join(REPO, "replay", "run_replay.py"))
rr = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(rr)


@pytest.mark.parametrize("url", [
    "http://localhost:8901/mcp",
    "http://127.0.0.1:8901",
    "https://0.0.0.0:9000/mcp",
    "http://mybox.local/mcp",
])
def test_a_url_azure_cannot_reach_is_rejected(url):
    """Foundry calls the replay server, not the other way round. A localhost
    URL becomes a tool call that times out inside the agent run, surfacing as
    an agent failure rather than as a configuration mistake."""
    assert rr.is_locally_scoped(url)


@pytest.mark.parametrize("url", [
    "https://replay.example.net/mcp",
    "https://abc123.ngrok-free.app/mcp",
    "https://replay.internal.corp:8443/mcp",
])
def test_a_reachable_url_is_accepted(url):
    assert not rr.is_locally_scoped(url)


def test_only_the_tools_are_swapped_when_cloning_an_agent():
    """A replayed agent already differs from production by its tool binding.
    Letting model, instructions or temperature drift too makes the comparison
    meaningless."""
    class FakeDef:
        def as_dict(self):
            return {"kind": "prompt", "model": "gpt-4o",
                    "instructions": "triage the ticket",
                    "temperature": 0.2,
                    "tools": [{"type": "mcp", "server_label": "connectwise"}]}

    class FakeVersion:
        definition = FakeDef()

    import azure.ai.projects.models as models
    out = rr.cloned_definition(FakeVersion(), "https://r.example/mcp", models)
    assert out["model"] == "gpt-4o"
    assert out["instructions"] == "triage the ticket"
    assert out["temperature"] == 0.2
    assert len(out["tools"]) == 1
    assert out["tools"][0]["server_url"] == "https://r.example/mcp"
    assert out["tools"][0]["server_label"] == rr.REPLAY_TOOL_LABEL


def test_a_cassette_without_a_query_says_so_rather_than_inventing_one():
    import json as _json
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        _json.dump({"interactions": [{"tool": "x"}]}, fh)
        path = fh.name
    with pytest.raises(SystemExit) as exc:
        rr.cassette_query(path)
    assert "no recorded query" in str(exc.value)


def test_divergence_alone_is_not_failure():
    """An agent change that removes a wasted call SHOULD diverge. That is the
    improvement, not a regression."""
    s = {"recorded_interactions": 50, "matched_prefix": 40,
         "writes_attempted": 2,
         "first_divergence": {"seq": 40, "tool": "a___cw_query"}}
    ok, text = rr.verdict(s)
    assert ok
    assert "matched prefix        : 40" in text


def test_diverging_before_the_floor_fails():
    s = {"recorded_interactions": 50, "matched_prefix": 3,
         "writes_attempted": 0,
         "first_divergence": {"seq": 3, "tool": "a___cw_resolve"}}
    ok, text = rr.verdict(s, allow_divergence_after=10)
    assert not ok
    assert "FAIL" in text


def test_the_report_states_that_no_write_was_performed():
    ok, text = rr.verdict({"recorded_interactions": 1, "matched_prefix": 1,
                           "writes_attempted": 4})
    assert "none performed" in text


def test_the_manifest_records_what_the_deleted_version_cannot(tmp_path):
    """Foundry evals are PROJECT-scoped -- evals.create() takes a dataset,
    not an agent id -- so deleting the temporary version loses no results.

    What it does lose is provenance. App Insights stamps gen_ai.agent.id and
    version on every span; delete the version and a trace names an agent that
    cannot be looked up, with nothing saying what it was cloned from.
    """
    import argparse
    out = tmp_path / "run.json"
    args = argparse.Namespace(agent="triage-orchestrator",
                              cassette="cassettes/2026-09-03-abc.json",
                              server_url="https://replay.example.net/mcp",
                              manifest=str(out))
    s = {"cassette": "4dda7f4fa5f0", "matched_prefix": 50,
         "recorded_interactions": 50, "writes_attempted": 4,
         "first_divergence": None}
    payload = rr.write_manifest(str(out), args, "82", "87", s)

    assert payload["base_version"] == "82"
    assert payload["temp_version"] == "87"
    assert payload["temp_version_deleted"] is True
    assert payload["cassette"] == "2026-09-03-abc.json"
    assert payload["writes_attempted"] == 4
    assert "no write performed" in payload["tools"]
    # the eval names the thing under test, not the fixture
    assert payload["suggested_eval_name"].endswith("v82")
    assert "87" not in payload["suggested_eval_name"]
    assert json.loads(out.read_text())["base_version"] == "82"
