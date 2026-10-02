"""Hosting the stub: the Azure Function custom handler and its state store.

The point of these is that the hosted server answers *identically* to
`replay_server.py`. If it drifts, the gate means one thing on a laptop and
another in CI, and nobody finds out until a release.
"""
import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import mcp_core
import replay_server as rs
from state_store import Conflict, MemoryStore, Unavailable
from conftest import REPO

FUNCTION_DIR = os.path.join(REPO, "functions", "replay-mcp")


def _load_server_module():
    path = os.path.join(FUNCTION_DIR, "server.py")
    spec = importlib.util.spec_from_file_location("replay_function", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = _load_server_module()


# --------------------------------------------------------------- state store

def test_memory_store_round_trips():
    store = MemoryStore()
    assert store.load("k") == (None, None)
    version = store.save("k", {"cursor": {"a": 1}, "journal": []}, None)
    state, loaded = store.load("k")
    assert state["cursor"] == {"a": 1}
    assert loaded == version


def test_stale_version_is_a_conflict_not_a_silent_overwrite():
    """Two writers on one session means the replay is already unordered."""
    store = MemoryStore()
    version = store.save("k", {"cursor": {}, "journal": []}, None)
    store.save("k", {"cursor": {"a": 1}, "journal": []}, version)
    with pytest.raises(Conflict):
        store.save("k", {"cursor": {"a": 9}, "journal": []}, version)


def test_creating_twice_is_a_conflict():
    store = MemoryStore()
    store.save("k", {}, None)
    with pytest.raises(Conflict):
        store.save("k", {}, None)


def test_state_survives_a_round_trip_through_the_store():
    """A cursor that does not persist is a replay that repeats its first read."""
    data = _cassette_fixture()
    cassette = mcp_core.Cassette(data)
    cassette.call("cw_get_ticket", {"ticket_number": 1})
    dumped = json.loads(json.dumps(cassette.dump_state()))

    resumed = mcp_core.Cassette(data)
    resumed.load_state(dumped)
    rec, entry = resumed.call("cw_get_ticket", {"ticket_number": 1})
    assert entry["outcome"] == "matched"
    assert rec["result"] == "second"


def test_missing_state_starts_a_fresh_replay():
    """Losing a state blob must not read as an agent regression."""
    cassette = mcp_core.Cassette(_cassette_fixture())
    cassette.load_state(None)
    assert cassette.summary()["replayed_calls"] == 0


def _cassette_fixture():
    def interaction(seq, result):
        return {"seq": seq, "agent": "a", "tool": "cw_get_ticket",
                "key": 'cw_get_ticket|{"ticket_number":1}',
                "arguments": {"ticket_number": 1}, "result": result,
                "success": True, "is_write": False, "truncated": False,
                "error_kind": "", "duration_ms": 1}
    return {"schema": "verve/mcp-cassette@1", "cassette_version": 1,
            "orchestration_id": "op-fixture", "recorded": "2026-09-03T17:00:00Z",
            "agents": ["a"], "writes": 0, "lossy": False, "warnings": [],
            "interactions": [interaction(0, "first"), interaction(1, "second")]}


# ------------------------------------------------------------ cassette choice

def test_url_beats_setting_beats_the_only_one(tmp_path):
    (tmp_path / "only.json").write_text("{}", encoding="utf-8")
    env = {"REPLAY_CASSETTE_DIR": str(tmp_path),
           "REPLAY_PAYLOAD": str(tmp_path / "absent.json")}
    config = server.Config(dict(env, REPLAY_CASSETTE="from-setting"))
    source = server.Source(config)
    assert server.resolve_cassette_id(config, source, "from-url") == "from-url"
    assert server.resolve_cassette_id(config, source, None) == "from-setting"

    bare = server.Config(dict(env))
    assert server.resolve_cassette_id(bare, server.Source(bare), None) == "only"

    (tmp_path / "other.json").write_text("{}", encoding="utf-8")
    ambiguous = server.Config(dict(env))
    assert server.resolve_cassette_id(
        ambiguous, server.Source(ambiguous), None) is None


def test_payload_is_preferred_over_directories(tmp_path):
    """Deployment is flat; a checkout is not. One reader, both shapes."""
    directory = tmp_path / "cassettes"
    directory.mkdir()
    (directory / "from-disk.json").write_text(json.dumps(_cassette_fixture()), encoding="utf-8")

    config = server.Config({"REPLAY_CASSETTE_DIR": str(directory),
                            "REPLAY_PAYLOAD": str(tmp_path / "absent.json")})
    assert server.Source(config).cassette_ids() == ["from-disk"]

    payload = tmp_path / "replay_payload.json"
    payload.write_text(json.dumps({
        "schema": "verve/replay-payload@1",
        "cassettes": {"from-payload": _cassette_fixture()},
        "tool_manifests": [{"tools": []}]}), encoding="utf-8")
    flat = server.Config({"REPLAY_CASSETTE_DIR": str(directory),
                          "REPLAY_PAYLOAD": str(payload)})
    source = server.Source(flat)
    assert source.cassette_ids() == ["from-payload"]
    assert source.cassette("from-payload")["orchestration_id"] == "op-fixture"
    assert source.manifests() == [{"tools": []}]


def test_the_package_has_no_directories(tmp_path):
    """The failure that cost three deploys, asserted.

    The deployment keeps files at the root of wwwroot and drops
    subdirectories. lib/ went that way first; once it was flattened,
    tool_manifests/ went the same way. A flat package cannot lose a directory
    because it does not have one.
    """
    source = _read(os.path.join(FUNCTION_DIR, "build.py"))
    assert "the package contains directories" in source
    assert "PAYLOAD_NAME" in source


# ------------------------------------------------------------------ the server

@pytest.fixture
def hosted(tmp_path):
    """The function's own server, on a port, with a token."""
    from http.server import ThreadingHTTPServer

    cassette_dir = tmp_path / "cassettes"
    cassette_dir.mkdir()
    (cassette_dir / "fixture.json").write_text(json.dumps(_cassette_fixture()), encoding="utf-8")

    config = server.Config({"REPLAY_CASSETTE_DIR": str(cassette_dir),
                            "REPLAY_TOOL_DEFS": os.path.join(REPO,
                                                             "tool_manifests"),
                            "REPLAY_TOKEN": "s3cret"})
    server.Handler.config = config
    server.Handler.library = server.Library(config)
    server.Handler.store = MemoryStore()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()


def _post(base, path, payload, token="s3cret", session=None):
    req = urllib.request.Request(base + path,
                                 json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if session:
        req.add_header("Mcp-Session-Id", session)
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read()), dict(response.headers)


def _get(base, path, token="s3cret", session=None):
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if session:
        req.add_header("Mcp-Session-Id", session)
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read())


def test_health_is_open_and_says_nothing_useful(hosted):
    body = _get(hosted, "/", token=None)
    assert body["status"] == "ok"
    assert body["writes"] == "never performed"


def test_mcp_requires_the_token(hosted):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(hosted, "/mcp/fixture", {"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/list"}, token=None)
    assert exc.value.code == 401


