"""The gate workflow, checked for the things that make it a gate.

A workflow cannot be run here, so this asserts the properties that decide
whether it gates anything at all. Each one has a way of being quietly wrong:
a step that reports instead of failing, a cassette that should have been
skipped, a summary that never reaches the builder.
"""
import json
import os

import pytest

from conftest import REPO

yaml = pytest.importorskip("yaml")
WORKFLOWS = os.path.join(REPO, ".github", "workflows")
GATE = os.path.join(WORKFLOWS, "agent-gate.yml")
EVALS = os.path.join(WORKFLOWS, "evals.yml")


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def workflow():
    with open(GATE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def steps(workflow):
    return workflow["jobs"]["replay"]["steps"]


def _run_text(steps):
    return "\n".join(s.get("run", "") for s in steps)


def test_it_is_callable_from_the_repository_that_deploys(workflow):
    """The agents merge elsewhere. A gate they cannot call gates nothing."""
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "workflow_call" in triggers
    assert "environment" in triggers["workflow_call"]["inputs"]
    assert triggers["workflow_call"]["secrets"]["REPLAY_TOKEN"]["required"]


def test_both_environments_are_reachable(workflow):
    """staging and prod, and the job binds to one so OIDC resolves."""
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert set(triggers["workflow_dispatch"]["inputs"]["environment"]
               ["options"]) == {"staging", "prod"}
    assert workflow["jobs"]["replay"]["environment"]["name"] == \
        "${{ inputs.environment }}"


def test_the_scores_reach_the_builder(steps):
    """A red build has to show which check failed, not just that one did."""
    assert "$GITHUB_STEP_SUMMARY" in _run_text(steps)
    assert "--summary" in _run_text(steps)


def test_the_detail_lands_in_foundry(steps):
    """Foundry -> Evaluation is where a run gets opened and read."""
    text = _run_text(steps)
    assert "run_cloud_eval.py" in text
    assert "to_foundry_dataset.py" in text

    foundry = [s for s in steps if "run_cloud_eval.py" in s.get("run", "")][0]
    # A failed gate is exactly when someone wants to open the run.
    assert "always()" in foundry.get("if", "")


def test_the_replay_server_is_verified_before_an_agent_is_pointed_at_it(steps):
    """Otherwise every call diverges and the gate blames the agent."""
    names = [s.get("name", "") for s in steps]
    verify = next(i for i, s in enumerate(steps)
                  if "verify.py" in s.get("run", ""))
    replay = next(i for i, s in enumerate(steps)
                  if "run_replay.py" in s.get("run", ""))
    assert verify < replay, names


def test_orchestrations_are_skipped_not_merely_refused(steps):
    """An orchestration's children run their production versions, which are
    still pointed at the real ConnectWise toolbox -- writes included.

    run_replay.py refuses one, but a gate that relies on a refusal to avoid
    live writes is one flag away from doing them. This skips them itself.
    """
    text = _run_text(steps)
    assert "agents" in text and "!= \"1\"" in text
    assert "skip" in text


def test_replaying_nothing_fails_the_build(steps):
    """A gate that silently gates nothing is worse than no gate."""
    text = _run_text(steps)
    assert 'ran" = "0"' in text
    assert "nothing was gated" in text


def test_a_missing_setting_fails_early_and_says_which(steps):
    text = _run_text(steps)
    for name in ("AZURE_AI_PROJECT_ENDPOINT", "REPLAY_SERVER_URL",
                 "REPLAY_TOKEN"):
        assert name in text


def test_the_replay_step_fails_the_job_on_a_nonzero_exit(steps):
    """`| tee` swallows the exit code without pipefail, so the gate would
    pass while the replay failed."""
    replay = [s for s in steps if "run_replay.py" in s.get("run", "")][0]
    assert "pipefail" in replay["run"]


def test_the_export_is_windowed_to_this_replay(steps):
    """A flat --hours sweeps up whatever else the project served.

    The gate exports traces from a project that also carries real traffic.
    Scored by the hour, another team's run lands in this agent change's
    verdict -- red for something the change did not do, or green because an
    unrelated success diluted a failure. Either way the gate stops meaning
    what it says.
    """
    run = _step(steps, "attribute")
    assert "export_traces.py" in run
    assert "--since" in run and "--until" in run
    assert "--hours" not in run
    assert "steps.window.outputs" in run


def test_the_window_refuses_to_guess(steps):
    """If no manifest carried a window, the honest move is to fail.

    Falling back to an hour is the bug this step exists to prevent, and it
    would fail open: the gate would still go green, on the wrong data.
    """
    window = [s for s in steps if s.get("id") == "window"][0]
    assert "sys.exit" in window["run"]
    assert "started_utc" in window["run"] and "finished_utc" in window["run"]


def _raw_outputs(steps):
    """Every path a step writes that holds exported trace content.

    Read from the commands themselves -- the output of export_traces.py,
    trace_to_eval.py (and its --skill-registry), attribute_runs.py's
    --out-spans/--out-runs, to_foundry_dataset.py, make_cassette.py,
    submit_to_foundry.py's --out and run_replay.py's --journal -- so a
    renamed output cannot slip past the check. A journal's keys are the
    arguments the agent under test sent, which stop being the scrubbed
    cassette's the moment an agent change diverges.
    """
    import re
    raw = []
    for s in steps:
        run = s.get("run", "")
        for cmd in re.split(r"\n(?=\s*python )", run):
            if not any(t in cmd for t in ("export_traces.py", "trace_to_eval.py",
                                          "attribute_runs.py",
                                          "to_foundry_dataset.py",
                                          "make_cassette.py",
                                          "submit_to_foundry.py",
                                          "run_replay.py")):
                continue
            for flag in ("-o", "--out", "--out-spans", "--out-runs",
                         "--skill-registry", "--journal"):
                for m in re.finditer(rf"(?:^|\s){re.escape(flag)}\s+(\S+)", cmd):
                    raw.append(m.group(1).strip('"'))
    return raw


def _upload_globs(steps):
    upload = [s for s in steps
              if str(s.get("uses", "")).startswith("actions/upload-artifact")][0]
    return [g.strip() for g in upload["with"]["path"].splitlines() if g.strip()]


@pytest.fixture(scope="module")
def drift_steps():
    return _load(EVALS)["jobs"]["drift"]["steps"]


def test_the_journals_are_raw_and_not_uploaded(steps):
    raw = _raw_outputs(steps)
    assert "out/raw/journal-$id.json" in raw, raw
    assert not any("journal" in g for g in _upload_globs(steps))


def test_the_drift_export_is_not_published(drift_steps):
    """The nightly export is the same live content as the gate's window."""
    _assert_unpublished(drift_steps, minimum=5)
    assert "artifacts/drift-*.json" in _upload_globs(drift_steps)


def test_the_raw_export_is_not_published(steps):
    """Artifacts in a public repo are downloadable by anyone.

    The exported window and the Foundry dataset are a live export of a
    project that also serves real traffic, and scrub_trace.py is deliberately
    not automatable (propose -> human review -> apply). So they stay on the
    runner.

    The first version of this test checked that the word "spans" was absent;
    `artifacts/replay-*.json` passed it and matched the raw spans file. The
    second matched globs with fnmatch against file paths, which misses what
    upload-artifact does: a pattern naming a directory uploads everything
    under it, and `./out/...` is `out/...`. So the rule is structural: raw
    output -- and each export's sidecars -- lives under out/, and every upload
    entry is a file pattern directly inside artifacts/.
    """
    _assert_unpublished(steps, minimum=6)
    assert "artifacts/gate.json" in _upload_globs(steps)


def _assert_unpublished(steps, minimum):
    import fnmatch
    raw = _raw_outputs(steps)
    assert len(raw) >= minimum, raw
    sidecars = []
    for path in raw:
        assert path.startswith("out/"), f"raw output outside out/: {path}"
        sidecars += [path + ".meta.json", path.rstrip("/") + "/runs_summary.csv",
                     path.rstrip("/") + "/eval_runs.jsonl"]
    for g in _upload_globs(steps):
        assert "${{" not in g and not g.startswith(("/", "./", "~")), g
        assert "**" not in g, g
        assert g.startswith("artifacts/") and "/" not in g[len("artifacts/"):], g
        assert "." in g.rsplit("/", 1)[-1], f"not a file pattern: {g}"
        for path in raw + sidecars:
            parts = path.split("/")
            for i in range(1, len(parts) + 1):     # the file and every parent
                assert not fnmatch.fnmatch("/".join(parts[:i]), g), (path, g)


def _step(steps, step_id):
    return [s for s in steps if s.get("id") == step_id][0]["run"]


def _flag(cmd, flag):
    import re
    m = re.search(rf"{re.escape(flag)}\s+\"?([^\s\"]+)", cmd)
    return m.group(1) if m else None


def test_the_score_step_reads_what_attribution_wrote(steps):
    attribute = _step(steps, "attribute")
    score = _step(steps, "score")
    assert _flag(attribute, "--out-runs") in score.split()
    assert _flag(attribute, "--out-baseline") == _flag(score, "--baseline")


def test_foundry_scores_only_the_replays_spans(steps):
    """Pointed at the window, the Foundry run would score every production
    run in it as though the agent change had caused it -- and nothing would
    turn red."""
    foundry = [s for s in steps if "to_foundry_dataset.py" in s.get("run", "")][0]
    out_spans = _flag(_step(steps, "attribute"), "--out-spans")
    assert f"to_foundry_dataset.py {out_spans}" in " ".join(foundry["run"].split())


def test_the_window_step_names_the_replay_agents(steps, tmp_path):
    """Executed, not grepped: an empty `agents=` exports nothing, and the gate
    goes red ten minutes later for a reason nobody can see."""
    import re
    import subprocess
    import sys
    script = _step(steps, "window")
    body = script.split("<<'EOF'\n", 1)[1].rsplit("EOF", 1)[0]
    body = "\n".join(line[10:] if line.startswith(" " * 10) else line
                      for line in body.splitlines())
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "manifest-a.json").write_text(json.dumps({
        "agent": "ops", "replay_agent": "ops-replay",
        "started_utc": "2026-09-22T10:00:00+00:00",
        "finished_utc": "2026-09-22T10:04:00+00:00"}), encoding="utf-8")
    out = tmp_path / "out.txt"
    subprocess.run([sys.executable, "-c", body], cwd=tmp_path, check=True,
                   env={"GITHUB_OUTPUT": str(out), "PATH": "/usr/bin:/bin"})
    got = dict(l.split("=", 1) for l in out.read_text(encoding="utf-8").splitlines())
    assert got["agents"] == "ops-replay"
    assert got["since"].startswith("2026-09-22T09:55")
    assert got["until"].startswith("2026-09-22T10:09")


