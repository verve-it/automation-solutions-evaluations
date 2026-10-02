"""run_replay.main() end to end, against a fake Foundry that records every call.

The replay's safety rests on WHICH agent each call names. A clone created,
invoked or deleted under the agent under test is what that agent's production
callers reach by name while it exists -- writes answered from a recording and
silently not performed. A review found every one of these call sites could be
pointed back at the production agent with the suite green, because nothing
drove main() past --dry-run. This does.
"""
import io
import json
import zipfile
from types import SimpleNamespace as NS

import pytest

import run_replay as rr

OPS = "connectwise-operations-agent"
REPLAY = OPS + "-replay"
LABEL = "ConnectWise-PSA-ForAgents"


def _zip(source):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("main.py", source)
    return buf.getvalue()


# What every hosted agent here reads (--inspect-code), and nothing else.
CODE = _zip(f'import os\nn = os.environ["{rr.TOOLBOX_NAME_VAR}"]\n'
            f'v = os.getenv("{rr.TOOLBOX_VERSION_VAR}")\n')


class NotFound(Exception):
    status_code = 404


class FakeProject:
    """Records every SDK call. `routes_to_clone` decides what the replay
    agent's name resolves to after the clone is created."""

    def __init__(self, env=None, routes_to_clone=True, replay_readable=True,
                 others=("triage-analysis-agent",), replay_exists=True,
                 code=CODE):
        self.calls = []
        self.code = code
        self.env = env or {"AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt",
                           rr.TOOLBOX_NAME_VAR: "ConnectwiseMCP",
                           rr.TOOLBOX_VERSION_VAR: "1"}
        self.routes_to_clone = routes_to_clone
        self.replay_readable = replay_readable
        self.replay_exists = replay_exists
        self.names = [OPS, REPLAY, *others]
        self.replay_latest = "7"
        self.agents = self
        self.toolboxes = NS(create_version=self._toolbox,
                            delete=lambda name: self.calls.append(
                                ("toolbox.delete", name)))

    # --- client
    def __call__(self, endpoint, credential, allow_preview):
        self.calls.append(("client", endpoint))
        return self

    def get_openai_client(self, agent_name):
        self.calls.append(("invoke", agent_name))
        return NS(responses=NS(create=lambda **kw: NS(id="resp_1")))

    # --- agents
    def get(self, name):
        self.calls.append(("get", name))
        if name == OPS:
            definition = {"kind": "hosted",
                          "environment_variables": dict(self.env),
                          "code_configuration": {"entry_point": "main.py"}}
            base = NS(version="29", definition=NS(as_dict=lambda: definition))
            return NS(versions=NS(latest=base), agent_endpoint=None)
        if name == REPLAY and not self.replay_exists:
            raise NotFound(f"no agent {name}")
        if name == REPLAY and self.replay_readable:
            return NS(versions=NS(latest=NS(version=self.replay_latest)),
                      agent_endpoint=NS(version_selector=None))
        raise RuntimeError(f"no agent {name}")

    def list(self):
        return [NS(name=n) for n in self.names]

    def download_code(self, name, agent_version):
        self.calls.append(("download_code", name, agent_version))
        return [self.code]

    def create_version_from_code(self, agent_name, definition, code,
                                 description=None, metadata=None):
        self.calls.append(("create", agent_name, definition))
        self.code_name = getattr(code, "name", None)
        self.replay_exists = True      # a first version creates the agent
        if self.routes_to_clone:
            self.replay_latest = "8"
        return NS(version="8")

    not_ready = 0      # create_session refusals before the version is ready
    session_error = None
    build_failures = 0  # versions whose provisioning fails in Foundry

    def create_session(self, agent_name, version_indicator):
        self.calls.append(("session", agent_name,
                           version_indicator.agent_version))
        if self.session_error:
            raise self.session_error
        if self.build_failures:
            self.build_failures -= 1
            from azure.core.exceptions import HttpResponseError
            raise HttpResponseError(message=(
                "(agent_version_failed) Agent version provisioning failed: "
                "[CodeError] in /__w/1/s/Oryx/src/BuildScriptGenerator/"
                "Python/PythonPlatform.cs:line 794 [Request ID: "
                "efc8e91097d355f9d0f8e2948291c86b]"))
        if self.not_ready:
            self.not_ready -= 1
            from azure.core.exceptions import HttpResponseError
            raise HttpResponseError(message=(
                "(agent_version_not_ready) Agent version is still being "
                "provisioned. Please try again after some time."))
        return NS(agent_session_id="sess_1")

    def stop_session(self, name, session_id):
        self.calls.append(("stop_session", name))

    def delete_version(self, name, version, force=None):
        self.calls.append(("delete_version", name, version))
        assert force, "a hosted version with a live session needs force"

    def delete(self, name, force=None):
        self.calls.append(("delete", name))
        assert force

    def _toolbox(self, name, tools, description=None, metadata=None):
        self.calls.append(("toolbox.create", name, tools))
        return NS(name=name, version="1")

    def named(self, kind):
        return [c for c in self.calls if c[0] == kind]