def test_initialize_issues_a_session(hosted):
    body, headers = _post(hosted, "/mcp/fixture",
                          {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert body["result"]["protocolVersion"] == mcp_core.PROTOCOL_VERSION
    assert headers.get("Mcp-Session-Id")


def test_two_sessions_do_not_share_a_cursor(hosted):
    """Concurrent replays of one cassette must not consume each other's queue."""
    _, first = _post(hosted, "/mcp/fixture",
                     {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    _, second = _post(hosted, "/mcp/fixture",
                      {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    a, b = first["Mcp-Session-Id"], second["Mcp-Session-Id"]
    assert a != b

    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "cw_get_ticket",
                       "arguments": {"ticket_number": 1}}}
    body_a, _ = _post(hosted, "/mcp/fixture", call, session=a)
    body_b, _ = _post(hosted, "/mcp/fixture", call, session=b)
    assert body_a["result"]["content"][0]["text"] == "first"
    assert body_b["result"]["content"][0]["text"] == "first"

    body_a2, _ = _post(hosted, "/mcp/fixture", call, session=a)
    assert body_a2["result"]["content"][0]["text"] == "second"


def test_a_session_header_sent_twice_is_one_session(hosted):
    """What Foundry's toolbox actually sends: the configured session plus
    the echoed one, joined by the Functions host. Observed in the state
    container as `<cassette>.<id>, <id>.json` for every gated replay."""
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "cw_get_ticket",
                       "arguments": {"ticket_number": 1}}}
    _, headers = _post(hosted, "/mcp/fixture",
                       {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                       session="abc123")
    assert headers["Mcp-Session-Id"] == "abc123"
    _post(hosted, "/mcp/fixture", call, session="abc123, abc123")
    _post(hosted, "/mcp/fixture", call, session="abc123")

    mine = _get(hosted, "/summary/fixture", session="abc123")
    assert mine["session"] == "abc123"
    assert mine["replayed_calls"] == 2
    assert _get(hosted, "/summary/fixture", session="abc123,abc123") \
        ["replayed_calls"] == 2


def test_two_different_sessions_in_one_header_are_refused(hosted):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(hosted, "/mcp/fixture",
              {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "cw_get_ticket",
                          "arguments": {"ticket_number": 1}}},
              session="aaa, bbb")
    assert exc.value.code == 400
    assert "2 different sessions" in json.loads(exc.value.read()) \
        ["error"]["message"]


def test_closing_a_session_is_refused_and_keeps_the_journal(hosted):
    _, headers = _post(hosted, "/mcp/fixture",
                       {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    session = headers["Mcp-Session-Id"]
    _post(hosted, "/mcp/fixture",
          {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
           "params": {"name": "cw_get_ticket",
                      "arguments": {"ticket_number": 1}}}, session=session)
    req = urllib.request.Request(hosted + "/mcp/fixture", method="DELETE")
    req.add_header("Authorization", "Bearer s3cret")
    req.add_header("Mcp-Session-Id", session)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 405
    assert _get(hosted, "/summary/fixture", session=session) \
        ["replayed_calls"] == 1


def test_ordered_playback_across_calls(hosted):
    _, headers = _post(hosted, "/mcp/fixture",
                       {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    session = headers["Mcp-Session-Id"]
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "cw_get_ticket",
                       "arguments": {"ticket_number": 1}}}
    seen = [_post(hosted, "/mcp/fixture", call, session=session)[0]
            ["result"]["content"][0]["text"] for _ in range(3)]
    # Third call is past the end of the queue; the default repeats the last.
    assert seen == ["first", "second", "second"]


def test_an_unrecorded_call_diverges_rather_than_inventing_an_answer(hosted):
    _, headers = _post(hosted, "/mcp/fixture",
                       {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    body, _ = _post(hosted, "/mcp/fixture",
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "cw_get_ticket",
                                "arguments": {"ticket_number": 999}}},
                    session=headers["Mcp-Session-Id"])
    assert body["result"]["isError"] is True
    assert json.loads(body["result"]["content"][0]["text"])["error"] \
        == "not_recorded"


def test_summary_is_per_session_and_needs_the_token(hosted):
    _, headers = _post(hosted, "/mcp/fixture",
                       {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    session = headers["Mcp-Session-Id"]
    _post(hosted, "/mcp/fixture",
          {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
           "params": {"name": "cw_get_ticket",
                      "arguments": {"ticket_number": 1}}}, session=session)

    mine = _get(hosted, "/summary/fixture", session=session)
    assert mine["replayed_calls"] == 1
    assert mine["matched_prefix"] == 1

    other = _get(hosted, "/summary/fixture?session=never-ran")
    assert other["replayed_calls"] == 0

    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(hosted, "/summary/fixture", token=None)
    assert exc.value.code == 401


def test_unknown_cassette_is_a_404(hosted):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(hosted, "/mcp/nope", {"jsonrpc": "2.0", "id": 1,
                                    "method": "tools/list"})
    assert exc.value.code == 404


# ------------------------------------------- cassettes uploaded for one run
#
# The gate replays the agent's recent real runs, fetched when it starts, so
# they cannot be baked into a deployment: run_replay.py --upload PUTs one for
# the run and DELETEs it after.

RT = "rt-0123456789abcdef0123"
CALL = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "cw_get_ticket", "arguments": {"ticket_number": 1}}}


def _http(base, method, path, body=None, token="s3cret", session=None):
    """(status, decoded body), error or not."""
    req = urllib.request.Request(base + path, body, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if session:
        req.add_header("Mcp-Session-Id", session)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _upload(base, cassette_id=RT, data=None, **kw):
    body = json.dumps(_cassette_fixture() if data is None else data).encode()
    return _http(base, "PUT", f"/cassettes/{cassette_id}", body, **kw)


def test_an_uploaded_cassette_replays_and_is_gone_after_its_delete(
        hosted, blob_stub):
    from state_store import open_store
    store = server.Handler.store = open_store(sas_url=blob_stub)
    assert _upload(hosted) == (201, {"cassette": RT, "interactions": 2})
    assert RT not in _get(hosted, "/", token=None)["cassettes"]

    body, _ = _post(hosted, f"/mcp/{RT}", CALL, session="run-1")
    assert body["result"]["content"][0]["text"] == "first"
    assert _get(hosted, f"/summary/{RT}", session="run-1") \
        ["replayed_calls"] == 1
    assert store.load(server._state_key(RT, "run-1"))[0]

    assert _http(hosted, "DELETE", f"/cassettes/{RT}", session="run-1") \
        == (204, None)
    assert store.load(f"cassette.{RT}") == (None, None)
    assert store.load(server._state_key(RT, "run-1")) == (None, None)
    assert _http(hosted, "POST", f"/mcp/{RT}", json.dumps(CALL).encode(),
                 session="run-1")[0] == 404


@pytest.mark.parametrize("cassette_id", [
    "fixture", "2026-09-03-4dda7f4fa5f0", "rt-short", "rt-UPPERCASE1",
    "rt-" + "a" * 64, "rt-..%2F..%2Fstate1", "rt-0123456789abcdef/extra"])
def test_only_a_runtime_id_is_uploaded_or_deleted(hosted, cassette_id):
    """Never a deployed cassette, and nothing that names another blob."""
    assert _upload(hosted, cassette_id)[0] == 400
    assert _http(hosted, "DELETE", f"/cassettes/{cassette_id}")[0] == 400


def test_an_uploaded_cassette_is_immutable(hosted):
    assert _upload(hosted)[0] == 201
    assert _upload(hosted, data=dict(_cassette_fixture(),
                                     interactions=[]))[0] == 409
    body, _ = _post(hosted, f"/mcp/{RT}", CALL, session="s")
    assert body["result"]["content"][0]["text"] == "first"


def test_upload_and_delete_need_the_token(hosted):
    assert _upload(hosted, token=None)[0] == 401
    assert _upload(hosted, token="wrong")[0] == 401
    assert _http(hosted, "DELETE", f"/cassettes/{RT}", token=None)[0] == 401
    assert server.Handler.store.load(f"cassette.{RT}") == (None, None)


def test_an_oversized_upload_is_refused(hosted, monkeypatch):
    monkeypatch.setattr(server, "MAX_CASSETTE_BYTES", 100)
    assert _upload(hosted)[0] == 413
    assert server.Handler.store.load(f"cassette.{RT}") == (None, None)


@pytest.mark.parametrize("length", ["-1", "abc"])
def test_a_content_length_that_is_not_a_byte_count_is_refused(
        hosted, monkeypatch, length):
    """read(-1) reads to the end of the stream: a 40 MB body went past the
    limit that way, and `abc` was a traceback and a dropped connection."""
    import http.client
    from urllib.parse import urlparse
    monkeypatch.setattr(server, "MAX_CASSETTE_BYTES", 100)
    url = urlparse(hosted)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    conn.putrequest("PUT", f"/cassettes/{RT}")
    conn.putheader("Authorization", "Bearer s3cret")
    conn.putheader("Content-Length", length)
    conn.endheaders(json.dumps(_cassette_fixture()).encode())
    assert conn.getresponse().status == 400
    conn.close()
    assert server.Handler.store.load(f"cassette.{RT}") == (None, None)


@pytest.mark.parametrize("body", [b"[]", b'{"no": 1}',
                                  b'{"interactions": "x"}', b"not json", b"",
                                  b'{"interactions": [1]}',
                                  b'{"interactions": [{"tool": "x"}]}'])
def test_only_a_cassette_is_accepted(hosted, body):
    """Parsed as a lookup parses it: the last two were stored, and every
    call on them was then a traceback."""
    assert _http(hosted, "PUT", f"/cassettes/{RT}", body)[0] == 400
    assert server.Handler.store.load(f"cassette.{RT}") == (None, None)


class _Down(MemoryStore):
    """A configured store that cannot be reached."""

    def _down(self, *a):
        raise Unavailable("storage is down")

    load = save = delete = _down


def test_an_unreachable_store_is_a_503_on_every_route(hosted):
    """Resolving an uploaded cassette reads the store, so it fails the way a
    state read does: an answer naming the cause, never a traceback."""
    server.Handler.store = _Down()
    for method, path, body in (
            ("POST", f"/mcp/{RT}", json.dumps(CALL).encode()),
            ("GET", f"/summary/{RT}", None),
            ("PUT", f"/cassettes/{RT}", json.dumps(_cassette_fixture()).encode()),
            ("DELETE", f"/cassettes/{RT}", None)):
        code, answer = _http(hosted, method, path, body, session="s")
        assert code == 503, (method, answer)
        assert "storage is down" in json.dumps(answer), method


def test_deleting_is_idempotent(hosted):
    """run_replay.py deletes in its finally path, after a failure too."""
    assert _http(hosted, "DELETE", f"/cassettes/{RT}", session="s")[0] == 204
    _upload(hosted)
    for _ in range(2):
        assert _http(hosted, "DELETE", f"/cassettes/{RT}",
                     session="s")[0] == 204


def test_the_stores_delete_and_a_missing_key_is_fine(blob_stub):
    from state_store import SasBlobStore
    for store in (MemoryStore(), SasBlobStore(blob_stub)):
        store.save("k", {"a": 1}, None)
        store.delete("k")
        assert store.load("k") == (None, None)
        store.delete("k")
        store.save("k", {"a": 2}, None)      # create-only works again
    store._sas = "sv=2023-01-03"             # no signature: a 403, not a 404
    with pytest.raises(urllib.error.HTTPError):
        store.delete("k")


# ------------------------------------------------- reading back the journal

def test_the_gate_reads_the_journal_it_created(hosted):
    """The hosted server keys the journal by session; run_replay must match.

    `initialize` issues a session id and the cursor and journal hang off it.
    Reading /summary without one answers for a session nobody used, so the
    gate would report a replay that made no calls at all -- indistinguishable
    from an agent that did nothing.
    """
    import run_replay

    base = hosted + "/mcp/fixture"
    session = "known-session-id"
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "cw_get_ticket",
                       "arguments": {"ticket_number": 1}}}
    _post(base, "", {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
          session=session)
    _post(base, "", call, session=session)

    journal = run_replay.read_journal(base, "s3cret", session)
    assert journal["replayed_calls"] == 1
    assert journal["session_honoured"] is True

    # What the bug looked like: the right server, the wrong bucket.
    assert run_replay.summary(base, "s3cret",
                              "some-other-session")["replayed_calls"] == 0


def test_the_gate_falls_back_to_the_shared_session(hosted):
    """A client that ignores the header is correct, just not concurrent."""
    import run_replay

    base = hosted + "/mcp/fixture"
    _post(base, "", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    _post(base, "", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "cw_get_ticket",
                                "arguments": {"ticket_number": 1}}})

    journal = run_replay.read_journal(base, "s3cret", "never-used")
    assert journal["replayed_calls"] == 1
    assert journal["session_honoured"] is False


# ------------------------------------------------------------------- parity

def test_shared_modules_sit_beside_the_entry_point():
    """Not in a subdirectory, which is what broke the first deployment.

    lib/ did not survive the remote build. The app came up with sys.path
    pointing at /home and nothing to import, and the only clue was a 502.
    Python puts a script's own directory on sys.path, so a file beside
    server.py cannot be dropped without dropping server.py too.
    """
    source = _read(os.path.join(FUNCTION_DIR, "build.py"))
    assert 'shutil.copy2(path, OUT)' in source
    assert '"lib"' not in source


def test_hosted_and_local_servers_share_one_dispatch():
    """Not two implementations that happen to agree today."""
    assert server.handle_rpc is rs.handle_rpc is mcp_core.handle_rpc
    assert server.Cassette is rs.Cassette is mcp_core.Cassette
    assert server.tool_definitions is rs.tool_definitions


def test_a_write_is_replayed_and_never_performed():
    """The guarantee, asserted rather than documented."""
    data = _cassette_fixture()
    data["interactions"] = [{
        "seq": 0, "agent": "a", "tool": "cw_update",
        "key": 'cw_update|{"id":1}', "arguments": {"id": 1},
        "result": '{"id": 805545, "status": "Closed"}', "success": True,
        "is_write": True, "truncated": False, "error_kind": "",
        "duration_ms": 1}]
    cassette = mcp_core.Cassette(data)
    tools, _ = mcp_core.tool_definitions(cassette, [])
    body = mcp_core.handle_rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "cw_update", "arguments": {"id": 1}}},
        cassette, tools)
    assert body["result"]["content"][0]["text"] == \
        '{"id": 805545, "status": "Closed"}'
    assert cassette.summary()["writes_attempted"] == 1