def test_the_gate_serialises_its_replays(workflow):
    """Clones are versions of a shared replay agent, invoked by name, and a
    name resolves to the newest version."""
    job = workflow["jobs"]["replay"]
    # exactly per environment: a run id in the group would serialise nothing
    assert job["concurrency"]["group"] == "agent-gate-${{ inputs.environment }}"
    assert job["concurrency"]["cancel-in-progress"] is False
    assert job["timeout-minutes"]


# --------------------------------------------- the retry loop, executed

FAKE_PY = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
state = os.environ["STATE"]
script, args = sys.argv[1], sys.argv[2:]
def bump(name):
    p = os.path.join(state, name)
    n = int(open(p).read()) + 1 if os.path.exists(p) else 1
    open(p, "w").write(str(n))
    return n
if script == "export_traces.py":
    n = bump("export")
    # export_traces.py exits 0 WITHOUT writing -o when the window is empty
    if n >= int(os.environ.get("EXPORT_FROM", "1")):
        json.dump([], open(args[args.index("-o") + 1], "w"))
    sys.exit(0)
if script == "trace_to_eval.py":       # the real one
    sys.exit(subprocess.call([sys.executable,
                              os.path.join(os.environ["GATE_REPO"], script), *args]))
if script == "replay/attribute_runs.py":
    n = bump("attribute")
    with open(os.path.join(state, "attribute.argv"), "a") as fh:
        fh.write(json.dumps(args) + "\n")
    rcs = [int(x) for x in os.environ["RCS"].split(",")]
    sys.exit(rcs[min(n, len(rcs)) - 1])