@pytest.fixture
def cassette(tmp_path):
    path = tmp_path / "2026-09-15-rec.json"
    path.write_text(json.dumps({
        "orchestration_id": "rec" + "0" * 29, "agents": [OPS], "query": "q",
        "interactions": [{"tool": f"{LABEL}___cw_query"},
                         {"tool": "load_skill"}]}), encoding="utf-8")
    return str(path)


@pytest.fixture
def run(monkeypatch, tmp_path, cassette):
    import azure.ai.projects
    import azure.identity

    def _run(project, *extra, summary=None):
        monkeypatch.setattr(azure.ai.projects, "AIProjectClient", project)
        monkeypatch.setattr(azure.identity, "DefaultAzureCredential",
                            lambda: None)
        monkeypatch.setattr(rr, "read_journal", lambda base, token, session: {
            "cassette": "rec", "session": session, "session_honoured": True,
            "replayed_calls": 1, "matched_prefix": 1,
            "recorded_interactions": 2, "writes_attempted": 0,
            "journal": [{"tool": "cw_query", "outcome": "matched"}],
            **(summary or {})})
        return rr.main(["--cassette", cassette, "--server-url",
                        "https://replay.example.net/mcp/rec", "--token", "t",
                        "--project-endpoint", "https://project.example.net",
                        "--journal", str(tmp_path / "journal.json"),
                        "--manifest", str(tmp_path / "manifest.json"), *extra])
    return _run


def test_every_call_names_the_replay_agent_never_the_agent_under_test(run):
    project = FakeProject()
    assert run(project) == 0
    names = {kind: [c[1] for c in project.named(kind)]
             for kind in ("create", "session", "invoke", "stop_session",
                          "delete_version")}
    assert names == {k: [REPLAY] for k in names}, names
    # the code comes from the agent under test, byte for byte (read once to
    # check it names no other agent, once to upload)
    assert set(project.named("download_code")) == {("download_code", OPS, "29")}


def test_the_name_is_checked_to_resolve_to_the_clone_before_invoking(run):
    project = FakeProject()
    assert run(project) == 0
    order = [c[0] + ":" + str(c[1]) for c in project.calls]
    assert order.index(f"get:{REPLAY}") < order.index(f"invoke:{REPLAY}")


def test_a_name_that_does_not_resolve_to_the_clone_is_never_invoked(run):
    project = FakeProject(routes_to_clone=False)
    with pytest.raises(SystemExit) as exc:
        run(project)
    assert "resolves to v7" in str(exc.value)
    assert not project.named("invoke") and not project.named("session")
    # and nothing is left behind
    assert project.named("delete_version") == [("delete_version", REPLAY, "8")]
    assert project.named("toolbox.delete")


def test_an_unreadable_replay_agent_is_never_invoked(run):
    project = FakeProject(replay_readable=False)
    with pytest.raises(SystemExit) as exc:
        run(project)
    assert "cannot read" in str(exc.value)
    assert not project.named("invoke")


def test_the_replay_tool_carries_the_recordings_server_label(run):
    """Foundry names MCP tools `<server_label>___<tool>`; the old default
    renamed every tool the agent's skills mention."""
    project = FakeProject()
    assert run(project) == 0
    (_kind, _name, tools), = project.named("toolbox.create")
    assert tools[0].server_label == LABEL


def test_the_clone_names_the_replay_toolbox(run):
    project = FakeProject()
    assert run(project) == 0
    (_kind, _name, definition), = project.named("create")
    toolbox_name = project.named("toolbox.create")[0][1]
    env = definition["environment_variables"]
    assert env[rr.TOOLBOX_NAME_VAR] == toolbox_name
    assert env["AZURE_AI_MODEL_DEPLOYMENT_NAME"] == "gpt"   # the rest untouched