# ----------------------------------------------------- why a custom handler

def test_the_manifest_carries_schema_the_mcp_extension_cannot_advertise():
    """This is the reason server.py is a custom handler, not an mcpToolTrigger.

    The Functions MCP extension describes a tool with a flat list of
    `{propertyName, propertyType, description, isRequired, isArray}`. There is
    nowhere in that shape to put an `enum`. Advertising `reference_type` as a
    bare string would tell the agent under test it may send values production
    rejects, and the divergence would be ours, not the agent's.

    If this ever fails because the enums are gone, the extension becomes a
    live option again -- reopen the decision rather than deleting the test.
    """
    path = os.path.join(REPO, "tool_manifests", "connectwisemcp.json")
    with open(path, encoding="utf-8") as fh:
        tools = json.load(fh)["tools"]

    enums = {}
    for tool in tools:
        for name, schema in ((tool.get("parameters") or {})
                             .get("properties") or {}).items():
            options = schema.get("enum") or next(
                (branch["enum"] for branch in schema.get("anyOf", [])
                 if "enum" in branch), None)
            if options:
                enums[f"{tool['name']}.{name}"] = len(options)

    assert enums, "no enums left in the manifest"
    assert enums.get("cw_resolve.reference_type", 0) >= 20


# ----------------------------------------------------- state without an SDK

@pytest.fixture
def blob_stub():
    """A blob endpoint that enforces Azure's conditional-write semantics."""
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    blobs = {}

    class Blob(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _key(self):
            return self.path.split("?")[0]

        def do_GET(self):
            if "sig=" not in self.path:          # the SAS is the credential
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            entry = blobs.get(self._key())
            if entry is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body, etag = entry
            self.send_response(200)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            assert self.headers.get("x-ms-blob-type") == "BlockBlob"
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            existing = blobs.get(self._key())
            if self.headers.get("If-None-Match") == "*" and existing:
                return self._status(409)
            match = self.headers.get("If-Match")
            if match and (not existing or existing[1] != match):
                return self._status(412)
            etag = f'"{uuid.uuid4().hex}"'
            blobs[self._key()] = (body, etag)
            self.send_response(201)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_DELETE(self):
            if "sig=" not in self.path:
                return self._status(403)
            self._status(202 if blobs.pop(self._key(), None) else 404)

        def _status(self, code):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Blob)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield (f"http://127.0.0.1:{httpd.server_address[1]}/replay-state"
           "?sv=2023-01-03&sig=stub&sp=rw")
    httpd.shutdown()


def test_sas_store_needs_no_sdk(blob_stub):
    """azure-storage-blob is not importable in a custom handler.

    Oryx installs it into .python_packages/lib/site-packages, which the
    Functions *Python worker* puts on sys.path; a custom handler is
    `python server.py` and gets none of that. The deployed app logged
    `ModuleNotFoundError: No module named 'azure'` with the package plainly
    there. So state goes over the REST API with a SAS, in stdlib.
    """
    import state_store
    from state_store import SasBlobStore
    body = _read(state_store.__file__)
    assert "import azure" not in body and "from azure" not in body

    store = SasBlobStore(blob_stub)
    assert store.load("k") == (None, None)
    version = store.save("k", {"cursor": {"a": 1}, "journal": [1]}, None)
    state, loaded = store.load("k")
    assert state["cursor"] == {"a": 1}
    assert loaded == version


def test_sas_store_refuses_a_lost_update(blob_stub):
    """Two instances on one replay means the ordering is already broken."""
    from state_store import Conflict, SasBlobStore
    store = SasBlobStore(blob_stub)
    version = store.save("k", {"journal": []}, None)
    store.save("k", {"journal": [1]}, version)
    with pytest.raises(Conflict):
        store.save("k", {"journal": [99]}, version)      # stale ETag -> 412
    with pytest.raises(Conflict):
        store.save("k", {}, None)                        # create again -> 409