sys.exit("unexpected python " + script)
'''


def _run_attribute_step(steps, tmp_path, rcs, export_from=1):
    """Run the step's own bash under GitHub's `bash -e`, with python and
    sleep stubbed, and report its exit code and each attribution call."""
    import re
    import stat
    import subprocess
    if os.name == "nt":
        # The step runs on the Linux runner, under its bash. On Windows a
        # symlink needs Developer Mode, and `bash` may be the WSL launcher,
        # which takes neither this PATH nor these variables.
        pytest.skip("runs the Linux runner's bash")
    script = re.sub(r"\$\{\{[^}]*\}\}", "X", _step(steps, "attribute"))
    work, state, bin_ = tmp_path / "w", tmp_path / "s", tmp_path / "bin"
    for d in (work, state, bin_):
        d.mkdir()
    os.symlink(os.path.join(REPO, "tool_manifests"), work / "tool_manifests")
    for name, body in (("python", FAKE_PY), ("sleep", "#!/bin/sh\nexit 0\n")):
        path = bin_ / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", STATE=str(state),
               GATE_REPO=REPO, RCS=",".join(map(str, rcs)),
               EXPORT_FROM=str(export_from),
               GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md"))
    p = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-c", script],
                       cwd=work, env=env, capture_output=True, text=True)
    argv = state / "attribute.argv"
    calls = [json.loads(l) for l in open(argv, encoding="utf-8")] if argv.exists() else []
    return p.returncode, calls, p.stdout + p.stderr


def test_the_loop_retries_ingestion_lag_then_passes(steps, tmp_path):
    rc, calls, out = _run_attribute_step(steps, tmp_path, [3, 3, 0])
    assert rc == 0, out
    assert len(calls) == 3


def test_the_loop_stops_at_once_on_a_hard_failure(steps, tmp_path):
    rc, calls, out = _run_attribute_step(steps, tmp_path, [1, 0])
    assert rc == 1, out
    assert len(calls) == 1, "rc 1 must stop at once, not burn ten minutes"


def test_lag_that_never_clears_fails_after_ten_with_final_last(steps, tmp_path):
    rc, calls, out = _run_attribute_step(steps, tmp_path, [3])
    assert rc == 1, out
    assert len(calls) == 10
    assert "--final" in calls[-1]
    assert all("--final" not in c for c in calls[:-1])


def test_an_empty_first_window_is_retried_not_crashed(steps, tmp_path):
    """Right after the replay nothing is ingested and export_traces.py writes
    no file. That is attempt 1 of 10, not a FileNotFoundError."""
    rc, calls, out = _run_attribute_step(steps, tmp_path, [3, 0], export_from=2)
    assert rc == 0, out
    assert len(calls) == 2


def _command(run, script):
    """One command's text, continuation lines included -- so a flag is found
    on the command it belongs to, not anywhere in the step."""
    lines, out, inside = run.splitlines(), [], False
    for line in lines:
        if script in line:
            inside = True
        if inside:
            out.append(line)
            if not line.rstrip().endswith("\\"):
                break
    return " ".join(out)


def test_attribution_is_given_the_tool_manifests(steps):
    """Without them the replayed row carries no schema, valid_tool_args stops
    scoring, and an unchanged agent fails on lost coverage."""
    cmd = _command(_step(steps, "attribute"), "attribute_runs.py")
    assert "--tool-defs tool_manifests/" in cmd


def test_discovery_asks_for_the_replay_agents(steps):
    """An empty or wrong --agents exports nothing, and the gate goes red ten
    minutes later for a reason nobody can see."""
    cmd = _command(_step(steps, "attribute"), "export_traces.py")
    assert '--agents "${{ steps.window.outputs.agents }}"' in cmd
    assert "--max-orchestrations 500" in cmd


def test_the_gate_has_no_agent_override(workflow):
    """Replaying another agent than the cassette recorded -- an orchestrator
    on a single-agent cassette -- runs its children live."""
    inputs = workflow[True]["workflow_call"]["inputs"]
    assert "agent" not in inputs
    assert "inputs.agent" not in open(GATE, encoding="utf-8").read()


def _bash(script, cwd, **env):
    """A step's own bash, under GitHub's `bash -e`, with the job's env and
    `${{ }}` stubbed."""
    import re
    import subprocess
    if os.name == "nt":
        pytest.skip("runs the Linux runner's bash")
    script = re.sub(r"\$\{\{[^}]*\}\}", "X", script)
    job = {k: str(v) for k, v in _load(GATE)["jobs"]["replay"]["env"].items()}
    return subprocess.run(["bash", "--noprofile", "--norc", "-e", "-c", script],
                          cwd=cwd, env=dict(os.environ, **job, **env),
                          capture_output=True, text=True)


@pytest.mark.parametrize("agents,ok", [("", False), (" , ", False),
                                       ("ops", True), ("ops, triage-a", True),
                                       ("../x", False), ("ops *", False),
                                       ("ops triage", False), ("-o", False),
                                       ("a,b,c,d", True), ("a,b,c,d,e", False)])
def test_gate_agents_is_required_and_only_names(steps, tmp_path, agents, ok):
    """Recordings are fetched per agent, so an empty gate-agents has nothing
    to gate; each name becomes a path under out/raw/; and preflight splits on
    commas only, so "ops triage" was two agents to the fetch and one to it.
    More agents than the job has time to replay is refused up front."""
    check = next(s for s in steps if s.get("name") == "Check the gate is configured")
    assert check["env"]["GATE_AGENTS"] == "${{ inputs.gate-agents }}"
    p = _bash(check["run"], tmp_path, REF="main", GATE_AGENTS=agents)
    assert (p.returncode == 0) is ok, p.stdout + p.stderr
    if agents.strip(" ,") == "":
        assert "::error::gate-agents is empty" in p.stdout


def test_the_gate_reads_no_committed_recording(steps):
    """Recordings and their baselines are this run's, fetched live; the
    committed traces and baselines are the eval-code frozen sets only."""
    text = _run_text(steps)
    assert "make cassettes" not in text
    assert "traces/" not in text and "--baselines baselines" not in text
    assert "--cassettes out/raw/cassettes --baselines out/raw/baselines" in text
    assert "--baselines out/raw/baselines" in _step(steps, "attribute")


def test_the_fetch_writes_only_under_out_raw(steps):
    import re
    fetch = next(s for s in steps if s.get("name") == "Fetch live recordings")
    run = fetch["run"]
    written = [m.group(1).strip('"') for m in re.finditer(
        r"(?:\s-o|--json|>)\s+(\S+)", run)]
    assert len(written) >= 5, written
    assert all(w.startswith("out/raw/") for w in written), written
    for raw in ("out/raw/recorded", "out/raw/cassettes", "out/raw/baselines",
                "out/raw/recorded-rows"):
        assert raw in run, raw
    assert "--standalone" in run and "--strict" in run
    assert fetch["env"]["WORKSPACES"] == ("${{ secrets.RECORDINGS_WORKSPACE_IDS"
                                          " || secrets.LOG_ANALYTICS_WORKSPACE_ID }}")


def test_recordings_workspaces_are_optional(workflow):
    secrets = workflow[True]["workflow_call"]["secrets"]
    assert secrets["RECORDINGS_WORKSPACE_IDS"]["required"] is False


def test_verify_and_every_replay_upload_the_recording(steps):
    verify = _command(_run_text(steps), "verify.py")
    assert "--cassette-dir out/raw/cassettes --upload" in verify
    replay = _command(_run_text(steps), "run_replay.py")
    assert '--server-url "${{ vars.REPLAY_SERVER_URL }}" --upload' in replay
    assert "/mcp/" not in replay
    assert "out/raw/cassettes/*.json" in _run_text(steps)


FETCH_PY = r'''#!/usr/bin/env python3
import json, os, sys
script, args = sys.argv[1], sys.argv[2:]
arg = lambda flag: args[args.index(flag) + 1]
if script == "export_traces.py":
    open(os.environ["LOG"], "a").write(" ".join(args) + "\n")
    # FAIL lists the workspaces that cannot be read
    if arg("--workspace") in os.environ.get("FAIL", "").split():
        sys.exit(1)
    # HAS lists the "<workspace>:<agent>" pairs that have a standalone run
    if f"{arg('--workspace')}:{arg('--agents')}" in os.environ["HAS"].split():
        json.dump([], open(arg("-o"), "w"))
