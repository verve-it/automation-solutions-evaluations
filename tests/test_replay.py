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
from conftest import span, tool_call, REPO


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
    payload = rr.definition_payload(FakeVersion())
    out = rr.binding_for(payload).rebind(
        payload, server_url="https://r.example/mcp", models=models,
        token=None, session=None)
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
    # Never a fabricated input: replaying a question the recording did not
    # ask produces a divergence the gate blames on the agent.
    message = str(exc.value)
    assert "no query" in message
    # The rebuild command for this platform: make, or tasks.ps1 on Windows.
    assert rr._runner() in message


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
    assert json.loads(out.read_text(encoding="utf-8"))["base_version"] == "82"


def test_the_manifest_carries_the_window_the_gate_exports_by(tmp_path):
    """Without it the gate falls back to exporting an hour of the project.

    The project also serves real traffic. An hour-wide export scores someone
    else's run as part of this agent change's verdict, and the gate goes red
    -- or green -- for something the change did not do.
    """
    import argparse
    out = tmp_path / "run.json"
    args = argparse.Namespace(agent="a", cassette="c.json",
                              server_url="https://r.example.net/mcp",
                              manifest=str(out))
    run = {"started_utc": "2026-09-22T10:00:00+00:00",
           "finished_utc": "2026-09-22T10:04:00+00:00",
           "agent_session_id": "sess_1", "response_id": "resp_1"}
    payload = rr.write_manifest(str(out), args, "82", "87", {}, run)

    assert payload["started_utc"] == "2026-09-22T10:00:00+00:00"
    assert payload["finished_utc"] == "2026-09-22T10:04:00+00:00"
    assert payload["agent_session_id"] == "sess_1"
    assert payload["response_id"] == "resp_1"
    # and the run never overwrites the provenance
    assert payload["base_version"] == "82"


# --- staying migratable -----------------------------------------------------

def test_a_cassette_declares_a_schema_identifier():
    """Microsoft ships no MCP record/replay format. Four independent projects
    do -- mcp-replay, mcpcassette, mcp-cassette, Agent VCR -- and all of them
    put a schema id in a meta header. A converter needs something to branch
    on, and a file that does not say what it is cannot be recognised later."""
    spans = mc.load_spans(os.path.join(REPO, "traces",
                                       "2026-09-03-full-triage.json"))
    cassettes = mc.build(spans)
    assert cassettes
    for c in cassettes:
        assert c["schema"] == "verve/mcp-cassette@1"
        assert c["cassette_version"] == 1


def test_the_recorded_mcp_protocol_version_is_captured():
    """The OTel MCP conventions are at Development stability with no
    versioned release to pin against, so the protocol version the recording
    saw is the only fixed point a future reader has."""
    spans = mc.load_spans(os.path.join(REPO, "traces",
                                       "2026-09-03-full-triage.json"))
    for c in mc.build(spans):
        assert c["mcp_protocol_version"] == "2025-11-25"


def test_the_protocol_version_is_read_from_initialize_not_tool_spans():
    """It is carried on `initialize`, which the cassette is not built from.
    Looking only at tool spans silently yields None."""
    spans = mc.load_spans(os.path.join(REPO, "traces",
                                       "2026-09-03-full-triage.json"))
    carriers = {s["name"].split()[0] for s in spans
                if s["d"].get("mcp.protocol.version")}
    assert carriers == {"initialize"}
    assert not any(mc.is_tool_span(s) for s in spans
                   if s["d"].get("mcp.protocol.version"))


def test_an_app_insights_mangled_protocol_version_is_trimmed():
    """MCP protocol versions are plain dates. App Insights stores
    2025-11-25 as 2025-11-25T00:00:00.0000000Z."""
    spans = [{"op_id": "op1", "d": {"mcp.protocol.version":
                                    "2025-11-25T00:00:00.0000000Z"}}]
    assert mc.mcp_protocol_version(spans) == {"op1": "2025-11-25"}


def test_a_plain_version_is_left_alone():
    spans = [{"op_id": "op1", "d": {"mcp.protocol.version": "2025-11-25"}},
             {"op_id": "op2", "d": {"mcp.protocol.version": "draft"}}]
    got = mc.mcp_protocol_version(spans)
    assert got == {"op1": "2025-11-25", "op2": "draft"}