def _lose_response(store, landed):
    """The next PUT gets no response, as on the deployed instance that timed
    out. `landed` decides whether the write reached storage first."""
    import urllib.error
    real = store._put
    calls = []

    def put(key, body, headers):
        calls.append(key)
        if len(calls) == 1:
            if landed:
                real(key, body, headers)
            raise urllib.error.URLError("timed out")
        return real(key, body, headers)
    store._put = put
    return calls


def test_a_save_whose_response_was_lost_but_landed_is_not_written_twice(blob_stub):
    from state_store import SasBlobStore
    store = SasBlobStore(blob_stub)
    version = store.save("k", {"journal": [0]}, None)
    calls = _lose_response(store, landed=True)
    etag = store.save("k", {"journal": [0, 1]}, version)
    state, now = store.load("k")
    assert state["journal"] == [0, 1] and now == etag
    assert len(calls) == 1                     # no second PUT


def test_a_save_whose_response_was_lost_and_did_not_land_is_retried(blob_stub):
    """The failure the deployed gate hit: one slow write, refused, gate red."""
    from state_store import SasBlobStore
    store = SasBlobStore(blob_stub)
    version = store.save("k", {"journal": [0]}, None)
    calls = _lose_response(store, landed=False)
    etag = store.save("k", {"journal": [0, 1]}, version)
    state, now = store.load("k")
    assert state["journal"] == [0, 1] and now == etag
    assert len(calls) == 2
    # A first write (create) is settled the same way.
    calls = _lose_response(store, landed=False)
    store.save("new", {"journal": [7]}, None)
    assert store.load("new")[0]["journal"] == [7] and len(calls) == 2


def test_a_lost_save_after_another_writer_won_is_a_conflict(blob_stub):
    import urllib.error
    from state_store import Conflict, SasBlobStore
    store = SasBlobStore(blob_stub)
    version = store.save("k", {"journal": [0]}, None)
    real = store._put

    def put(key, body, headers):
        store._put = real
        real(key, b'{"journal": [0, 9]}', {**headers})   # someone else's write
        raise urllib.error.URLError("timed out")
    store._put = put
    with pytest.raises(Conflict):
        store.save("k", {"journal": [0, 1]}, version)
    assert store.load("k")[0]["journal"] == [0, 9]


def test_sas_store_keeps_every_call_of_a_replay(blob_stub):
    """The failure this replaces: a journal with 3 of 50 calls in it."""
    from state_store import SasBlobStore
    store = SasBlobStore(blob_stub)
    _state, version = store.load("big")
    journal = []
    for seq in range(50):
        journal.append(seq)
        version = store.save("big", {"cursor": {}, "journal": list(journal)},
                             version)
    final, _ = store.load("big")
    assert len(final["journal"]) == 50


def _fan_out(base, n, session):
    """n concurrent tools/call on one session, as the ops agent sends them."""
    import concurrent.futures as cf

    call = {"jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "cw_get_ticket",
                       "arguments": {"ticket_number": 1}}}

    def one(i):
        try:
            body, _ = _post(base, "/mcp/fixture", dict(call, id=i),
                            session=session)
            return 200, body
        except urllib.error.HTTPError as exc:
            return exc.code, None

    with cf.ThreadPoolExecutor(n) as pool:
        return list(pool.map(one, range(n)))


def test_a_fan_out_in_one_session_is_answered_and_journalled(hosted, blob_stub):
    """The recordings show up to nine MCP calls in flight at once. Against a
    store that saves conditionally on the ETag -- the real one -- every call
    but one used to lose the race, get a 409 and go unjournalled: the agent
    saw errors where the recording saw results."""
    from state_store import SasBlobStore
    server.Handler.store = SasBlobStore(blob_stub)
    results = _fan_out(hosted, 9, "fan-out")
    assert [code for code, _ in results] == [200] * 9
    summary = _get(hosted, "/summary/fixture", session="fan-out")
    assert summary["replayed_calls"] == 9
    texts = sorted(b["result"]["content"][0]["text"] for _, b in results)
    assert texts.count("first") == 1        # each recorded answer used once


class _LosesFirstRaces(MemoryStore):
    """Another instance wins the first `n` saves of each call."""

    def __init__(self, n):
        super().__init__()
        self.to_lose = n

    def save(self, key, state, version):
        if self.to_lose:
            self.to_lose -= 1
            # the winner's write lands first: bump the stored version
            current, v = self.load(key)
            super().save(key, current, v)
            raise Conflict(key)
        return super().save(key, state, version)


class _AnotherInstanceWins(MemoryStore):
    """Before our first save, another instance answers the same call -- a
    real Cassette consumes the head of the queue and its state is saved.

    The first version of this fake re-saved unchanged state, so a retry that
    overwrote the winner, or kept its first answer, passed. A reviewer
    mutated both into the server and the suite stayed green.
    """

    def __init__(self):
        super().__init__()
        self.raced = False

    def save(self, key, state, version):
        if not self.raced:
            self.raced = True
            from mcp_core import Cassette
            winner = Cassette(_cassette_fixture())
            current, v = self.load(key)
            winner.load_state(current)
            winner.call("cw_get_ticket", {"ticket_number": 1})
            super().save(key, winner.dump_state(), v)
            raise Conflict(key)
        return super().save(key, state, version)


def test_a_lost_race_is_answered_again_from_the_winners_state(hosted):
    server.Handler.store = _AnotherInstanceWins()
    body, _ = _post(hosted, "/mcp/fixture", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "cw_get_ticket", "arguments": {"ticket_number": 1}}},
        session="race")
    # the winner took "first"; re-applied on its state, ours is "second"
    assert body["result"]["content"][0]["text"] == "second"
    summary = _get(hosted, "/summary/fixture", session="race")
    assert summary["replayed_calls"] == 2       # the winner's call survived


class _CountsConflicts(MemoryStore):
    def __init__(self):
        super().__init__()
        self.conflicts = 0

    def save(self, key, state, version):
        import time as _t
        _t.sleep(0.01)                          # a blob round trip, roughly
        try:
            return super().save(key, state, version)
        except Conflict:
            self.conflicts += 1
            raise


def test_one_instance_serialises_a_session_rather_than_racing_itself(hosted):
    """The retry is for another instance. Within one, the per-session lock
    means a fan-out never conflicts at all -- without it, every call in the
    burst would race the others and the retry would be doing the lock's job,
    one lost blob round trip at a time."""
    store = _CountsConflicts()
    server.Handler.store = store
    results = _fan_out(hosted, 9, "one-instance")
    assert [code for code, _ in results] == [200] * 9
    assert store.conflicts == 0


def test_a_race_lost_every_time_is_a_409(hosted, monkeypatch):
    monkeypatch.setattr(server, "SAVE_ATTEMPTS", 3)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    server.Handler.store = _LosesFirstRaces(10_000)
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(hosted, "/mcp/fixture", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "cw_get_ticket",
                       "arguments": {"ticket_number": 1}}}, session="x")
    assert exc.value.code == 409


@pytest.fixture
def identity_stub(monkeypatch):
    """The platform's managed-identity endpoint, and a blob endpoint that
    accepts only the bearer token it issued. Enforces what the real ones do:
    the X-IDENTITY-HEADER echo, api-version 2019-08-01, and x-ms-version on
    an OAuth blob request."""
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    # Knobs: token_fail answers the next N token requests with a 500;
    # forbid answers every blob request with a 403, as storage does before a
    # role reaches the identity. A container named "missing" does not exist.
    seen = {"token_requests": [], "expires_in": 3600, "token_fail": 0,
            "forbid": False, "blob_busy": 0}
    token = "tok-" + uuid.uuid4().hex
    secret = "hdr-" + uuid.uuid4().hex
    blobs = {}

    class Identity(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            seen["token_requests"].append(q)
            if seen["token_fail"] > 0:
                seen["token_fail"] -= 1
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            ok = (self.headers.get("X-IDENTITY-HEADER") == secret
                  and q.get("api-version") == "2019-08-01"
                  and q.get("resource") == "https://storage.azure.com/")
            body = json.dumps({"access_token": token, "token_type": "Bearer",
                               "expires_on": str(int(__import__("time").time())
                                                 + seen["expires_in"]),
                               "resource": q.get("resource")}).encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Blob(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _authorised(self):
            if (seen["forbid"]
                    or self.headers.get("Authorization") != f"Bearer {token}"
                    or self.headers.get("x-ms-version", "") < "2017-11-09"):
                self._status(403)
                return False
            if self.path.startswith("/missing/"):
                self._status(404, error="ContainerNotFound")
                return False
            return True

        def _status(self, code, etag=None, error=None):
            self.send_response(code)
            if etag:
                self.send_header("ETag", etag)
            if error:
                self.send_header("x-ms-error-code", error)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self._authorised():
                return
            if seen["blob_busy"] > 0:           # storage's ServerBusy
                seen["blob_busy"] -= 1
                return self._status(503, error="ServerBusy")
            entry = blobs.get(self.path)
            if entry is None:
                return self._status(404, error="BlobNotFound")
            body, etag = entry
            self.send_response(200)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            if not self._authorised():
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            existing = blobs.get(self.path)
            if self.headers.get("If-None-Match") == "*" and existing:
                return self._status(409)
            match = self.headers.get("If-Match")
            if match and (not existing or existing[1] != match):
                return self._status(412)
            etag = f'"{uuid.uuid4().hex}"'
            blobs[self.path] = (body, etag)
            self._status(201, etag)

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), h) for h in (Identity, Blob)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    monkeypatch.setenv("IDENTITY_ENDPOINT",
                       f"http://127.0.0.1:{servers[0].server_address[1]}/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", secret)
    seen["account"] = f"http://127.0.0.1:{servers[1].server_address[1]}"
    yield seen
    for s in servers:
        s.shutdown()


