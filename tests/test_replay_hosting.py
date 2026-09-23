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
    env = {"REPLAY_CASSETTE_DIR": str(tmp_path),
           "REPLAY_PAYLOAD": str(tmp_path / "absent.json")}
    config = server.Config(dict(env, REPLAY_CASSETTE="from-setting"))
    source = server.Source(config)
    assert server.resolve_cassette_id(config, source, "from-url") == "from-url"
    assert server.resolve_cassette_id(config, source, None) == "from-setting"

    bare = server.Config(dict(env))
    assert server.resolve_cassette_id(bare, server.Source(bare), None) == "only"

    (tmp_path / "other.json").write_text("{}")
    ambiguous = server.Config(dict(env))
    assert server.resolve_cassette_id(
        ambiguous, server.Source(ambiguous), None) is None


def test_payload_is_preferred_over_directories(tmp_path):
    """Deployment is flat; a checkout is not. One reader, both shapes."""
    directory = tmp_path / "cassettes"
    directory.mkdir()
    (directory / "from-disk.json").write_text(json.dumps(_cassette_fixture()))

    config = server.Config({"REPLAY_CASSETTE_DIR": str(directory),
                            "REPLAY_PAYLOAD": str(tmp_path / "absent.json")})
    assert server.Source(config).cassette_ids() == ["from-disk"]

    payload = tmp_path / "replay_payload.json"
    payload.write_text(json.dumps({
        "schema": "verve/replay-payload@1",
        "cassettes": {"from-payload": _cassette_fixture()},
        "tool_manifests": [{"tools": []}]}))
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
    body = _read(state_store.__file__).split("class SasBlobStore")[1] \
        .split("class BlobStore")[0]
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


def test_a_sas_that_does_not_answer_degrades_rather_than_kills(capsys):
    from state_store import open_store
    store = open_store(sas_url="http://127.0.0.1:1/x?sig=nope")
    store.load("k")                       # resolves on first use
    assert store.backend == "MemoryStore"
    assert "could not use blob storage" in capsys.readouterr().out


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
            .joinpath("fan.json").write_text(json.dumps(_fan_out_cassette()))


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
