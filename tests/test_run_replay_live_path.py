"""run_replay.main() end to end, against a fake Foundry that records every call.

The replay's safety rests on WHICH agent each call names. A clone created,
invoked or deleted under the agent under test is what that agent's production
callers reach by name while it exists -- writes answered from a recording and
silently not performed. A review found every one of these call sites could be
pointed back at the production agent with the suite green, because nothing
drove main() past --dry-run. This does.
"""
import json
from types import SimpleNamespace as NS

import pytest

import run_replay as rr

OPS = "connectwise-operations-agent"
REPLAY = OPS + "-replay"
LABEL = "ConnectWise-PSA-ForAgents"


class NotFound(Exception):
    status_code = 404


class FakeProject:
    """Records every SDK call. `routes_to_clone` decides what the replay
    agent's name resolves to after the clone is created."""

    def __init__(self, env=None, routes_to_clone=True, replay_readable=True,
                 others=("triage-analysis-agent",), replay_exists=True):
        self.calls = []
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
        return [b"PK\x03\x04zip"]

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

    def _run(project, *extra):
        monkeypatch.setattr(azure.ai.projects, "AIProjectClient", project)
        monkeypatch.setattr(azure.identity, "DefaultAzureCredential",
                            lambda: None)
        monkeypatch.setattr(rr, "read_journal", lambda base, token, session: {
            "cassette": "rec", "session": session, "session_honoured": True,
            "replayed_calls": 1, "matched_prefix": 1,
            "recorded_interactions": 2, "writes_attempted": 0,
            "journal": [{"tool": "cw_query", "outcome": "matched"}]})
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
    # the code comes from the agent under test, byte for byte
    assert project.named("download_code") == [("download_code", OPS, "29")]


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