elif script == "replay/make_cassette.py":
    os.makedirs(arg("-o"), exist_ok=True)
    name = os.path.basename(args[0])
    json.dump({"agents": [name[:-5]]}, open(os.path.join(arg("-o"), name), "w"))
elif script == "trace_to_eval.py":
    os.makedirs(arg("-o"), exist_ok=True)
elif script == "run_evals.py":
    json.dump([], open(arg("--json"), "w"))
    print("CANARY-RUN-EVALS-STDOUT")
    sys.exit(1)        # a recorded run that fails a check is a baseline
else:
    sys.exit("unexpected python " + script)
'''


def _run_fetch(steps, tmp_path, has, fail=""):
    fetch = next(s for s in steps if s.get("name") == "Fetch live recordings")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "python").write_text(FETCH_PY, encoding="utf-8")
    (bin_ / "python").chmod(0o755)
    return _bash(fetch["run"], tmp_path, PATH=f"{bin_}:{os.environ['PATH']}",
                 HAS=has, FAIL=fail, LOG=str(tmp_path / "exports.log"),
                 GATE_AGENTS="ops, triage",
                 WORKSPACES="11111111-prod,22222222-stg")


def test_the_fetch_prefers_the_first_workspace_and_names_only_its_place(
        steps, tmp_path):
    p = _run_fetch(steps, tmp_path, "22222222-stg:ops 11111111-prod:triage "
                                    "22222222-stg:triage")
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    assert "ops: 1 recording(s) from workspace 2 of 2" in out
    assert "triage: 1 recording(s) from workspace 1 of 2" in out
    assert "11111111" not in out and "22222222" not in out
    assert "CANARY-RUN-EVALS-STDOUT" not in out
    raw = tmp_path / "out" / "raw"
    assert sorted(os.listdir(raw / "cassettes")) == ["ops.json", "triage.json"]
    assert sorted(os.listdir(raw / "baselines")) == ["ops.json", "triage.json"]


def test_the_fetch_takes_settled_runs_and_shares_out_the_replays(
        steps, tmp_path):
    """The newest runs are picked first, and one not fully ingested became
    a recording of placeholders. Five per agent did not fit two agents in
    the job's 60 minutes."""
    import datetime as dt
    p = _run_fetch(steps, tmp_path, "11111111-prod:ops 11111111-prod:triage")
    assert p.returncode == 0, p.stdout + p.stderr
    log = (tmp_path / "exports.log").read_text(encoding="utf-8").split("\n")
    for line in filter(None, log):
        args = line.split()
        until = dt.datetime.fromisoformat(
            args[args.index("--until") + 1].replace("Z", "+00:00"))
        age = dt.datetime.now(dt.timezone.utc) - until
        assert dt.timedelta(minutes=29) < age < dt.timedelta(minutes=31)
        assert args[args.index("--max-orchestrations") + 1] == "2"


def test_an_unreadable_workspace_moves_on_to_the_next(steps, tmp_path):
    """Production's is first; a role not granted there yet must not stop
    the gate before it tries staging's."""
    p = _run_fetch(steps, tmp_path, "22222222-stg:ops 22222222-stg:triage",
                   fail="11111111-prod")
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    assert "::warning::cannot read recordings workspace 1 of 2" in out
    assert "Log Analytics Reader" in out
    assert "ops: 1 recording(s) from workspace 2 of 2" in out
    assert "11111111" not in out and "22222222" not in out


def test_an_agent_with_no_recording_fails_the_fetch_by_name(steps, tmp_path):
    p = _run_fetch(steps, tmp_path, "11111111-prod:triage")
    assert p.returncode == 1
    assert ("::error::ops has no recording to replay: it needs at least one "
            "standalone run with a tool call in the last 14 days") in p.stdout


def test_the_gate_scores_attributed_runs_strictly(steps):
    """Scored as exported, a replay has a new operation_Id, matches no
    baseline row, and the gate passes whatever the agent did."""
    score = _step(steps, "score")
    assert "out/replay/eval_runs.jsonl" in score
    assert "--baseline artifacts/replay-baseline.json" in score
    assert "--strict-baseline" in score
    assert "baselines/" not in score        # never a hard-coded baseline file


def test_manifests_are_written_where_they_are_read(steps):
    text = _run_text(steps)
    assert '--manifest "artifacts/manifest-$id.json"' in text
    assert 'glob.glob("artifacts/manifest-*.json")' in text
    assert 'attribute_runs.py "artifacts/manifest-*.json"' in text


def test_no_workflow_invokes_an_agent_against_real_tools():
    """The rule, enforced rather than documented.

    `staging-replay.yml` ran `microsoft/ai-agent-evals`, which INVOKES the
    agents; the ops agent then wrote its results into the dev ConnectWise
    instance. It was described here for weeks as an acceptable weekly smoke
    test. It is not: an eval that writes to a system of record is not an eval,
    and the exception is what a later reader copies.

    A doc saying so is worth less than a test, because the next live action
    will arrive as a plausible line in a workflow file.
    """
    import glob
    banned = ("ai-agent-evals", "allow-live-children")
    for path in glob.glob(os.path.join(REPO, ".github", "workflows", "*.yml")):
        text = open(path, encoding="utf-8").read()
        for token in banned:
            # A comment explaining the removal is fine; a step is not.
            steps = [line for line in text.splitlines()
                     if token in line and not line.lstrip().startswith("#")]
            assert not steps, f"{os.path.basename(path)}: {steps}"


@pytest.mark.parametrize("name", ["agent-gate.yml", "evals.yml"])
def test_azure_login_needs_no_visible_subscription(name):
    """The CI apps hold roles on the project and the workspace only, so they
    see no subscription: a plain login fails "No subscriptions found", and a
    subscription-id makes azure/login run `az account set`, which fails the
    same way. Nothing in CI uses ARM."""
    path = os.path.join(REPO, ".github", "workflows", name)
    with open(path, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    logins = [s for job in wf["jobs"].values() for s in job.get("steps", [])
              if str(s.get("uses", "")).startswith("azure/login")]
    assert logins
    for step in logins:
        assert step["with"].get("allow-no-subscriptions") is True
        assert "subscription-id" not in step["with"]


def test_the_caller_deploys_staging_then_gates_then_deploys_prod(workflow):
    """The gate replays the LATEST version in the project it is pointed at:
    before the staging deploy, or against prod, it tests what is already
    there instead of the change."""
    path = os.path.join(REPO, "docs", "agent-gate-caller.yml")
    with open(path, encoding="utf-8") as fh:
        caller = yaml.safe_load(fh)
    jobs = caller["jobs"]
    gate = jobs["gate"]
    assert gate["uses"] == ("verve-it/automation-solutions-evaluations/"
                            ".github/workflows/agent-gate.yml@main")
    assert gate["needs"] == "deploy-staging"
    assert gate["with"]["environment"] == "staging"
    assert gate["secrets"] == "inherit"
    assert jobs["deploy-prod"]["needs"] == "gate"
    assert jobs["deploy-staging"]["environment"] == "staging"
    assert caller["permissions"]["id-token"] == "write"
    # Every input it passes is one the gate declares.
    on = workflow.get("on") or workflow.get(True)
    declared = set(on["workflow_call"]["inputs"])
    assert set(gate["with"]) <= declared


def test_the_configuration_check_runs_before_login_and_names_everything(steps):
    names = [s.get("name") or s.get("uses") for s in steps]
    assert names.index("Check the gate is configured") < \
        names.index("azure/login@v2")
    check = next(s for s in steps if s.get("name") == "Check the gate is configured")
    gate_text = open(GATE, encoding="utf-8").read()
    import re
    read = set(re.findall(r"(?:vars|secrets)\.([A-Z_]+)", gate_text))
    optional = {"AZURE_JUDGE_DEPLOYMENT"}
    for name in read - optional:
        assert name in check["run"], name


def test_azure_is_logged_into_again_before_each_late_azure_step(steps):
    """GitHub's OIDC assertion lives 5 minutes and the CLI presents it for
    each new resource. The first staging run reached the export 16 minutes
    in and failed AADSTS700024. Each step that first touches a resource late
    is preceded by its own login."""
    names = [s.get("name") or s.get("uses", "") for s in steps]
    for late in ("Export the window and attribute each replay to its recording",
                 "Score it in Foundry, for the detail view"):
        i = names.index(late)
        assert str(steps[i - 1].get("uses", "")).startswith("azure/login"), late
    foundry = names.index("Score it in Foundry, for the detail view")
    assert steps[foundry - 1].get("if") == steps[foundry].get("if")


# --- public channels ---------------------------------------------------------
# The job log, its annotations, $GITHUB_STEP_SUMMARY and the artifacts of a
# public repository are readable by anyone. No value read from a recording
# goes to them.

def _python_jobs():
    for path in (GATE, EVALS):
        for name, job in _load(path)["jobs"].items():
            yield os.path.basename(path), name, job


@pytest.mark.parametrize("where,name,job", list(_python_jobs()),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_every_job_runs_on_a_fresh_hosted_runner(where, name, job):
    """A hosted runner is discarded with the job; a cache is not."""
    assert job["runs-on"] == "ubuntu-latest", (where, name)
    for step in job.get("steps", []):
        assert not str(step.get("uses", "")).startswith("actions/cache"), \
            (where, name)


@pytest.mark.parametrize("where,name,job", [
    j for j in _python_jobs()
    if any(str(s.get("uses", "")).startswith("actions/setup-python")
           for s in j[2].get("steps", []))],
    ids=lambda v: v if isinstance(v, str) else "")
def test_every_python_job_keeps_tracebacks_out_of_annotations_and_cleans_up(
        where, name, job):
    """setup-python's problem matcher turns a traceback's exception line --
    an SDK error quoting the agent's input, say -- into a public annotation.
    And the last step, whatever happened, removes what was written under
    out/."""
    steps = job["steps"]
    i = next(n for n, s in enumerate(steps)
             if str(s.get("uses", "")).startswith("actions/setup-python"))
    assert steps[i + 1].get("run", "").strip() == \
        'echo "::remove-matcher owner=python::"', (where, name)
    last = steps[-1]
    assert last.get("if") == "always()", (where, name)
    assert last.get("run", "").strip().startswith("rm -rf out"), (where, name)


def _workflow_texts():
    for entry in sorted(os.listdir(WORKFLOWS)):
        with open(os.path.join(WORKFLOWS, entry), encoding="utf-8") as fh:
            yield entry, fh.read()


def test_no_workflow_prints_recorded_values():
    """Each of these prints recorded content by design: they are for a
    local, scrubbed trace, never a public job log."""
    import re
    for entry, text in _workflow_texts():
        assert "--show-values" not in text, entry
        assert "--dry-run" not in text, entry
        for line in text.splitlines():
            if "verify.py" in line:
                assert not re.search(r"\s(-v|--verbose)\b", line), (entry, line)


def test_the_gate_runs_only_a_reviewed_ref(steps):
    """`ref` is checked out and run with the environment's secrets; a caller
    could name `refs/pull/<n>/head` from a fork. Checked before the
    checkout, since after it the code has already been fetched."""
    import re
    names = [s.get("name") or s.get("uses") for s in steps]
    check = next(s for s in steps if s.get("name") == "Check the gate is configured")
    assert names.index("Check the gate is configured") < \
        names.index("actions/checkout@v4")
    assert check["env"]["REF"] == "${{ inputs.ref || 'main' }}"
    pattern = "^(main|v[0-9][0-9.]*|[0-9a-f]{40})$"
    assert f'[[ "$REF" =~ {pattern} ]]' in check["run"]
    for ref, ok in (("main", True), ("v1.2", True), ("a" * 40, True),
                    ("refs/pull/7/head", False), ("develop", False),
                    ("main; curl x", False), ("abc123", False)):
        assert bool(re.match(pattern, ref)) is ok, ref


def test_preflight_runs_before_anything_is_created(steps):
    """After the cassettes it checks exist, before verify and before the
    first clone: the point is seconds, not a failure 25 minutes in."""
    names = [s.get("name", "") for s in steps]
    pre = names.index("Preflight")
    assert names.index("Fetch live recordings") < pre
    assert pre < names.index("Verify the replay server serves what we recorded")
    assert pre < names.index("Replay each single-agent cassette against "
                             "stubbed tools")
    run = steps[pre]["run"]
    for flag in ("--gate-agents", "--project-endpoint", "--workspace",
                 "--judge-deployment", "--summary"):
        assert flag in run
    assert "--offline" not in run