def test_identity_store_keeps_state_with_a_token_and_no_secret(identity_stub):
    from state_store import IdentityBlobStore
    store = IdentityBlobStore(identity_stub["account"], "replay-state",
                              client_id="cid-123")
    assert store.load("k") == (None, None)
    version = store.save("k", {"cursor": {"a": 1}}, None)
    assert store.load("k") == ({"cursor": {"a": 1}}, version)
    request = identity_stub["token_requests"][0]
    assert request["client_id"] == "cid-123"     # the user-assigned identity
    assert store.detail == {"auth": "managed identity", "client_id": "cid-123"}


def test_identity_store_refuses_a_lost_update(identity_stub):
    from state_store import Conflict, IdentityBlobStore
    store = IdentityBlobStore(identity_stub["account"], "c")
    v1 = store.save("k", {"n": 1}, None)
    store.save("k", {"n": 2}, v1)
    with pytest.raises(Conflict):
        store.save("k", {"n": 3}, v1)
    store.save("fresh", {"n": 1}, None)
    with pytest.raises(Conflict):                # created twice
        store.save("fresh", {"n": 1}, None)


def test_identity_token_is_cached_until_it_nears_expiry(identity_stub):
    from state_store import IdentityBlobStore
    store = IdentityBlobStore(identity_stub["account"], "c")
    for i in range(5):
        store.save(f"k{i}", {"i": i}, None)
    assert len(identity_stub["token_requests"]) == 1
    identity_stub["expires_in"] = 60        # inside the refresh margin
    store._token = None
    store.load("k0")
    store.load("k1")
    assert len(identity_stub["token_requests"]) == 3


def test_identity_store_needs_no_sdk(identity_stub, monkeypatch):
    """The whole reason it exists: the SDK is not importable in a custom
    handler. With every azure.* import failing, it still keeps state."""
    import builtins
    import importlib
    real = builtins.__import__

    def no_azure(name, *a, **k):
        if name == "azure" or name.startswith("azure."):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_azure)
    # A private copy of the module, imported with azure unavailable -- not a
    # reload of the shared one, which would swap out the Conflict class the
    # server catches and break every test after this.
    spec = importlib.util.spec_from_file_location(
        "state_store_no_sdk", os.path.join(REPO, "replay", "state_store.py"))
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    store = fresh.open_store(identity_stub["account"], "c", client_id="x")
    store.save("k", {"ok": True}, None)
    assert store.backend == "IdentityBlobStore"


def test_no_identity_endpoint_is_unavailable_and_says_which_variable(
        monkeypatch):
    """Configured means durable or an error -- never in-process state."""
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.delenv("IDENTITY_HEADER", raising=False)
    from state_store import Unavailable, open_store
    store = open_store("https://acct.blob.core.windows.net/", "c")
    with pytest.raises(Unavailable):
        store.load("k")
    assert store.backend == "unavailable"
    assert store.detail["wanted"] == "IdentityBlobStore"
    assert "IDENTITY_ENDPOINT" in store.detail["error"]


def test_a_failed_resolution_is_retried_not_kept(identity_stub, monkeypatch):
    """One failed token request used to pin an instance to in-process state
    for its whole life. Now it is an error until the next attempt, and the
    attempt after that can succeed."""
    import state_store
    from state_store import Unavailable, open_store
    monkeypatch.setattr(state_store.time, "sleep", lambda s: None)
    store = open_store(identity_stub["account"], "c", client_id="cid")
    identity_stub["token_fail"] = 99
    with pytest.raises(Unavailable):
        store.load("k")
    asked = len(identity_stub["token_requests"])
    with pytest.raises(Unavailable):       # inside RETRY_AFTER: not re-tried
        store.load("k")
    assert len(identity_stub["token_requests"]) == asked
    identity_stub["token_fail"] = 0
    monkeypatch.setattr(store, "_retry_at", 0.0)
    assert store.load("k") == (None, None)
    assert store.backend == "IdentityBlobStore"


def test_a_transient_token_failure_is_retried(identity_stub, monkeypatch):
    import state_store
    from state_store import IdentityBlobStore
    monkeypatch.setattr(state_store.time, "sleep", lambda s: None)
    identity_stub["token_fail"] = 2
    IdentityBlobStore(identity_stub["account"], "c").save("k", {}, None)
    assert len(identity_stub["token_requests"]) == 3


def test_a_failed_refresh_uses_the_token_that_is_still_valid(identity_stub,
                                                            monkeypatch,
                                                            capsys):
    import time
    import state_store
    from state_store import IdentityBlobStore
    monkeypatch.setattr(state_store.time, "sleep", lambda s: None)
    store = IdentityBlobStore(identity_stub["account"], "c")
    store._expires = time.time() + 200          # inside the refresh margin
    identity_stub["token_fail"] = 99
    assert store.load("k") == (None, None)
    assert "using the cached token" in capsys.readouterr().out
    store._expires = time.time() - 1            # and now actually expired
    with pytest.raises(urllib.error.HTTPError):
        store.load("k")


def test_a_slow_failure_is_attempted_once_not_once_per_waiting_call(
        monkeypatch):
    """The retry window runs from when an attempt FAILED. Timed from its
    start, a failure slower than the window left it already expired, and
    every call queued behind the lock waited out an attempt of its own."""
    import concurrent.futures as cf
    import time
    from state_store import LazyStore, Unavailable
    attempts = []

    def slow_failure():
        attempts.append(1)
        time.sleep(0.3)
        raise OSError("no answer")
    store = LazyStore(slow_failure, "SasBlobStore")
    monkeypatch.setattr(store, "RETRY_AFTER", 0.2)

    def one(_):
        with pytest.raises(Unavailable):
            store.load("k")
    with cf.ThreadPoolExecutor(6) as pool:
        list(pool.map(one, range(6)))
    assert len(attempts) == 1


def test_a_store_that_fails_after_resolving_says_so(hosted, identity_stub,
                                                    capsys):
    """Resolved once is not working now: health, verify and the log all
    carry the failure, and it clears when storage comes back."""
    from state_store import open_store
    server.Handler.store = open_store(identity_stub["account"], "c",
                                      client_id="cid")
    assert _get(hosted, "/?resolve=1")["state"] == "IdentityBlobStore"

    identity_stub["forbid"] = True             # e.g. the role was removed
    with pytest.raises(urllib.error.HTTPError) as exc:
        _call(hosted, "after")
    assert exc.value.code == 503
    health = _get(hosted, "/?resolve=1")
    assert health["state"] == "IdentityBlobStore"
    assert "403" in health["state_detail"]["error"]
    problems = verify.check_state(health)
    assert problems and "10 minutes" in problems[0]
    out = capsys.readouterr().out
    assert "load failed: HTTPError" in out
    assert "REFUSED  -32003 session=after cassette=fixture" in out

    identity_stub["forbid"] = False
    health = _get(hosted, "/?resolve=1")
    assert "error" not in health["state_detail"]
    assert verify.check_state(health) == []


def test_a_busy_blob_read_is_retried(identity_stub, monkeypatch):
    import state_store
    from state_store import IdentityBlobStore
    monkeypatch.setattr(state_store.time, "sleep", lambda s: None)
    store = IdentityBlobStore(identity_stub["account"], "c")
    identity_stub["blob_busy"] = 2
    assert store.load("k") == (None, None)