def test_the_manifest_carries_what_attribution_checks(run, tmp_path):
    assert run(FakeProject()) == 0
    m = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert m["agent"] == OPS and m["replay_agent"] == REPLAY
    assert m["temp_version"] == "8" and m["replay_toolbox"][1] == "1"
    assert m["server_label"] == LABEL
    assert m["journal_tools"] == {"cw_query": 1}
    # the recording's own unprefixed calls plus the project's configured ones;
    # never an MCP tool
    assert "load_skill" in m["local_tools"]
    assert set(rr.evalconfig.local_tools(rr.CONFIG)) <= set(m["local_tools"])
    assert "cw_query" not in m["local_tools"]
    assert m["recorded_orchestration_id"].startswith("rec")


def test_an_agent_that_names_another_agent_is_refused_before_anything_exists(run):
    """The orchestrator reaches triage-analysis-agent by the name in its
    environment: that agent's production version, its live toolbox."""
    env = {rr.TOOLBOX_NAME_VAR: "ConnectwiseMCP", rr.TOOLBOX_VERSION_VAR: "1",
           "TRIAGE_ANALYSIS_AGENT_NAME": "triage-analysis-agent"}
    project = FakeProject(env=env)
    with pytest.raises(SystemExit) as exc:
        run(project)
    assert "TRIAGE_ANALYSIS_AGENT_NAME=triage-analysis-agent" in str(exc.value)
    assert not project.named("create") and not project.named("toolbox.create")


@pytest.mark.parametrize("code,said", [
    (_zip('import os\nchild = os.getenv("TRIAGE_ANALYSIS_AGENT_NAME", '
          '"triage-analysis-agent")\n'), "reads TRIAGE_ANALYSIS_AGENT_NAME"),
    (b"PK\x03\x04 not a zip", "cannot read")])
def test_code_that_names_another_agent_is_refused_before_anything_exists(
        run, code, said):
    """The orchestrator's environment names no child -- the names are
    defaults in its code -- and a live run whose hand-offs failed before the
    children emitted a span is a single-agent recording. That replayed: the
    clone invoked, its children ran against the live toolbox. Code that
    cannot be read is refused too: unknown is not none."""
    project = FakeProject(code=code)
    with pytest.raises(SystemExit) as exc:
        run(project)
    assert said in str(exc.value) and "Nothing was created" in str(exc.value)
    assert not project.named("create") and not project.named("toolbox.create")


def test_another_agent_cannot_be_replayed_on_a_cassette(run):
    """Setting the agent under test to the orchestrator on a single-agent
    cassette would run its children live. Refused before a client exists."""
    project = FakeProject()
    with pytest.raises(SystemExit) as exc:
        run(project, "--agent", "triage-orchestrator")
    assert "cannot replay triage-orchestrator" in str(exc.value)
    assert not project.calls


def test_the_replay_agent_can_never_be_the_agent_under_test(run):
    project = FakeProject()
    with pytest.raises(SystemExit):
        run(project, "--replay-agent", OPS)
    assert not project.calls


def test_the_code_is_uploaded_as_a_named_zip(run):
    """The SDK: the stream "must expose a name attribute ... and that name
    must end with .zip". Unnamed, the multipart part is just `code`."""
    project = FakeProject()
    assert run(project) == 0
    assert project.code_name.endswith(".zip")


def test_a_failed_version_delete_still_removes_the_toolbox(run):
    """The toolbox carries the replay token in its headers."""
    project = FakeProject()

    def refuses(name, version):
        project.calls.append(("delete_version", name, version))
        raise RuntimeError("409 conflict")
    project.delete_version = refuses
    assert run(project) == 0
    assert project.named("toolbox.delete")


def test_an_unlistable_project_refuses_before_anything_exists(run):
    """The environment scan needs the project's agent names. Unable to list
    them, it cannot tell a child's name from any other value -- so it
    refuses, rather than treating 'unknown' as 'none'."""
    project = FakeProject()

    def cannot():
        raise RuntimeError("403")
    project.list = cannot
    with pytest.raises(SystemExit) as exc:
        run(project)
    assert "cannot list the project's agents" in str(exc.value)
    assert not project.named("create") and not project.named("toolbox.create")


def test_the_replay_agent_is_made_for_the_run_and_deleted_after_it(run):
    """Nothing to set up per agent: a first version creates <agent>-replay,
    and a run that made it deletes the whole agent, so nothing is left for
    anything to call by name."""
    project = FakeProject(replay_exists=False)
    assert run(project) == 0
    assert project.named("create")[0][1] == REPLAY
    assert project.named("delete") == [("delete", REPLAY)]
    assert not project.named("delete_version")