def test_cassette_records_the_agent_and_the_query():
    """A replay should need the cassette and a URL, nothing else.

    The agent name and the input are both part of the recording. Asking a
    caller to retype the query invites replaying a slightly different question
    than the one recorded, which is a divergence the gate would blame on the
    agent.
    """
    spans = [
        span("invoke_agent", "triage-orchestrator", ts="2026-09-03T17:00:00.000Z",
             gen_ai__operation__name="invoke_agent",
             gen_ai__input__messages=json.dumps([
                 {"role": "system", "parts": [{"type": "text",
                                               "content": "you are an agent"}]},
                 {"role": "user", "parts": [{"type": "text",
                                             "content": "triage 805392"}]}])),
        span("invoke_agent", "triage-analysis-agent", ts="2026-09-03T17:00:05.000Z",
             gen_ai__operation__name="invoke_agent",
             gen_ai__input__messages=json.dumps([
                 {"role": "user", "parts": [{"type": "text",
                                             "content": "hand-off, not the ask"}]}])),
    ]
    spans += tool_call("cw_get_ticket", "triage-orchestrator",
                       args={"ticket_number": 805392}, result="{}",
                       ts="2026-09-03T17:00:10.000Z")

    built = mc.build(spans)
    assert len(built) == 1
    assert built[0]["query"] == "triage 805392"
    assert built[0]["agents"][0] == "triage-orchestrator"


def test_a_stale_cassette_says_to_rebuild(tmp_path):
    """Built before make_cassette recorded the input: no `query` key at all.

    The fix is `make cassettes`, not --query, and the difference matters --
    typing the query by hand replays a question the recording did not ask.
    """
    import run_replay
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"orchestration_id": "op", "agents": ["a"],
                                "interactions": [1, 2], "recorded": "x"}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        run_replay.cassette_query(str(path))
    assert run_replay._runner() in str(exc.value)


def test_a_rebuilt_cassette_with_no_input_says_something_else(tmp_path):
    """`query` present and null: the trace itself carried no user message."""
    import run_replay
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"orchestration_id": "op", "agents": ["a"],
                                "interactions": [1, 2], "recorded": "x",
                                "query": None}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        run_replay.cassette_query(str(path))
    message = str(exc.value)
    assert run_replay._runner() not in message
    assert "--query" in message