def test_a_failed_refresh_is_not_retried_on_every_call(identity_stub,
                                                       monkeypatch):
    import time
    import state_store
    from state_store import IdentityBlobStore
    monkeypatch.setattr(state_store.time, "sleep", lambda s: None)
    store = IdentityBlobStore(identity_stub["account"], "c")
    store._expires = time.time() + 200          # inside the refresh margin
    identity_stub["token_fail"] = 99
    store.load("a")
    asked = len(identity_stub["token_requests"])
    store.load("b")
    store.load("c")
    assert len(identity_stub["token_requests"]) == asked


def test_a_missing_container_is_not_a_successful_probe(identity_stub):
    """A 404 for the probe blob means the credential works; a 404 for the
    container means every save will fail."""
    from state_store import IdentityBlobStore
    with pytest.raises(urllib.error.HTTPError) as exc:
        IdentityBlobStore(identity_stub["account"], "missing")
    assert exc.value.headers.get("x-ms-error-code") == "ContainerNotFound"


def test_a_fan_out_is_journalled_through_the_identity_store(hosted,
                                                           identity_stub):
    from state_store import open_store
    server.Handler.store = open_store(identity_stub["account"], "c",
                                      client_id="cid")
    results = _fan_out(hosted, 9, "fan-identity")
    assert [code for code, _ in results] == [200] * 9
    assert _get(hosted, "/summary/fixture",
                session="fan-identity")["replayed_calls"] == 9
    health = _get(hosted, "/")
    assert health["state"] == "IdentityBlobStore"
    assert health["state_detail"]["auth"] == "managed identity"


def test_a_sas_says_when_it_expires(blob_stub):
    from state_store import SasBlobStore, sas_expiry
    url = blob_stub + "&se=2027-09-23T00:00:00Z"
    assert sas_expiry(url) == "2027-09-23T00:00:00Z"
    assert SasBlobStore(url).detail["expires"] == "2027-09-23T00:00:00Z"


def test_a_store_that_does_not_answer_is_unavailable_not_in_process():
    """And it still says when its SAS expires: that is usually why."""
    from state_store import Unavailable, open_store
    store = open_store(sas_url="http://127.0.0.1:1/x?sig=no&se=2025-01-01")
    with pytest.raises(Unavailable):
        store.load("k")
    assert store.backend == "unavailable"
    assert store.detail["expires"] == "2025-01-01"
    assert store.detail["wanted"] == "SasBlobStore"
    assert store.detail["error"]


def _two_instances(identity_stub):
    from state_store import open_store
    return (open_store(identity_stub["account"], "c", client_id="cid"),
            open_store(identity_stub["account"], "c", client_id="cid"))


def _call(base, session):
    return _post(base, "/mcp/fixture", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "cw_get_ticket",
                   "arguments": {"ticket_number": 1}}}, session=session)[0]


def test_an_instance_whose_store_failed_answers_nothing(hosted, identity_stub,
                                                        monkeypatch):
    """Two instances of one app, one replay. Instance B's first token request
    fails. It used to fall back to its own in-process cursor and answer the
    second call with the FIRST recorded result; it now answers with an error,
    and once the store works it answers from the shared cursor."""
    import state_store
    monkeypatch.setattr(state_store.time, "sleep", lambda s: None)
    a, b = _two_instances(identity_stub)

    server.Handler.store = a
    assert "first" in json.dumps(_call(hosted, "s"))

    server.Handler.store = b
    identity_stub["token_fail"] = 99
    with pytest.raises(urllib.error.HTTPError) as exc:
        _call(hosted, "s")
    assert exc.value.code == 503
    body = json.loads(exc.value.read())
    assert body["error"]["code"] == -32003
    assert "replay state unavailable" in body["error"]["message"]
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(hosted, "/summary/fixture", session="s")
    assert exc.value.code == 503

    identity_stub["token_fail"] = 0
    monkeypatch.setattr(b, "_retry_at", 0.0)
    assert "second" in json.dumps(_call(hosted, "s"))
    assert b.backend == "IdentityBlobStore"
    assert _get(hosted, "/summary/fixture", session="s")["replayed_calls"] == 2


def test_health_resolves_the_store_only_when_asked(hosted, blob_stub):
    from state_store import open_store
    server.Handler.store = open_store(sas_url=blob_stub)
    assert _get(hosted, "/")["state"] == "unresolved"
    health = _get(hosted, "/?resolve=1")
    assert health["state"] == "SasBlobStore"
    assert verify.check_state(health) == []


def test_health_says_why_a_store_is_unavailable(hosted, identity_stub):
    """Before a role reaches the identity, storage answers 403."""
    from state_store import open_store
    identity_stub["forbid"] = True
    server.Handler.store = open_store(identity_stub["account"], "c",
                                      client_id="cid")
    health = _get(hosted, "/?resolve=1")
    assert health["state"] == "unavailable"
    assert "403" in health["state_detail"]["error"]
    problems = verify.check_state(health)
    assert problems and "10 minutes" in problems[0]


def test_the_deployed_settings_reach_the_store_they_name(identity_stub,
                                                         blob_stub):
    """The last silent failure was here: a setting reaching the wrong store."""
    identity = server.build_store(server.Config({
        "REPLAY_STATE_ACCOUNT": identity_stub["account"],
        "REPLAY_STATE_CONTAINER": "replay-state",
        "AZURE_CLIENT_ID": "cid-from-settings"}))
    identity.load("k")
    assert identity.backend == "IdentityBlobStore"
    assert identity_stub["token_requests"][-1]["client_id"] == \
        "cid-from-settings"

    sas = server.build_store(server.Config({
        "REPLAY_STATE_SAS": blob_stub,
        "REPLAY_STATE_CONTAINER": "replay-state"}))
    sas.load("k")
    assert sas.backend == "SasBlobStore"

    assert type(server.build_store(server.Config({}))).__name__ == \
        "MemoryStore"


