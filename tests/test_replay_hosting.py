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
from state_store import Conflict, MemoryStore
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
    (tmp_path / "only.json").write_text("{}")
    config = server.Config({"REPLAY_CASSETTE_DIR": str(tmp_path),
                            "REPLAY_CASSETTE": "from-setting"})
    assert server.resolve_cassette_id(config, "from-url") == "from-url"
    assert server.resolve_cassette_id(config, None) == "from-setting"

    bare = server.Config({"REPLAY_CASSETTE_DIR": str(tmp_path)})
    assert server.resolve_cassette_id(bare, None) == "only"

    (tmp_path / "other.json").write_text("{}")
    ambiguous = server.Config({"REPLAY_CASSETTE_DIR": str(tmp_path)})
    assert server.resolve_cassette_id(ambiguous, None) is None


# ------------------------------------------------------------------ the server

@pytest.fixture
def hosted(tmp_path):
    """The function's own server, on a port, with a token."""
    from http.server import ThreadingHTTPServer

    cassette_dir = tmp_path / "cassettes"
    cassette_dir.mkdir()
    (cassette_dir / "fixture.json").write_text(json.dumps(_cassette_fixture()))

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
    (directory / "fixture.json").write_text(json.dumps(_cassette_fixture()))
    return str(directory)


def test_verifier_passes_against_a_faithful_server(hosted, recordings, capsys):
    code = verify.main([hosted, "--token", "s3cret",
                        "--cassette-dir", recordings])
    assert code == 0
    out = capsys.readouterr().out
    assert "replay identically" in out
    assert "no write was performed" in out


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


def test_connection_string_mode_reaches_the_state_store():
    """The fallback is only useful if state still lands in blob storage."""
    main = _bicep("main.bicep")
    assert "REPLAY_STATE_CONNECTION" in main
    assert "REPLAY_STATE_CONNECTION" in _read(os.path.join(FUNCTION_DIR,
                                                           "server.py"))


def test_state_store_picks_a_backend_from_what_it_is_given():
    from state_store import open_store
    assert type(open_store()).__name__ == "MemoryStore"
    # A container with no way to reach it is not a half-configured BlobStore.
    assert type(open_store(container="c")).__name__ == "MemoryStore"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()