def test_a_replay_agent_that_already_exists_keeps_its_other_versions(run):
    project = FakeProject(replay_exists=True)
    assert run(project) == 0
    assert project.named("delete_version") == [("delete_version", REPLAY, "8")]
    assert not project.named("delete")


def test_a_replay_agent_that_cannot_be_read_stops_before_anything_exists(run):
    project = FakeProject(replay_readable=False)
    with pytest.raises(SystemExit) as exc:
        run(project)
    assert "cannot read" in str(exc.value)
    assert not project.named("toolbox.create")
    assert not project.named("create")


def test_a_version_still_being_provisioned_is_waited_for(run, monkeypatch):
    """The first staging gate run failed here: create_session straight after
    create_version_from_code -> agent_version_not_ready."""
    slept = []
    monkeypatch.setattr(rr, "_sleep", slept.append)
    project = FakeProject()
    project.not_ready = 3
    assert run(project) == 0
    assert len(project.named("session")) == 4 and len(slept) == 3
    assert project.named("delete") or project.named("delete_version")


def test_waiting_for_provisioning_gives_up_and_still_cleans_up(run, monkeypatch):
    monkeypatch.setattr(rr, "_sleep", lambda s: None)
    monkeypatch.setattr(rr, "READY_TIMEOUT_S", 30)
    project = FakeProject()
    project.not_ready = 99
    with pytest.raises(Exception, match="agent_version_not_ready"):
        run(project)
    assert project.named("delete") or project.named("delete_version")
    assert project.named("toolbox.delete")
    assert len(project.named("session")) == 4      # 0, 10, 20, 30 s


def test_any_other_session_error_is_not_retried(run, monkeypatch):
    monkeypatch.setattr(rr, "_sleep", lambda s: pytest.fail("retried"))
    project = FakeProject()
    project.session_error = RuntimeError("403 Forbidden")
    with pytest.raises(RuntimeError, match="403"):
        run(project)
    assert len(project.named("session")) == 1


def test_a_failed_foundry_build_is_rebuilt_once(run, tmp_path, capsys):
    """2026-10-02: one clone failed in Oryx's ResolveVersions while five
    clones of the same bytes built in the same run."""
    project = FakeProject(replay_exists=False)
    project.build_failures = 1
    assert run(project) == 0
    assert len(project.named("create")) == 2
    assert len(project.named("session")) == 2
    assert "efc8e91097d355f9d0f8e2948291c86b" in capsys.readouterr().out
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["temp_version"] == "8"
    assert ("delete_version", REPLAY, "8") in project.calls   # the failed one
    assert project.named("delete") and project.named("toolbox.delete")


def test_a_build_that_fails_twice_is_not_retried_again(run):
    project = FakeProject(replay_exists=False)
    project.build_failures = 2
    with pytest.raises(Exception, match="agent_version_failed"):
        run(project)
    assert len(project.named("create")) == 2
    assert project.named("delete") and project.named("toolbox.delete")


def test_a_rebuild_beside_other_versions_deletes_the_failed_one(run):
    project = FakeProject()           # the replay agent already exists
    project.build_failures = 1
    assert run(project) == 0
    assert ("delete_version", REPLAY, "8") in project.calls


# --- no recorded content in the job log or the manifest ---------------------

QUERY_CANARY = "CANARY-Q-7f3a"
ERROR_CANARY = "CANARY-E-7f3a"


class FailingInvoke(FakeProject):
    """The service quotes the input back in its error message."""

    def get_openai_client(self, agent_name):
        self.calls.append(("invoke", agent_name))

        def create(**kw):
            raise Exception(f"(invalid_request) bad input {ERROR_CANARY} "
                            "[Request ID: 0123456789abcdef0123456789abcdef]")
        return NS(responses=NS(create=create))


def test_the_query_is_printed_as_its_length(run, capsys):
    assert run(FakeProject(), "--query", QUERY_CANARY) == 0
    out = capsys.readouterr()
    assert QUERY_CANARY not in out.out + out.err
    assert f"query: {len(QUERY_CANARY)} chars" in out.out


def test_an_invoke_error_prints_class_status_and_request_id_only(run, capsys):
    run(FailingInvoke(), "--query", QUERY_CANARY)
    out = capsys.readouterr()
    text = out.out + out.err
    assert ERROR_CANARY not in text and QUERY_CANARY not in text
    assert "invoke failed: Exception, no status, request id " \
           "0123456789abcdef0123456789abcdef" in text