def test_the_built_package_starts_on_its_own(tmp_path):
    """The package build.py assembles, started the way the host starts it:
    `python server.py` in a directory with nothing else on sys.path. It could
    not import for a while -- evalconfig.py was not in it -- and nothing
    short of a deploy showed it."""
    import shutil
    import socket
    import subprocess
    import sys
    import time
    spec = importlib.util.spec_from_file_location(
        "replay_build", os.path.join(FUNCTION_DIR, "build.py"))
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    # Deep enough that server.py's checkout fallback (two levels up) finds
    # nothing: the package has to stand on its own.
    package = tmp_path / "a" / "b" / "wwwroot"
    package.mkdir(parents=True)
    for name in build.OWN:
        shutil.copy2(os.path.join(FUNCTION_DIR, name), package)
    for path in build.SHARED:
        shutil.copy2(path, package)
    (package / build.PAYLOAD_NAME).write_text(json.dumps({
        "schema": build.PAYLOAD_SCHEMA,
        "cassettes": {"fixture": _cassette_fixture()},
        "tool_manifests": []}), encoding="utf-8")

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {"PATH": os.environ.get("PATH", ""), "REPLAY_TOKEN": "s3cret",
           "FUNCTIONS_CUSTOMHANDLER_PORT": str(port)}
    # Windows cannot open a socket without SYSTEMROOT (WinError 10106). It
    # says nothing about the package, so it is not part of what is stripped.
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    proc = subprocess.Popen([sys.executable, "-E", "-s", "server.py"],
                            cwd=package, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if proc.poll() is not None:
                break
            try:
                health = _get(base, "/", token=None)
                break
            except OSError:
                time.sleep(0.1)
        assert proc.poll() is None, proc.stdout.read()
        assert health["mode"] == "replay"
        assert "first" in json.dumps(_call(base, "iso"))
    finally:
        proc.kill()
        proc.wait()


def test_the_deployment_asks_for_no_remote_build():
    """A build that installs where nothing looks is worse than no build."""
    for script in ("deploy.sh", "deploy.ps1"):
        body = _read(os.path.join(FUNCTION_DIR, script))
        assert "--build-remote true" not in body
    requirements = _read(os.path.join(FUNCTION_DIR, "requirements.txt"))
    assert not [line for line in requirements.splitlines()
                if line.strip() and not line.startswith("#")]


# ------------------------------------------------------------ the verifier

def _verify_module():
    path = os.path.join(FUNCTION_DIR, "verify.py")
    spec = importlib.util.spec_from_file_location("replay_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = _verify_module()


@pytest.fixture
def recordings(tmp_path):
    """A local copy of what the hosted server is serving."""
    directory = tmp_path / "local"
    directory.mkdir()
    (directory / "fixture.json").write_text(json.dumps(_cassette_fixture()), encoding="utf-8")
    return str(directory)


def test_verifier_passes_against_a_faithful_server(hosted, recordings,
                                                  blob_stub, capsys):
    """With the store main() installs -- a LazyStore, `unresolved` until
    used -- not a bare one."""
    from state_store import LazyStore, open_store
    server.Handler.store = open_store(sas_url=blob_stub)
    assert isinstance(server.Handler.store, LazyStore)
    code = verify.main([hosted, "--token", "s3cret",
                        "--cassette-dir", recordings])
    assert code == 0
    out = capsys.readouterr().out
    assert "replay identically" in out
    assert "no write was performed" in out
    assert "state after replaying: SasBlobStore" in out


def test_verifier_asks_health_to_resolve_the_store(hosted, recordings,
                                                  blob_stub, monkeypatch):
    """The last GET / can reach an instance no MCP call has; without
    ?resolve=1 it reports `unresolved` and a healthy deployment fails."""
    from state_store import open_store
    server.Handler.store = open_store(sas_url=blob_stub)
    asked = []
    real = verify.Client.health

    def health(self, resolve=False):
        asked.append(resolve)
        return real(self, resolve)
    monkeypatch.setattr(verify.Client, "health", health)
    assert verify.main([hosted, "--token", "s3cret",
                        "--cassette-dir", recordings]) == 0
    assert asked[-1] is True


def test_verifier_fails_a_store_that_is_down(hosted, recordings, capsys):
    from state_store import open_store
    server.Handler.store = open_store(sas_url="http://127.0.0.1:1/x?sig=no")
    assert verify.main([hosted, "--token", "s3cret",
                        "--cassette-dir", recordings]) == 1
    out = capsys.readouterr().out
    assert "replay state (SasBlobStore, container SAS) failed" in out
    # The server's own reason, not just "Service Unavailable".
    assert "HTTP 503 (replay state unavailable" in out


def test_verifier_reports_a_503_mid_check_instead_of_crashing(
        hosted, recordings, blob_stub, monkeypatch, capsys):
    from state_store import open_store
    server.Handler.store = open_store(sas_url=blob_stub)

    def refused(*a):
        raise urllib.error.HTTPError("http://x", 503, "Service Unavailable",
                                     {}, None)
    monkeypatch.setattr(verify, "check_isolation", refused)
    assert verify.main([hosted, "--token", "s3cret",
                        "--cassette-dir", recordings]) == 1
    out = capsys.readouterr().out
    assert "refused: HTTP 503" in out
    assert "state after replaying" in out           # carried on to the end


def test_redeploy_advice_keeps_the_mode_and_works_from_the_repo_root(
        monkeypatch):
    monkeypatch.setattr(verify.os, "name", "posix")
    same, move = verify.redeploy_commands("container SAS")
    assert same == ("REPLAY_STORAGE_AUTH=connectionString "
                    "./functions/replay-mcp/deploy.sh <rg>")
    assert "REPLAY_STORAGE_AUTH=identity" in move
    assert verify.redeploy_commands("managed identity") == \
        ("./functions/replay-mcp/deploy.sh <rg>", None)
    monkeypatch.setattr(verify.os, "name", "nt")
    same, _ = verify.redeploy_commands("container SAS")
    assert same == (".\\functions\\replay-mcp\\deploy.ps1 -ResourceGroup "
                    "<rg> -StorageAuth connectionString")


@pytest.mark.parametrize("health", [
    {}, {"state": None}, {"state": "unresolved"}, {"state": "Mystery"},
    {"state": "MemoryStore"},
    {"state": "unavailable", "state_detail": {"error": "boom"}},
])
def test_only_a_durable_store_passes(health):
    assert verify.check_state(health)


def test_verifier_fails_an_expired_sas_end_to_end(hosted, recordings,
                                                  blob_stub, capsys):
    """Through the server's own /health, not a hand-built dict."""
    import datetime as dt
    from state_store import open_store

    def at(days):
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")

    server.Handler.store = open_store(sas_url=f"{blob_stub}&se={at(-2)}")
    assert verify.main([hosted, "--token", "s3cret",
                        "--cassette-dir", recordings]) == 1
    assert "state SAS expired" in capsys.readouterr().out

    server.Handler.store = open_store(sas_url=f"{blob_stub}&se={at(10)}")
    assert verify.main([hosted, "--token", "s3cret",
                        "--cassette-dir", recordings]) == 0
    assert "expires in" in capsys.readouterr().out


def test_verifier_fails_in_process_state(hosted, recordings, capsys):
    """It used to print a NOTE and pass. In-process state keeps a replay's
    order only while one instance serves it -- the 50-call replay that
    journalled 3."""
    server.Handler.store = MemoryStore()
    assert verify.main([hosted, "--token", "s3cret",
                        "--cassette-dir", recordings]) == 1
    assert "replay state is in-process" in capsys.readouterr().out


def test_verifier_fails_an_expired_sas_and_warns_before_it_does(capsys):
    import datetime as dt
    now = dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc)
    sas = lambda when: {"state": "SasBlobStore",               # noqa: E731
                        "state_detail": {"auth": "container SAS",
                                         "expires": when}}
    assert verify.check_state(sas("2026-09-01T00:00:00Z"), now)
    assert verify.check_state(sas("2026-10-03T00:00:00Z"), now) == []
    assert "expires in 10 day(s)" in capsys.readouterr().out
    assert verify.check_state(sas("2027-09-01T00:00:00Z"), now) == []
    assert verify.check_state(sas("2026-09-01"), now)          # date only
    assert verify.check_state(sas("garbage"), now) == []       # warned
    assert "cannot read the state SAS expiry" in capsys.readouterr().out


def test_verify_and_the_store_agree_on_what_is_durable():
    import state_store
    assert tuple(verify.DURABLE) == tuple(state_store.DURABLE)


def test_verifier_fails_when_a_result_differs(hosted, recordings, capsys):
    """A check that cannot fail is not a check."""
    path = os.path.join(recordings, "fixture.json")
    with open(path, encoding="utf-8") as fh:
        bent = json.load(fh)
    bent["interactions"][1]["result"] = "not what the recording said"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bent, fh)

    assert verify.main([hosted, "--token", "s3cret",
                        "--cassette-dir", recordings]) == 1
    assert "came back different" in capsys.readouterr().out


def _fan_out_cassette():
    def interaction(seq, n):
        return {"seq": seq, "agent": "a", "tool": "cw_get_ticket",
                "key": f'cw_get_ticket|{{"ticket_number":{n}}}',
                "arguments": {"ticket_number": n}, "result": f"ticket {n}",
                "success": True, "is_write": False, "truncated": False,
                "error_kind": "", "duration_ms": 1}
    data = _cassette_fixture()
    data["orchestration_id"] = "op-fan"
    data["interactions"] = [interaction(i, 100 + i) for i in range(9)]
    return data


def _serve_fan_out(tmp_path, recordings):
    for d in (tmp_path / "cassettes", recordings):
        (d if hasattr(d, "joinpath") else __import__("pathlib").Path(d)) \
            .joinpath("fan.json").write_text(json.dumps(_fan_out_cassette()), encoding="utf-8")


def test_verifier_checks_a_concurrent_fan_out(hosted, recordings, blob_stub,
                                              tmp_path, capsys):
    from state_store import SasBlobStore
    server.Handler.store = SasBlobStore(blob_stub)
    _serve_fan_out(tmp_path, recordings)
    assert verify.main([hosted, "--token", "s3cret", "--cassette-dir",
                        recordings, "--cassette", "fan"]) == 0


def test_verifier_fails_a_server_that_loses_fan_out_races(
        hosted, recordings, blob_stub, tmp_path, monkeypatch, capsys):
    """A check that cannot fail is not a check: with the per-session lock and
    the retry taken away, the server is the one that used to answer 8 of 9
    concurrent calls with a 409."""
    import contextlib
    from state_store import SasBlobStore
    server.Handler.store = SasBlobStore(blob_stub)
    monkeypatch.setattr(server, "_session_lock",
                        lambda key: contextlib.nullcontext())
    monkeypatch.setattr(server, "SAVE_ATTEMPTS", 1)
    _serve_fan_out(tmp_path, recordings)
    assert verify.main([hosted, "--token", "s3cret", "--cassette-dir",
                        recordings, "--cassette", "fan"]) == 1
    assert "concurrent calls" in capsys.readouterr().out


