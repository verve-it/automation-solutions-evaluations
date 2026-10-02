"""replay/preflight.py: each check fails with its message, before anything
is created, and prints nothing read from a recording."""
import io
import json
import os
import zipfile
from types import SimpleNamespace as NS

import pytest

from conftest import REPO

import make_cassette as mc
import preflight as pf
import run_replay as rr

OPS = "connectwise-operations-agent"
BASELINES = os.path.join(REPO, "baselines")
OPS_TRACE = os.path.join(REPO, "traces", "2026-09-15-ops-worst-case.json")
CANARY = "CANARYLEAK7f3aQZ"


@pytest.fixture(scope="module")
def cassettes(tmp_path_factory):
    out = tmp_path_factory.mktemp("cassettes")
    for c in mc.build(mc.load_spans(OPS_TRACE)):
        with open(out / f"2026-09-15-{c['orchestration_id'][:12]}.json", "w",
                  encoding="utf-8") as fh:
            json.dump(c, fh)
    return out


def _run(capsys, *argv):
    rc = pf.main(list(argv) + ["--offline"])
    return rc, capsys.readouterr().out


def test_the_committed_recordings_pass(cassettes, capsys):
    rc, out = _run(capsys, "--cassettes", str(cassettes),
                   "--baselines", BASELINES, "--gate-agents", OPS)
    assert rc == 0, out
    assert f"{OPS}: 2 recording(s)" in out


def test_a_recording_without_a_baseline_fails_before_the_replay(
        cassettes, tmp_path, capsys):
    rc, out = _run(capsys, "--cassettes", str(cassettes),
                   "--baselines", str(tmp_path))
    assert rc == 1
    assert out.count("no baseline row") == 2


def test_an_agent_with_no_recording_fails(cassettes, capsys):
    rc, out = _run(capsys, "--cassettes", str(cassettes), "--baselines",
                   BASELINES, "--gate-agents", f"{OPS},brand-new-agent")
    assert rc == 1
    assert "gate-agents names brand-new-agent" in out


def test_a_lossy_or_inputless_recording_fails_without_printing_it(
        cassettes, tmp_path, capsys):
    src = sorted(cassettes.glob("*.json"))[0]
    data = json.loads(src.read_text(encoding="utf-8"))
    lossy = dict(data, lossy=True, query=f"triage {CANARY}")
    (tmp_path / "lossy.json").write_text(json.dumps(lossy), encoding="utf-8")
    bare = dict(data, query=None)
    (tmp_path / "bare.json").write_text(json.dumps(bare), encoding="utf-8")
    rc, out = _run(capsys, "--cassettes", str(tmp_path),
                   "--baselines", BASELINES)
    assert rc == 1
    assert "lossy: lossy" in out and "bare: " in out
    assert CANARY not in out


def test_an_arm_id_for_the_workspace_fails(cassettes, capsys):
    rc, out = _run(capsys, "--cassettes", str(cassettes), "--baselines",
                   BASELINES, "--workspace",
                   "/subscriptions/x/resourceGroups/y/providers/"
                   "Microsoft.OperationalInsights/workspaces/z")
    assert rc == 1 and "not a GUID (an ARM resource id)" in out


def test_an_unset_judge_deployment_warns_and_does_not_fail(cassettes, capsys):
    rc, out = _run(capsys, "--cassettes", str(cassettes), "--baselines",
                   BASELINES, "--judge-deployment", "")
    assert rc == 0 and "WARN  foundry" in out


# ------------------------------------------------------------- workspace

class Forbidden(Exception):
    status_code = 403


def test_a_missing_content_role_is_named(capsys):
    def query(kql):
        if kql.startswith("AppGenAIContent"):
            raise Forbidden(f"InsufficientAccessError {CANARY}")
        return NS(tables=[NS(rows=[[1]])])
    report = pf.Report()
    pf.check_workspace(report, "ws", query=query)
    out = capsys.readouterr().out
    assert report.failed()
    assert "Privileged Monitoring Data Reader" in out
    assert "AppDependencies readable" in out
    assert CANARY not in out


def test_an_empty_workspace_warns():
    report = pf.Report()
    pf.check_workspace(report, "ws", query=lambda kql: NS(tables=[NS(rows=[])]))
    assert not report.failed()
    assert [s for s, _, _ in report.lines] == ["WARN", "WARN"]


# ---------------------------------------------------------------- agents

def _zip(source):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("main.py", source)
    return buf.getvalue()


class FakeAgents:
    def __init__(self, kind="hosted", code=None, missing=False):
        self.kind, self.code, self.missing = kind, code, missing
        self.calls = []

    def get(self, name):
        self.calls.append(("get", name))
        if self.missing:
            raise Forbidden("(not_found) agent")
        definition = {"kind": self.kind}
        return NS(versions=NS(latest=NS(
            version="4", definition=NS(as_dict=lambda: definition))))

    def download_code(self, name, agent_version):
        self.calls.append(("download_code", name, agent_version))
        return [self.code]


VARS = (rr.TOOLBOX_NAME_VAR, rr.TOOLBOX_VERSION_VAR)


def _agents(client):
    report = pf.Report()
    pf.check_agents(report, client, {OPS: ["c.json"]}, VARS)
    return report


def test_an_agent_not_in_the_project_fails_naming_azure_yaml():
    report = _agents(FakeAgents(missing=True))
    assert report.failed()
    assert "azure.yaml" in report.lines[0][2]


def test_a_workflow_agent_cannot_be_bound():
    report = _agents(FakeAgents(kind="workflow"))
    assert report.failed() and "kind=workflow" in report.lines[0][2]


def test_hosted_code_that_reads_the_toolbox_variables_passes():
    code = _zip(f'import os\nn = os.environ["{VARS[0]}"]\n'
                f'v = os.getenv("{VARS[1]}", "1")\n')
    client = FakeAgents(code=code)
    report = _agents(client)
    assert not report.failed(), report.lines
    assert all(c[0] in ("get", "download_code") for c in client.calls)


def test_hosted_code_that_ignores_them_would_keep_its_live_toolbox():
    report = _agents(FakeAgents(code=_zip(
        'import os\nurl = os.getenv("MY_OWN_TOOLBOX_URL")\n')))
    assert report.failed()
    msg = report.lines[0][2]
    assert "keep its live toolbox" in msg and "MY_OWN_TOOLBOX_URL" in msg


def test_unreadable_code_fails():
    assert _agents(FakeAgents(code=b"not a zip")).failed()