def test_the_entry_agent_comes_from_the_cassette(tmp_path):
    import run_replay
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "orchestration_id": "op", "recorded": "x", "interactions": [],
        "agents": ["triage-orchestrator", "connectwise-operations-agent"]}), encoding="utf-8")
    entry, everyone = run_replay.cassette_agent(str(path))
    assert entry == "triage-orchestrator"
    assert everyone[1] == "connectwise-operations-agent"

    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps({"orchestration_id": "op", "agents": [],
                                "interactions": [], "recorded": "x"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        run_replay.cassette_agent(str(bare))


def test_exporter_double_encoding_is_undone():
    """Within one export, one orchestration's input arrived double-encoded.

    The two orchestrations in 2026-09-03-full-triage.json are the same flow.
    One carried a real newline (0x0a), the other the characters `\\`, `\\`,
    `n`. Replaying the second verbatim asks a different question than
    production was asked, and the gate scores the difference as the agent's.
    """
    double = "entityType=ticket" + chr(92) * 2 + "nentityId=805392"
    repaired, rounds = mc.repair_escaping(double)
    assert rounds == 1
    assert repaired == "entityType=ticket\nentityId=805392"

    single = "entityType=ticket" + chr(92) + "nentityId=805392"
    assert mc.repair_escaping(single)[0] == "entityType=ticket\nentityId=805392"


def test_text_that_is_not_an_encoding_artefact_is_left_alone():
    """Narrow on purpose: rewriting real content would change the question."""
    already = "entityType=ticket\nentityId=805392"
    assert mc.repair_escaping(already) == (already, 0)

    # A backslash-n mentioned in prose, alongside real line breaks.
    prose = "use " + chr(92) + "n to break lines\nsecond line"
    assert mc.repair_escaping(prose) == (prose, 0)


def test_an_unescaped_query_does_not_make_a_cassette_lossy():
    """`lossy` means a truncated RESULT -- a fixture the agent reasons over.

    build.py refuses to package a lossy cassette, so conflating the two would
    have blocked the deployment over a whitespace repair.
    """
    spans = [
        span("invoke_agent", "a", ts="2026-09-03T17:00:00.000Z",
             gen_ai__operation__name="invoke_agent",
             gen_ai__input__messages=json.dumps([
                 {"role": "user", "parts": [{"type": "text",
                                             "content": "one" + chr(92) * 2
                                                        + "ntwo"}]}])),
    ]
    spans += tool_call("cw_get_ticket", "a", args={"n": 1}, result="{}",
                       ts="2026-09-03T17:00:10.000Z")
    built = mc.build(spans)[0]
    assert built["query"] == "one\ntwo"
    assert built["query_unescaped_rounds"] == 1
    assert built["lossy"] is False
    assert built["warnings"] == []


def test_every_tracked_python_file_compiles_without_warnings():
    """The class of bug my own test runs could not see.

    An invalid escape sequence is a SyntaxWarning on Python 3.12 and a hidden
    DeprecationWarning on 3.11, so a docstring containing a stray backslash
    printed a warning on the machine running the gate and nothing on the one
    that wrote it. pytest did not catch it either: it imports through cached
    bytecode, which is not recompiled.

    compile() with warnings as errors surfaces it on every version, which is
    what this does. It is a whole-repo check because the next one will be in a
    different file.
    """
    import subprocess
    import warnings

    listed = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO,
                            capture_output=True, text=True)
    files = [f for f in listed.stdout.split() if f]
    assert files, "git ls-files found no python files"

    offenders = []
    for name in files:
        with open(os.path.join(REPO, name), encoding="utf-8") as fh:
            source = fh.read()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            try:
                compile(source, name, "exec")
            except SyntaxWarning as exc:
                offenders.append(f"{name}: {exc}")
            except SyntaxError as exc:
                # 3.11 escalates the warning into a SyntaxError under
                # simplefilter("error"); a real syntax error would have failed
                # the import long before this test ran.
                offenders.append(f"{name}:{exc.lineno}: {exc.msg}")

    assert not offenders, "\n".join(offenders)


# ---------------------------------------------- the SDK shapes, against the SDK

def test_the_definition_comes_from_a_version_not_the_agent():
    """`agents.get(name)` returns the agent, which has no definition.

    This is what failed on the first real run:
        AttributeError: 'AgentDetails' object has no attribute 'definition'
    The definition hangs off AgentVersionDetails, reached through
    `versions.latest` or `get_version`.
    """
    class Definition:
        def as_dict(self):
            return {"model": "gpt-5", "tools": [{"type": "mcp"}]}

    class VersionDetails:
        version = "7"
        definition = Definition()

    class Versions:
        latest = VersionDetails()

    class AgentDetails:            # deliberately has no `definition`
        name = "triage-orchestrator"
        versions = Versions()

    class Agents:
        def __init__(self):
            self.asked = None

        def get(self, name):
            return AgentDetails()

        def get_version(self, name, version):
            self.asked = (name, version)
            return VersionDetails()

    agents = Agents()
    latest = rr.agent_version_details(agents, "triage-orchestrator")
    assert latest.definition.as_dict()["model"] == "gpt-5"

    pinned = rr.agent_version_details(agents, "triage-orchestrator", "3")
    assert agents.asked == ("triage-orchestrator", "3")
    assert pinned.version == "7"


def test_an_agent_with_no_versions_says_so():
    class Agents:
        def get(self, name):
            return type("AgentDetails", (), {"versions": None})()

    with pytest.raises(RuntimeError) as exc:
        rr.agent_version_details(Agents(), "nameless")
    assert "no versions" in str(exc.value)


def test_the_session_indicator_is_the_concrete_one():
    """VersionIndicator is the abstract base and takes no version at all.

    run_replay built `VersionIndicator(version=...)`, which the SDK rejects
    outright -- the concrete class is VersionRefIndicator and its field is
    `agent_version`. Asserted against the installed SDK rather than a fake, so
    a shape change in the SDK fails here rather than on someone's first run.
    """
    models = pytest.importorskip("azure.ai.projects.models")

    indicator = models.VersionRefIndicator(agent_version="7")
    assert indicator.as_dict() == {"agent_version": "7", "type": "version_ref"}

    with pytest.raises(TypeError):
        models.VersionIndicator(version="7")

    source = _read_source(rr)
    assert "VersionRefIndicator" in source
    assert "VersionIndicator(version=" not in source


def test_create_version_takes_definition_as_a_keyword():
    """Checked against the real signature, because two siblings did not."""
    operations = pytest.importorskip("azure.ai.projects.operations")
    import inspect

    signature = inspect.signature(operations.AgentsOperations.create_version)
    assert "definition" in signature.parameters
    assert signature.parameters["definition"].kind is inspect.Parameter.KEYWORD_ONLY

    session_signature = inspect.signature(
        operations.AgentsOperations.create_session)
    assert "version_indicator" in session_signature.parameters


def _read_source(module):
    import inspect
    return inspect.getsource(module)


def test_a_prompt_agent_has_its_tools_swapped_and_nothing_else():
    """Everything but the binding is carried over verbatim.

    A replayed agent already differs from production by its tools; letting
    the model or instructions drift as well makes the comparison meaningless.
    """
    models = pytest.importorskip("azure.ai.projects.models")

    payload = {"kind": "prompt", "model": "gpt-5", "instructions": "triage",
               "temperature": 0.2,
               "tools": [{"type": "mcp", "server_label": "cw"}]}
    binding = rr.binding_for(payload)
    assert isinstance(binding, rr.PromptBinding)

    clone = binding.rebind(payload, server_url="https://x/mcp/y",
                           models=models, token="t", session="s")
    assert clone["model"] == "gpt-5"
    assert clone["instructions"] == "triage"
    assert clone["temperature"] == 0.2
    assert len(clone["tools"]) == 1
    assert clone["tools"][0]["server_url"] == "https://x/mcp/y"
    assert clone["tools"][0]["headers"]["Mcp-Session-Id"] == "s"
    # The original is not mutated -- the base version must stay readable.
    assert payload["tools"][0] == {"type": "mcp", "server_label": "cw"}


class _FakeToolboxes:
    def __init__(self):
        self.created = None
        self.deleted = None

    def create_version(self, name, *, tools, description, metadata):
        self.created = (name, tools, metadata)
        return type("TV", (), {"name": name, "version": "1"})()

    def delete(self, name):
        self.deleted = name


class _FakeAgents:
    def __init__(self):
        self.uploaded = None

    def download_code(self, name, *, agent_version=None):
        return iter([b"PK\x03\x04", b"-code-"])

    def create_version_from_code(self, agent_name, *, definition, code,
                                 description, metadata):
        self.uploaded = (agent_name, definition, code.read())
        return type("V", (), {"version": "44"})()


def test_a_hosted_agent_can_also_be_given_a_url_variable():
    """For a hosted agent that holds an endpoint rather than a toolbox name.

    None in this project do -- --inspect-code showed them all naming a
    toolbox -- but --replay-env-var covers the other shape, and the rest of
    the definition still has to survive untouched.
    """
    models = pytest.importorskip("azure.ai.projects.models")
    toolboxes = _FakeToolboxes()
    client = type("C", (), {"toolboxes": toolboxes})()

    payload = {"kind": "hosted", "cpu": "1", "memory": "2Gi",
               "environment_variables": {"AZURE_AI_MODEL_DEPLOYMENT_NAME": "x"},
               "code_configuration": {"runtime": "python_3_13"}}
    binding = rr.binding_for(payload, ["CONNECTWISE_MCP_URL"])
    assert isinstance(binding, rr.HostedBinding)
    binding.prepare(client, server_url="https://x/mcp/y", token=None,
                    session=None, models=models, label="replay-t")

    clone = binding.rebind(payload, server_url="https://x/mcp/y", token=None,
                           session=None, models=models)
    env = clone["environment_variables"]
    assert env["CONNECTWISE_MCP_URL"] == "https://x/mcp/y"
    assert env[rr.TOOLBOX_NAME_VAR] == "replay-t"
    # Carried over, not replaced.
    assert env["AZURE_AI_MODEL_DEPLOYMENT_NAME"] == "x"
    assert clone["cpu"] == "1" and clone["memory"] == "2Gi"
    assert payload["environment_variables"] == {
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "x"}


def test_a_hosted_version_is_created_from_the_same_code():
    """The clone must differ by the variables and nothing else."""
    class Agents:
        def __init__(self):
            self.downloaded = None
            self.uploaded = None

        def download_code(self, name, *, agent_version=None):
            self.downloaded = (name, agent_version)
            return iter([b"PK\x03\x04", b"-agent-code-"])

        def create_version_from_code(self, agent_name, *, definition, code,
                                     description, metadata):
            self.uploaded = (agent_name, definition, code.read(), metadata)
            return type("V", (), {"version": "99"})()

    agents = Agents()
    binding = rr.HostedBinding(["CONNECTWISE_MCP_URL"])
    created = binding.create(agents, "triage-orchestrator",
                             {"kind": "hosted"}, description="d",
                             metadata={"purpose": "p"}, base_version="65")

    assert agents.downloaded == ("triage-orchestrator", "65")
    assert agents.uploaded[2] == b"PK\x03\x04-agent-code-"
    assert created.version == "99"


def test_a_kind_with_nowhere_to_bind_is_refused_by_name():
    with pytest.raises(SystemExit) as exc:
        rr.binding_for({"kind": "workflow"})
    message = str(exc.value)
    assert "kind='workflow'" in message
    assert "--describe" in message


def test_describe_needs_no_cassette():
    """Surveying a project should not require knowing which cassette to use."""
    import inspect
    source = inspect.getsource(rr.main)
    assert "--cassette is required (or use --describe)" in source
    signature = inspect.getsource(rr.describe)
    assert "nothing was created or changed" in signature


# ------------------------------------------ the hosted path, end to end (fake)

def test_a_hosted_agent_binds_through_a_temporary_toolbox():
    """These agents hold no endpoint; they name a toolbox.

    --inspect-code showed every hosted agent reading
    CONNECTWISE_TOOLBOX_NAME/_VERSION/_AUTH_SCOPE with ai.azure.com as the
    only host in the code. So the replay creates a toolbox pointing at the
    replay server and names it -- no code change, and the agent cannot tell.
    """
    models = pytest.importorskip("azure.ai.projects.models")

    toolboxes = _FakeToolboxes()
    client = type("C", (), {"toolboxes": toolboxes})()
    binding = rr.HostedBinding()

    binding.prepare(client, server_url="https://x/mcp/c", token="t",
                    session="s", models=models, label="replay-abc123")

    name, tools, metadata = toolboxes.created
    assert name == "replay-abc123"
    tool = tools[0].as_dict()
    assert tool["server_url"] == "https://x/mcp/c"
    assert tool["headers"]["Authorization"] == "Bearer t"
    # The session travels on the TOOLBOX, because Foundry is what calls the
    # replay server -- not the agent.
    assert tool["headers"]["Mcp-Session-Id"] == "s"
    assert metadata["purpose"] == rr.TEMP_MARKER

    payload = {"kind": "hosted",
               "environment_variables": {"AZURE_AI_MODEL_DEPLOYMENT_NAME": "m"}}
    clone = binding.rebind(payload, server_url="https://x/mcp/c", token="t",
                           session="s", models=models)
    env = clone["environment_variables"]
    assert env[rr.TOOLBOX_NAME_VAR] == "replay-abc123"
    assert env[rr.TOOLBOX_VERSION_VAR] == "1"
    assert env["AZURE_AI_MODEL_DEPLOYMENT_NAME"] == "m"

    binding.teardown()
    assert toolboxes.deleted == "replay-abc123"


def test_rebinding_before_the_toolbox_exists_is_a_bug_not_a_silent_miss():
    binding = rr.HostedBinding()
    with pytest.raises(RuntimeError):
        binding.rebind({"kind": "hosted"}, server_url="u", token=None,
                       session=None)


def test_the_hosted_clone_ships_the_same_code_bytes():
    agents = _FakeAgents()
    binding = rr.HostedBinding()
    created = binding.create(agents, "connectwise-operations-agent",
                             {"kind": "hosted"}, description="d",
                             metadata={"purpose": "p"}, base_version="43")
    assert agents.uploaded[2] == b"PK\x03\x04-code-"
    assert created.version == "44"


def test_an_orchestration_is_refused_because_its_children_are_not_stubbed():
    """The guarantee this repo exists for, defended.

    The orchestrator reaches its children by NAME. A name resolves to that
    agent's own default version, whose environment still points at the real
    ConnectWise toolbox -- so a replay of an orchestration sends the
    children's calls, writes included, to the live service, and the journal
    never sees them.
    """
    with pytest.raises(SystemExit) as exc:
        rr.refuse_unstubbed_children(
            "c.json",
            ["triage-orchestrator", "triage-analysis-agent",
             "connectwise-operations-agent"])
    message = str(exc.value)
    assert "BY NAME" in message
    assert "live service" in message
    assert "no override" in message


def test_a_single_agent_cassette_is_not_refused():
    rr.refuse_unstubbed_children("c.json", ["connectwise-operations-agent"])


def test_there_is_no_way_to_replay_an_orchestration_anyway():
    """Nothing in this repo may reach ConnectWise, so the refusal takes no
    override.

    It used to: --allow-live-children printed a warning and went ahead, which
    made the one safeguard between a multi-agent cassette and a live write a
    flag someone could set in a hurry. An eval that writes to a system of
    record is not an eval.
    """
    import inspect
    assert "allow" not in inspect.signature(
        rr.refuse_unstubbed_children).parameters
    source = inspect.getsource(rr)
    assert "--allow-live-children" not in source.split('"""', 3)[-1]
    with pytest.raises(SystemExit):
        rr.refuse_unstubbed_children("c.json", ["orchestrator", "child"])


# ------------------------------------------------- project facts, not constants

def test_a_write_tool_is_decided_by_the_server_then_the_config():
    """The hard-coded set was wrong in both directions.

    It named cw_patch, which does not exist in the manifest, and missed eight
    tools that plainly mutate -- cw_log_time, cw_set_approval and friends. A
    missed write tool is counted as a read, so the gate reports "0 writes,
    none performed" about a run that attempted several. Nothing catches that.
    """
    import evalconfig

    config = {"write_tools": {"names": ["cw_update"],
                              "prefixes": ["cw_set", "cw_log"]}}

    assert evalconfig.is_write_tool("cw_update", config) is True
    assert evalconfig.is_write_tool("cw_set_approval", config) is True
    assert evalconfig.is_write_tool("cw_log_time", config) is True
    assert evalconfig.is_write_tool("cw_query", config) is False
    # Foundry prefixes the server: the bare name is what is matched.
    assert evalconfig.is_write_tool("SomeServer___cw_set_approval",
                                    config) is True

    # What the server says about itself wins over the config either way.
    read_only = [{"tools": [{"name": "cw_update",
                             "annotations": {"readOnlyHint": True}}]}]
    assert evalconfig.is_write_tool("cw_update", config, read_only) is False
    mutating = [{"tools": [{"name": "cw_query",
                            "annotations": {"readOnlyHint": False}}]}]
    assert evalconfig.is_write_tool("cw_query", config, mutating) is True


def test_this_project_is_configured_not_compiled_in():
    """Any agent, any tool server -- so nothing about ConnectWise is a
    constant in a script."""
    import evalconfig

    config = evalconfig.load()
    assert evalconfig.toolbox_env(config) == ("CONNECTWISE_TOOLBOX_NAME",
                                              "CONNECTWISE_TOOLBOX_VERSION")
    assert "cw_log_time" not in json.dumps(evalconfig.DEFAULTS)
    assert evalconfig.DEFAULTS["agents"] == []
    assert evalconfig.toolbox_env(evalconfig.DEFAULTS) == ("", "")

    # The defaults must not quietly fit this project, or the next one inherits
    # settings nobody chose.
    assert evalconfig.is_write_tool("cw_update", evalconfig.DEFAULTS) is False


def test_a_missing_config_file_still_runs(tmp_path):
    import evalconfig
    config = evalconfig.load(str(tmp_path / "absent.json"))
    assert config == evalconfig.DEFAULTS


def test_a_partial_config_gets_defaults_for_the_rest(tmp_path):
    """A KeyError three calls later is a worse failure than a default."""
    import evalconfig
    path = tmp_path / "eval-config.json"
    path.write_text(json.dumps({"write_tools": {"names": ["x_write"]}}), encoding="utf-8")
    config = evalconfig.load(str(path))
    assert config["write_tools"]["names"] == ["x_write"]
    assert config["write_tools"]["prefixes"] == []
    assert config["agents"] == []


def test_a_hosted_agent_with_no_configured_toolbox_vars_says_so():
    """Better than creating a toolbox nothing will ever look at."""
    models = pytest.importorskip("azure.ai.projects.models")
    binding = rr.HostedBinding(name_var="", version_var="")
    binding.name_var = binding.version_var = ""
    client = type("C", (), {"toolboxes": _FakeToolboxes()})()
    with pytest.raises(SystemExit) as exc:
        binding.prepare(client, server_url="u", token=None, session=None,
                        models=models, label="replay-x")
    assert "eval-config.json" in str(exc.value)


# --- what the stub advertises ----------------------------------------------
# The contract is the production server's tools/list, nothing added, nothing
# dropped. Recordings hold more than that (the agent's own tools) and less
# (only what one run happened to call).

CW_MANIFEST = [{"toolbox": "ConnectwiseMCP", "versions": ["*"], "tools": [
    {"name": "cw_get_ticket", "description": "g",
     "parameters": {"type": "object", "properties": {"ticket_number": {}}}},
    {"name": "cw_log_time", "description": "l",
     "parameters": {"type": "object", "properties": {"hours": {}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
]}]


def test_the_agents_own_tools_are_not_advertised():
    """They were: `load_skill` went out as `<label>___load_skill`, with an
    empty schema, beside the agent's real one."""
    cas = rs.Cassette(_cassette([
        _interaction(0, "load_skill", {"skill_name": "triage"}, "---"),
        _interaction(1, "tool_search", {"q": "ticket"}, "[]"),
        _interaction(2, "ConnectWise-PSA-ForAgents___cw_get_ticket",
                     {"ticket_number": 1}, "{}"),
    ]))
    tools, missing = rs.tool_definitions(
        cas, CW_MANIFEST, local=["load_skill", "tool_search"])
    names = [t["name"] for t in tools]
    assert "load_skill" not in names and "tool_search" not in names
    assert missing == []


def test_a_prefixed_call_is_the_servers_even_if_its_name_is_local():
    """A server that really has a tool of that name is the recording's
    evidence; the configured list only decides for bare names."""
    cas = rs.Cassette(_cassette([
        _interaction(0, "ConnectWise-PSA-ForAgents___tool_search", {}, "[]")]))
    tools, _ = rs.tool_definitions(cas, [], local=["tool_search"])
    assert [t["name"] for t in tools] == ["tool_search"]


def test_an_unknown_bare_name_still_counts_as_the_servers():
    """Same rule as attribution: a bare name that is not a configured local
    tool is a call that had to reach the stub."""
    cas = rs.Cassette(_cassette([_interaction(0, "cw_resolve", {}, "x")]))
    tools, missing = rs.tool_definitions(cas, [], local=["load_skill"])
    assert [t["name"] for t in tools] == ["cw_resolve"] and missing == ["cw_resolve"]


def test_the_whole_production_contract_is_advertised():
    """An agent change that reaches for a tool the recording never called
    must meet `not_recorded` -- a divergence the gate reports -- not a tool
    production would have offered and the stub hid."""
    cas = rs.Cassette(_cassette([
        _interaction(0, "ConnectWise-PSA-ForAgents___cw_get_ticket",
                     {"ticket_number": 1}, "{}")]))
    tools, _ = rs.tool_definitions(cas, CW_MANIFEST, local=[])
    assert [t["name"] for t in tools] == ["cw_get_ticket", "cw_log_time"]
    assert tools[1]["annotations"]["readOnlyHint"] is False


def test_another_servers_manifest_is_not_offered():
    cas = rs.Cassette(_cassette([
        _interaction(0, "Confluence___search", {"q": "x"}, "[]")]))
    tools, _ = rs.tool_definitions(cas, CW_MANIFEST, local=[])
    assert [t["name"] for t in tools] == ["search"]


def test_the_committed_recordings_advertise_only_the_server(tmp_path):
    """Built from the frozen sets: every cassette advertises the manifest's
    tools and none of the configured local ones."""
    import evalconfig, make_cassette
    import trace_to_eval as tte
    with open(os.path.join(REPO, "tool_manifests", "connectwisemcp.json"),
              encoding="utf-8") as fh:
        manifest = json.load(fh)
    contract = {t["name"] for t in manifest["tools"]}
    local = set(evalconfig.local_tools())
    checked = 0
    for trace in ("traces/2026-09-03-full-triage.json",
                  "traces/2026-09-15-ops-worst-case.json",
                  "traces/2026-09-23-triage-analysis.json"):
        for cas in make_cassette.build(tte.load_spans(os.path.join(REPO, trace))):
            if len(cas.get("agents") or []) != 1:
                continue
            tools, missing = rs.tool_definitions(rs.Cassette(cas), [manifest])
            names = {t["name"] for t in tools}
            assert not names & local, (trace, sorted(names & local))
            assert names == contract and not missing, (trace, missing)
            checked += 1
    assert checked == 7, checked      # the single-agent cassettes