def test_an_openai_error_prints_its_code_and_request_id_attribute():
    """The openai client keeps the id in exc.request_id, not the message, so
    reading only the message printed "no request id" for every invoke
    failure, and dropped the code that says whether it was a schema
    complaint."""
    exc = Exception(f"Error code: 400 - {ERROR_CANARY}")
    exc.status_code, exc.request_id, exc.code = \
        400, "req_0123abcd", "unknown_parameter"
    text = rr._public_error(exc)
    assert ERROR_CANARY not in text
    assert text == ("Exception, status 400, code unknown_parameter, "
                    "request id req_0123abcd")


def test_an_error_attribute_that_is_not_an_identifier_is_not_printed():
    exc = Exception("x")
    exc.code, exc.request_id = f"bad {ERROR_CANARY}", f"{ERROR_CANARY} x"
    text = rr._public_error(exc)
    assert ERROR_CANARY not in text
    assert text == "Exception, no status, no request id"


def test_the_dry_run_prints_the_querys_length_and_source(run, capsys):
    assert run(FakeProject(), "--query", QUERY_CANARY, "--dry-run") == 0
    out = capsys.readouterr().out
    assert QUERY_CANARY not in out
    assert '"chars": 13' in out and '"source": "--query"' in out


def test_the_manifest_keeps_where_it_diverged_not_what_was_sent(run, tmp_path):
    canary = "CANARY-K-7f3a"
    assert run(FakeProject(), summary={"first_divergence": {
        "seq": 3, "tool": "cw_query", "outcome": "diverged",
        "reason": "not recorded", "key": f'cw_query {{"q": "{canary}"}}',
        "arguments": {"q": canary}}}) == 0
    text = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    assert canary not in text
    assert json.loads(text)["first_divergence"] == {
        "seq": 3, "tool": "cw_query", "outcome": "diverged",
        "reason": "not recorded"}


def test_the_manifest_still_carries_what_attribution_reads(run, tmp_path):
    assert run(FakeProject(), summary={"first_divergence": None}) == 0
    m = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    for key in ("journal_tools", "diverged_tools", "local_tools",
                "replay_session", "temp_version", "replayed_calls"):
        assert key in m, key
    assert m["first_divergence"] is None


# --- a recording uploaded for this run ----------------------------------------

UPLOAD = ("--upload", "--server-url", "https://replay.example.net")


def _fake_server(monkeypatch, project, put=201):
    """The hosted server's /cassettes/<id>, recorded into the project's calls
    so their order against the SDK's is visible."""
    def http(method, url, token, body=None, session=None):
        project.calls.append((method, url, session, body))
        return put if method == "PUT" else 204
    monkeypatch.setattr(rr, "_cassette_http", http)


def test_the_recording_goes_up_before_anything_is_created(run, monkeypatch,
                                                          tmp_path, cassette):
    project = FakeProject()
    _fake_server(monkeypatch, project)
    assert run(project, *UPLOAD) == 0
    kinds = [c[0] for c in project.calls]
    assert kinds.index("PUT") < kinds.index("toolbox.create") \
        < kinds.index("create")
    (_put, url, _session, body), = project.named("PUT")
    cassette_id = url.rsplit("/", 1)[-1]
    assert url == f"https://replay.example.net/cassettes/{cassette_id}"
    assert cassette_id.startswith("rt-")
    with open(cassette, "rb") as fh:
        assert body == fh.read()
    (_kind, _name, tools), = project.named("toolbox.create")
    assert tools[0].server_url == \
        f"https://replay.example.net/mcp/{cassette_id}"
    (_delete, deleted, session, _body), = project.named("DELETE")
    assert deleted == url and session
    assert kinds.index("DELETE") > kinds.index("toolbox.delete")
    manifest = json.loads((tmp_path / "manifest.json").read_text(
        encoding="utf-8"))
    assert manifest["cassette_id"] == cassette_id


def test_the_upload_is_deleted_when_the_invoke_raises(run, monkeypatch):
    project = FakeProject()
    _fake_server(monkeypatch, project)

    def raises(*a):
        raise RuntimeError("invoke blew up")
    monkeypatch.setattr(rr, "invoke_agent", raises)
    with pytest.raises(RuntimeError):
        run(project, *UPLOAD)
    assert project.named("DELETE")


@pytest.mark.parametrize("status", [503, 409, "URLError"])
def test_a_failed_upload_creates_nothing(run, monkeypatch, status):
    project = FakeProject()
    _fake_server(monkeypatch, project, put=status)
    with pytest.raises(SystemExit) as exc:
        run(project, *UPLOAD)
    assert f"could not upload the cassette to the replay server: {status}" \
        in str(exc.value)
    assert not project.named("toolbox.create") and not project.named("create")