def test_verifier_fails_on_a_cassette_the_server_was_never_given(
        hosted, recordings, blob_stub, capsys):
    """The gate replays every cassette built from the committed traces. One
    the server lacks 404s after the agent has been invoked, and reads as the
    agent's failure. Otherwise this server passes (see the faithful-server
    test above); the only difference is one extra local cassette."""
    from state_store import open_store
    server.Handler.store = open_store(sas_url=blob_stub)
    with open(os.path.join(recordings, "fixture.json"), encoding="utf-8") as fh:
        body = fh.read()
    with open(os.path.join(recordings, "2026-09-23-newtrace.json"), "w",
              encoding="utf-8") as fh:
        fh.write(body)
    assert verify.main([hosted, "--token", "s3cret",
                        "--cassette-dir", recordings]) == 1
    out = capsys.readouterr().out
    assert ("2026-09-23-newtrace: built from the committed traces but not "
            "deployed") in out
    assert "replay-deploy" in out


def test_verifier_uploads_what_it_checks_and_deletes_it(
        hosted, recordings, blob_stub, monkeypatch, capsys):
    """The gate's recordings are fetched when it runs, so none is deployed:
    --upload checks each local one as the gate will use it. The fan-out
    recording is one the server was never given."""
    from state_store import open_store
    store = server.Handler.store = open_store(sas_url=blob_stub)
    saved, real_save = [], store.save
    monkeypatch.setattr(store, "save", lambda key, state, version: (
        saved.append(key), real_save(key, state, version))[1])
    with open(os.path.join(recordings, "2026-10-01-live00000000.json"), "w",
              encoding="utf-8") as fh:
        json.dump(_fan_out_cassette(), fh)

    assert verify.main([hosted, "--token", "s3cret", "--cassette-dir",
                        recordings, "--upload"]) == 0
    out = capsys.readouterr().out
    assert "OK — 2 cassette(s) replay identically" in out
    assert "not deployed" not in out
    uploaded = [k for k in saved if k.startswith("cassette.rt-verify-")]
    assert len(uploaded) == 2
    # and the checks' sessions: a journal is keyed by a live run's arguments
    sessions = [k for k in saved if k.startswith("rt-verify-")]
    assert len(sessions) >= 8, saved
    assert all(store.load(k) == (None, None) for k in uploaded + sessions)


def test_verifier_fails_on_a_bad_token(hosted, recordings, capsys):
    assert verify.main([hosted, "--token", "wrong",
                        "--cassette-dir", recordings]) == 1
    assert "401" in capsys.readouterr().out


def test_verifier_rejects_something_that_is_not_the_replay_server(capsys):
    """The write guarantee is the server's identity, so check for it."""
    class Impostor(verify.Client):
        def health(self):
            return {"status": "ok", "cassettes": ["fixture"]}

    module_client = verify.Client
    verify.Client = Impostor
    try:
        assert verify.main(["http://example.invalid", "--token", "t"]) == 1
    finally:
        verify.Client = module_client
    assert "write guarantee" in capsys.readouterr().out


# ------------------------------------------------- the two storage-auth modes

INFRA = os.path.join(FUNCTION_DIR, "infra")


def _bicep(name):
    with open(os.path.join(INFRA, name), encoding="utf-8") as fh:
        return fh.read()


def test_only_the_role_assignment_is_conditional():
    """Everything else must stay valid in both modes.

    ARM evaluates both sides of a ternary, so a reference to a resource that
    only sometimes exists is a deployment error rather than a dead branch.
    The identity is created either way for exactly that reason.
    """
    main = _bicep("main.bicep")
    conditional = [line for line in main.splitlines()
                   if line.startswith("resource ") and " = if (" in line]
    assert len(conditional) == 1
    assert "roleAssignments" in conditional[0]


def test_the_preview_feature_flag_is_set():
    """Set because Microsoft's sample sets it, not because we proved we need it.

    Host 4.1054.250.26428 honoured the mcp-custom-handler profile with the
    flag absent -- it logged `1 functions found (Custom)`. The flag's name
    says the profile is preview, so a host that does check for it is a
    plausible future, and setting it costs nothing. Asserted so that removing
    it is a decision rather than an edit.
    """
    main = _bicep("main.bicep")
    assert "AzureWebJobsFeatureFlags" in main
    assert "EnableMcpCustomHandlerPreview" in main
    assert "mcp-custom-handler" in _read(os.path.join(FUNCTION_DIR,
                                                      "host.json"))


def test_keys_are_off_unless_the_deployment_needs_them():
    """A key nothing uses is a credential left to leak."""
    assert "allowSharedKeyAccess: !useIdentity" in _bicep("main.bicep")


def test_rbac_template_computes_the_same_assignment_name():
    """Otherwise running both makes two assignments instead of one."""
    main, rbac = _bicep("main.bicep"), _bicep("rbac.bicep")
    same = "guid(storage.id, identity.id, blobDataOwner)"
    assert same in main and same in rbac
    assert "b7e6dc6d-f1e8-4753-8033-0f276bb0955b" in main
    assert "b7e6dc6d-f1e8-4753-8033-0f276bb0955b" in rbac


def _branch(main, which):
    """The app settings Bicep emits under storageAuth=identity (0) or
    connectionString (1)."""
    tail = main.split("useIdentity ? [", 1)[1]
    identity, rest = tail.split("] : [", 1)
    return (identity, rest.split("])", 1)[0])[which]


def test_identity_mode_keeps_state_as_the_identity():
    """The Bicep default. It used to set only an account URL, which reached
    an SDK store that cannot import in a custom handler and fell back to
    in-process state without a word."""
    identity = _branch(_bicep("main.bicep"), 0)
    assert "REPLAY_STATE_ACCOUNT" in identity and "AZURE_CLIENT_ID" in identity
    assert "REPLAY_STATE_SAS" not in identity


def test_connection_string_mode_keeps_state_with_a_sas_and_nothing_else():
    """No connection string for the state store: the SAS is narrower, and a
    second copy of the account key in app settings bought nothing."""
    main = _bicep("main.bicep")
    assert "REPLAY_STATE_SAS" in _branch(main, 1)
    for text in (main, _read(os.path.join(FUNCTION_DIR, "server.py")),
                 _read(os.path.join(FUNCTION_DIR, "diagnose.py"))):
        assert "REPLAY_STATE_CONNECTION" not in text


def test_state_store_picks_a_backend_from_what_it_is_given():
    from state_store import open_store
    assert type(open_store()).__name__ == "MemoryStore"
    # A container with no way to reach it is not a half-configured BlobStore.
    assert type(open_store(container="c")).__name__ == "MemoryStore"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_verify_fails_a_server_that_advertises_the_agents_own_tools():
    """What a deployment from before the fix looks like: redeploy it."""
    class Stale:
        def initialize(self, cassette):
            return {}, "s1"

        def tools(self, cassette, session):
            return [{"name": "cw_get_ticket", "inputSchema": {}},
                    {"name": "load_skill", "inputSchema": {}}]

        def call(self, *args):
            return {"content": [{"text": "x"}]}

        def summary(self, cassette, session):
            return {"replayed_calls": 1, "diverged": 0}

    recording = {"interactions": [{"seq": 0, "tool": "cw_get_ticket",
                                   "arguments": {}, "result": "x",
                                   "is_write": False}]}
    _report, problems = verify.replay(Stale(), "c", recording)
    assert any("load_skill" in p and "redeploy" in p for p in problems), problems
    assert verify.advertised_local_tools(
        [{"name": "cw_get_ticket"}], local=["load_skill"]) == []


def test_verify_v_reports_lengths_and_offset_not_the_responses():
    """-v is how a failing deploy is rerun, in a public job log. Both sides
    of a mismatch are a recorded ConnectWise response."""
    recorded = 'CANARY-R-7f3a {"id": 805392}'
    got = 'CANARY-R-7f3a {"id": 805393, "extra": 1}'

    class Differs:
        def initialize(self, cassette):
            return {}, "s1"

        def tools(self, cassette, session):
            return [{"name": "cw_get_ticket", "inputSchema": {}}]

        def call(self, *args):
            return {"content": [{"text": got}]}

        def summary(self, cassette, session):
            return {"replayed_calls": 1, "diverged": 0}

    recording = {"interactions": [{"seq": 4, "tool": "cw_get_ticket",
                                   "arguments": {}, "result": recorded,
                                   "is_write": False}]}
    _report, problems = verify.replay(Differs(), "c", recording, verbose=True)
    text = "\n".join(problems)
    assert "CANARY-R-7f3a" not in text and "805392" not in text
    offset = recorded.index("2}")
    assert (f"seq 4 cw_get_ticket: expected {len(recorded)} chars, got "
            f"{len(got)}; first difference at char {offset}") in text
    assert verify._first_diff("abc", "abcd") == 3
    assert verify._first_diff("abc", "abc") == 3
