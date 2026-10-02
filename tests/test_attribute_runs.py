"""The gate must be able to fail, able to pass, and unable to be fooled.

It could not fail: a baseline is keyed by the recorded run's operation_Id, a
replay gets a new one, and every replayed row went to "new, not compared" --
so the two worst recorded runs, scored the way agent-gate.yml scored them,
passed 0 of 2 runs and exited 0.

These tests do not copy recorded rows. They rewrite the RECORDED SPANS the way
a real replay's export shows them -- a new operation_Id, the replay agent's
name, the temporary version, the temporary toolbox -- and push them through
the real converter, attribution and scorer. A fixture that copied rows passed
an "unchanged agent" test while a real export of the same run could not have.
"""
import collections
import json
import os
import subprocess
import sys

import pytest

from conftest import REPO

import attribute_runs as ar
import run_evals as ev
import run_replay as rr

OPS_TRACE = os.path.join(REPO, "traces", "2026-09-15-ops-worst-case.json")
FULL_TRACE = os.path.join(REPO, "traces", "2026-09-03-full-triage.json")
BASELINES = os.path.join(REPO, "baselines")
EXPECTED = os.path.join(REPO, "expected.json")
TOOL_DEFS = os.path.join(REPO, "tool_manifests")
OPS = "connectwise-operations-agent"
REPLAY_AGENT = OPS + "-replay"
RECORDED_TOOLBOX = "/toolboxes/ConnectwiseMCP/versions/1/"
PREFIXED = "ConnectWise-PSA-ForAgents___"


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _dims(span):
    return json.loads(span["customDimensions"])


def _set_dims(span, dims):
    span["customDimensions"] = json.dumps(dims)


def _recorded_ops():
    ops = []
    for s in _load(OPS_TRACE):
        if s["operation_Id"] not in ops:
            ops.append(s["operation_Id"])
    return ops


def as_replayed(spans, recorded_op, new_op, *, agent=REPLAY_AGENT,
                version="98", toolbox="replay-aaaaaaaaaaaa"):
    """The recorded run's spans as a replay's export would carry them."""
    out = []
    for s in spans:
        if s["operation_Id"] != recorded_op:
            continue
        s = dict(s, operation_Id=new_op)
        if toolbox:
            s["name"] = s["name"].replace(
                RECORDED_TOOLBOX, f"/toolboxes/{toolbox}/versions/1/")
        if s["name"] == f"invoke_agent {OPS}":
            s["name"] = f"invoke_agent {agent}"
        dims = _dims(s)
        for k, v in list(dims.items()):
            if v == OPS:
                dims[k] = agent
        if "gen_ai.agent.version" in dims:
            dims["gen_ai.agent.version"] = version
        _set_dims(s, dims)
        out.append(s)
    return out


def _convert(spans, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "spans.json"
    path.write_text(json.dumps(spans), encoding="utf-8")
    subprocess.run([sys.executable, os.path.join(REPO, "trace_to_eval.py"),
                    str(path), "-o", str(out_dir), "--tool-defs", TOOL_DEFS],
                   check=True, capture_output=True)
    return str(path), str(out_dir / "eval_runs.jsonl")


def _calls_to(spans, op, tool):
    """execute_tool spans the converter counts as a call to `tool`: named for
    it, or the generic `call_tool` wrapper the ops agent dispatches through,
    which trace_to_eval unwraps."""
    out = []
    for s in spans:
        if s["operation_Id"] != op or not s["name"].startswith("execute_tool"):
            continue
        dims = _dims(s)
        name = dims.get("gen_ai.tool.name") or ""
        if name == "call_tool":
            try:
                name = json.loads(dims.get("gen_ai.tool.call.arguments")
                                  or "{}").get("name") or ""
            except ValueError:
                name = ""
        if name.endswith("___" + tool):
            out.append(s)
    return out


_JOURNALS = {}
_LOCALS = {}


def _local_tools_for(recorded_op):
    """What run_replay.py writes into the manifest, computed by it: cassettes
    built from the recording, then recorded_local_tools() on the one for this
    run -- not a list typed into the test."""
    if not _LOCALS:
        import tempfile
        import make_cassette as mc
        directory = tempfile.mkdtemp(prefix="attribute-cassettes-")
        spans = mc.load_spans(OPS_TRACE)
        for c in mc.build(spans):
            path = os.path.join(directory, f"{c['orchestration_id'][:12]}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(c, fh)
            _LOCALS[c["orchestration_id"]] = path
    return rr.recorded_local_tools(_LOCALS[recorded_op])


def _journal_for(recorded_op):
    """What the stub journals for a faithful replay: every MCP call, by bare
    name. Local tools (load_skill, tool_search) never reach it. Read from the
    converted recording, which names tools the way the checks do."""
    if not _JOURNALS:
        import tempfile
        out = tempfile.mkdtemp(prefix="attribute-journals-")
        subprocess.run([sys.executable, os.path.join(REPO, "trace_to_eval.py"),
                        OPS_TRACE, "-o", out, "--tool-defs", TOOL_DEFS],
                       check=True, capture_output=True)
        for line in open(os.path.join(out, "eval_runs.jsonl"), encoding="utf-8"):
            row = json.loads(line)
            _JOURNALS[row["orchestration_id"]] = dict(collections.Counter(
                n.split("___", 1)[1] for n in row["tool_names"] if "___" in n))
    return dict(_JOURNALS[recorded_op])


def _manifest(recorded_op, version, **extra):
    return dict({
        "agent": OPS, "replay_agent": REPLAY_AGENT, "base_version": "29",
        "temp_version": version, "recorded_orchestration_id": recorded_op,
        "recorded_agent": OPS, "replay_toolbox": [f"replay-{version * 6}", "1"],
        "session_honoured": True, "journal_tools": _journal_for(recorded_op),
        "local_tools": _local_tools_for(recorded_op),
        "cassette": f"2026-09-15-{recorded_op[:12]}.json",
    }, **extra)


class Window:
    """A replay window: two replays of the ops runs among real traffic."""

    def __init__(self, tmp):
        self.tmp = tmp
        recorded = _load(OPS_TRACE)
        self.ops = _recorded_ops()
        self.replay_ops = [f"replay{i:026d}" for i in range(len(self.ops))]
        self.spans = []
        self.manifests = []
        for i, (op, new, version) in enumerate(
                zip(self.ops, self.replay_ops, ("98", "99"))):
            self.spans += as_replayed(recorded, op, new, version=version,
                                      toolbox=f"replay-{version * 6}")
            self.manifests.append(_manifest(op, version))
        # Real traffic in the same window: the whole full-triage set (the ops
        # agent at v16 among it), and a production run of the ops agent at
        # its own name and BASE version.
        self.spans += _load(FULL_TRACE)
        self.spans += as_replayed(recorded, self.ops[0], "production" + "0" * 22,
                                  agent=OPS, version="29", toolbox=None)

    def write_manifests(self, manifests=None):
        paths = []
        for i, m in enumerate(manifests or self.manifests):
            p = self.tmp / f"manifest-{i}.json"
            p.write_text(json.dumps(m), encoding="utf-8")
            paths.append(str(p))
        return paths

    def attribute(self, spans=None, manifests=None, rows=None, extra=(),
                  baselines=BASELINES):
        spans_path, runs = _convert(spans or self.spans, self.tmp / "window")
        if rows is not None:
            rows = rows(json.loads(l) for l in open(runs, encoding="utf-8") if l.strip())
            with open(runs, "w", encoding="utf-8") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in rows)
        paths = (manifests if manifests and isinstance(manifests[0], str)
                 else self.write_manifests(manifests))
        return ar.main(paths + [
            "--runs", runs, "--baselines", baselines, "--tool-defs", TOOL_DEFS,
            "--spans", spans_path, "--out-spans", str(self.tmp / "replay-spans.json"),
            "--out-runs", str(self.tmp / "replay.jsonl"),
            "--out-baseline", str(self.tmp / "replay-baseline.json"),
            *extra])

    def gate(self, *extra):
        return ev.main([str(self.tmp / "replay.jsonl"), "--expected", EXPECTED,
                        "--baseline", str(self.tmp / "replay-baseline.json"),
                        "--strict-baseline", *extra])

    def rows(self):
        return [json.loads(l) for l in open(self.tmp / "replay.jsonl", encoding="utf-8")]


@pytest.fixture
def w(tmp_path):
    return Window(tmp_path)


# ------------------------------------------------------------ the original bug

def test_replays_that_are_all_v1_are_told_apart_by_their_toolbox(w):
    """The first full staging run: the replay agent is made for each run, so
    every clone is v1, and attribution refused all seven replays -- '2 scored
    runs of connectwise-operations-agent-replay v1 ... refusing to pick'."""
    recorded = _load(OPS_TRACE)
    spans, manifests = [], []
    for op, new, box in zip(w.ops, w.replay_ops, ("replay-aaaa", "replay-bbbb")):
        spans += as_replayed(recorded, op, new, version="1", toolbox=box)
        manifests.append(_manifest(op, "1", replay_toolbox=[box, "1"]))
    spans += _load(FULL_TRACE)
    assert w.attribute(spans=spans, manifests=manifests) == 0
    got = {r["orchestration_id"] for r in w.rows()}
    assert got == set(w.ops)        # each presented as its own recording


def test_the_old_gate_passed_the_two_worst_runs(w, capsys):
    """Scored as exported, against the baseline the workflow named, nothing
    is compared and the exit code is 0."""
    _, runs = _convert(w.spans, w.tmp / "window")
    code = ev.main([runs, "--expected", EXPECTED, "--baseline",
                    os.path.join(BASELINES, "full-triage-2026-09-18.json")])
    out = capsys.readouterr().out
    assert "NEW RUNS" in out and "REGRESSED" not in out
    assert code == 0


def test_strict_mode_refuses_the_same_comparison(w, capsys):
    _, runs = _convert(w.spans, w.tmp / "window")
    code = ev.main([runs, "--expected", EXPECTED, "--strict-baseline",
                    "--baseline",
                    os.path.join(BASELINES, "full-triage-2026-09-18.json")])
    assert code == 1
    assert "STRICT BASELINE FAILED" in capsys.readouterr().out


# ------------------------------------------------------------ attributed path

def test_an_unchanged_agent_passes(w, capsys):
    """From spans carrying the replay agent, temporary version and temporary
    toolbox: every verdict must match the recording."""
    assert w.attribute() == 0
    assert w.gate() == 0
    assert "no change against baseline" in capsys.readouterr().out


def test_the_replay_is_presented_under_the_recordings_identity(w):
    assert w.attribute() == 0
    rows = w.rows()
    assert {r["run_agent"] for r in rows} == {OPS}
    assert all(r["traj_key"].split("|")[0] == OPS for r in rows)
    assert all(r["has_tool_definitions"] for r in rows)
    assert all(r["mcp_toolboxes"] == [{"toolbox": "ConnectwiseMCP",
                                       "version": "1"}] for r in rows)
    # and what was actually observed is kept
    assert {r["replay"]["replay_agent"] for r in rows} == {REPLAY_AGENT}
    assert all(r["replay"]["toolboxes"][0]["toolbox"].startswith("replay-")
               for r in rows)


def test_a_gating_regression_fails_and_the_summary_names_it(w, tmp_path):
    """The changed agent sends cw_resolve reference_type='severity' -- the
    bug the enum exists to catch -- in a run that passed valid_tool_args."""
    spans = [dict(s) for s in w.spans]
    target = _calls_to(spans, w.replay_ops[0], "cw_resolve")[0]
    dims = _dims(target)
    args = json.loads(dims["gen_ai.tool.call.arguments"])
    inner = args.get("arguments") if dims.get("gen_ai.tool.name") == "call_tool" \
        else args
    inner["reference_type"] = "severity"
    dims["gen_ai.tool.call.arguments"] = json.dumps(args)
    _set_dims(target, dims)
    assert w.attribute(spans=spans) == 0
    summary = str(tmp_path / "summary.md")
    assert w.gate("--summary", summary) == 1
    text = open(summary, encoding="utf-8").read()
    assert "**regressed**" in text and "`valid_tool_args` | yes" in text
    assert "Compared **2 of 2**" in text
    # the regression comes first: it is what turned the build red
    assert text.index("### Against the baseline") < \
        text.index("### Runs that failed a gating check")


def test_a_reporting_regression_does_not_fail_the_build(w, capsys):
    """no_truncation is not GATING. It is shown, and the build stays green."""
    assert w.attribute() == 0
    rows = w.rows()
    rows[0]["truncated_results"] = 1
    with open(w.tmp / "replay.jsonl", "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    assert w.gate() == 0
    out = capsys.readouterr().out
    assert "no_truncation" in out and "does not fail the build" in out


def test_only_the_replays_are_scored(w):
    """Same agent, same base version, its own name: not the replay."""
    assert w.attribute() == 0
    rows = w.rows()
    assert len(rows) == 2
    assert {r["replay"]["operation_id"] for r in rows} == set(w.replay_ops)


def test_another_agent_at_the_same_version_is_not_the_replay(w):
    """Version numbers are per agent. Matching on the version alone would
    find two runs of 'v98' and refuse, or score the wrong one."""
    recorded = _load(OPS_TRACE)
    spans = w.spans + as_replayed(recorded, w.ops[0], "stranger" + "0" * 24,
                                  agent="some-other-agent", version="98",
                                  toolbox=None)
    assert w.attribute(spans=spans) == 0
    assert {r["replay"]["operation_id"] for r in w.rows()} == set(w.replay_ops)


def test_telemetry_tool_definitions_survive_normalisation():
    """A replay's own tool definitions from telemetry are kept, and the
    recording's manifest adds to them rather than replacing them."""
    import trace_to_eval as tte
    manifests = tte.load_tool_manifests([TOOL_DEFS])
    extra = {"name": "only_in_telemetry", "parameters": {}}
    row = {"run_agent": REPLAY_AGENT, "traj_key": REPLAY_AGENT,
           "orchestration_id": "op", "agent_version": "98",
           "tool_definitions": [extra], "tool_definitions_source": "telemetry",
           "mcp_toolboxes": [{"toolbox": "replay-x", "version": "1"}]}
    out = ar.normalise(row, {"agent": OPS}, ("rec", OPS),
                       {"mcp_toolboxes": [{"toolbox": "ConnectwiseMCP",
                                           "version": "1"}]}, manifests)
    names = {d.get("name") for d in out["tool_definitions"]}
    assert "only_in_telemetry" in names and len(names) > 1
    assert out["tool_definitions_source"].startswith("telemetry+manifest:")


def test_a_call_the_stub_did_not_record_is_not_an_avoidable_call():
    """2026-10-02, cassette 1f3c5a2f5a73: the journal had 3 diverged calls
    (cw_resolve x2, cw_get) and the trace 3 `empty_failed`, because a failed
    call carries no ToolCallResult. Scored as is, no_wasted_calls -- a
    gating check -- failed an unchanged agent for leaving the recording."""
    def err(tool, kind="empty_failed"):
        return {"tool": PREFIXED + tool, "kind": kind, "args": "{}"}
    row = {"run_agent": REPLAY_AGENT, "traj_key": REPLAY_AGENT,
           "orchestration_id": "op", "agent_version": "1",
           "tool_call_count": 6,
           "tool_errors": [err("cw_resolve"), err("cw_resolve"),
                           err("cw_get"), err("cw_get"),
                           err("cw_query", "invalid_entity")]}
    m = {"agent": OPS, "diverged_tools": {"cw_resolve": 2, "cw_get": 1}}
    out = ar.normalise(row, m, ("rec", OPS), {}, None)

    kinds = [(e["tool"].split("___")[-1], e["kind"]) for e in out["tool_errors"]]
    assert kinds == [("cw_resolve", "not_recorded"),
                     ("cw_resolve", "not_recorded"),
                     ("cw_get", "not_recorded"),
                     # one more empty failure than the stub diverged on: a
                     # recorded failure, still the agent's
                     ("cw_get", "empty_failed"),
                     ("cw_query", "invalid_entity")]
    assert out["replay"]["not_recorded"] == {"cw_get": 1, "cw_resolve": 2}
    assert row["tool_errors"][0]["kind"] == "empty_failed"  # input untouched

    wasted = ev.check_no_wasted_calls(out, {})
    assert wasted["passed"] is False and wasted["count"] == 2
    # Still a tool error: no_tool_errors reports every divergence.
    assert ev.check_no_tool_errors(out, {})["count"] == 5


def test_an_old_manifest_relabels_nothing():
    row = {"run_agent": REPLAY_AGENT, "traj_key": REPLAY_AGENT,
           "orchestration_id": "op", "agent_version": "1",
           "tool_errors": [{"tool": "x", "kind": "empty_failed"}]}
    out = ar.normalise(row, {"agent": OPS}, ("rec", OPS), {}, None)
    assert out["tool_errors"][0]["kind"] == "empty_failed"
    assert out["replay"]["not_recorded"] == {}


def test_the_manifest_counts_the_stubs_divergences_per_tool():
    journal = [{"tool": PREFIXED + "cw_get_ticket", "outcome": "matched"},
               {"tool": PREFIXED + "cw_resolve", "outcome": "diverged"},
               {"tool": PREFIXED + "cw_resolve", "outcome": "matched"},
               {"tool": PREFIXED + "cw_get", "outcome": "diverged"},
               {"tool": PREFIXED + "cw_resolve", "outcome": "diverged"}]
    assert rr.diverged_tools(journal) == {"cw_get": 1, "cw_resolve": 2}
    assert rr.diverged_tools(None) is None


def test_the_baseline_is_the_recordings_own(w):
    assert w.attribute() == 0
    base = _load(w.tmp / "replay-baseline.json")
    ops = _load(os.path.join(BASELINES, "ops-worst-case-2026-09-18.json"))
    assert sorted(b["orchestration_id"] for b in base) == \
        sorted(b["orchestration_id"] for b in ops)


def test_the_foundry_dataset_sees_the_recordings_identity(w):
    """to_foundry_dataset.py converts spans itself. Under the replay's names
    it would find no schema and no trajectory expectation, and the detail
    view would score less than the gate did."""
    assert w.attribute() == 0
    spans = _load(w.tmp / "replay-spans.json")
    assert {s["operation_Id"] for s in spans} == set(w.replay_ops)
    assert not any("/toolboxes/replay-" in s["name"] for s in spans)
    assert not any(REPLAY_AGENT in s["customDimensions"] for s in spans)
    out = w.tmp / "foundry.jsonl"
    subprocess.run([sys.executable,
                    os.path.join(REPO, "foundry", "to_foundry_dataset.py"),
                    str(w.tmp / "replay-spans.json"), "--expected", EXPECTED,
                    "--tool-defs", TOOL_DEFS, "--no-messages", "-o", str(out)],
                   check=True, capture_output=True)
    rows = [json.loads(l) for l in open(out, encoding="utf-8") if l.strip()]
    assert len(rows) == 2
    assert all(r["tool_definitions"] and r["expected_actions"] for r in rows)


def test_spans_with_unparseable_dimensions_are_kept_as_exported(w):
    spans = [dict(s) for s in w.spans]
    odd = next(s for s in spans if s["operation_Id"] == w.replay_ops[0])
    odd["customDimensions"] = ""
    assert w.attribute(spans=spans) == 0
    out = _load(w.tmp / "replay-spans.json")
    assert any(s["customDimensions"] == "" for s in out)


def test_a_quoted_glob_is_expanded_here(w):
    """PowerShell passes the pattern through unexpanded."""
    w.write_manifests()
    assert w.attribute(manifests=[str(w.tmp / "manifest-*.json")]) == 0


def test_a_manifest_for_a_different_agent_is_refused(w, capsys):
    """run_replay.py refuses to replay another agent against a cassette; an
    orchestrator on a single-agent cassette reaches its children, unstubbed,
    by name. Attribution refuses it too, in case a manifest came from
    anywhere else."""
    manifests = [dict(m, agent="triage-orchestrator") for m in w.manifests]
    assert w.attribute(manifests=manifests) == ar.EXIT_FAILED
    assert "against a recording of" in capsys.readouterr().out


# ------------------------------------------------- the stub, and only the stub

def test_calls_through_a_live_toolbox_are_refused(w, capsys):
    """The agent change stopped reading CONNECTWISE_TOOLBOX_NAME: the clone
    called the production toolbox. Matching its recording exactly is the
    worst case, not the best."""
    recorded = _load(OPS_TRACE)
    spans = [s for s in w.spans if s["operation_Id"] != w.replay_ops[0]]
    spans += as_replayed(recorded, w.ops[0], w.replay_ops[0], toolbox=None)
    assert w.attribute(spans=spans) == ar.EXIT_FAILED
    assert "ConnectwiseMCP@1" in capsys.readouterr().out


def test_mcp_calls_that_never_reached_the_stub_are_refused(w, capsys):
    """Nothing reached the server: no session in either bucket, so
    session_honoured is None and the journal is empty."""
    manifests = [dict(w.manifests[0], session_honoured=None, journal_tools={},
                      replay_toolbox=None), w.manifests[1]]
    assert w.attribute(manifests=manifests) == ar.EXIT_FAILED
    assert "none reached the replay server" in capsys.readouterr().out


def test_more_calls_than_the_stub_journalled_are_refused(w, capsys):
    journal = dict(w.manifests[0]["journal_tools"])
    journal["cw_resolve"] -= 1
    manifests = [dict(w.manifests[0], journal_tools=journal), w.manifests[1]]
    assert w.attribute(manifests=manifests) == ar.EXIT_FAILED
    assert "cw_resolve x1" in capsys.readouterr().out


def _bare_writes(spans, op):
    """The agent change sends cw_update through a client it built itself:
    same tool, no MCP prefix, no toolbox. Rewrites both span forms."""
    out = []
    for s in spans:
        s = dict(s)
        if s["operation_Id"] == op and s["name"].startswith("execute_tool"):
            dims = _dims(s)
            if dims.get("gen_ai.tool.name") == PREFIXED + "cw_update":
                dims["gen_ai.tool.name"] = "cw_update"
                s["name"] = "execute_tool cw_update"
            elif dims.get("gen_ai.tool.name") == "call_tool":
                args = json.loads(dims.get("gen_ai.tool.call.arguments") or "{}")
                if args.get("name") == PREFIXED + "cw_update":
                    dims["gen_ai.tool.name"] = "cw_update"
                    dims["gen_ai.tool.call.arguments"] = json.dumps(
                        args.get("arguments") or {})
                    s["name"] = "execute_tool cw_update"
            _set_dims(s, dims)
        out.append(s)
    return out


def test_a_bare_named_live_write_is_refused(w, capsys):
    """Found by review: the old check counted only `<label>___` names as
    calls that must reach the stub, so a write under a bare name was
    'local', the journal did not miss it, and the gate reported no change."""
    spans = _bare_writes(w.spans, w.replay_ops[1])
    journal = dict(w.manifests[1]["journal_tools"])
    journal.pop("cw_update")
    manifests = [w.manifests[0], dict(w.manifests[1], journal_tools=journal)]
    assert w.attribute(spans=spans, manifests=manifests) == ar.EXIT_FAILED
    assert "cw_update x1" in capsys.readouterr().out


def test_bare_named_calls_with_nothing_journalled_are_refused(w, capsys):
    spans = _bare_writes(w.spans, w.replay_ops[1])
    manifests = [w.manifests[0], dict(w.manifests[1], session_honoured=None,
                                      journal_tools={}, replay_toolbox=None)]
    assert w.attribute(spans=spans, manifests=manifests) == ar.EXIT_FAILED
    assert "none reached the replay server" in capsys.readouterr().out


def test_a_local_tool_this_recording_never_used_is_not_a_bypass(w):
    """Found by review: local tools came from one recording, and 73d29 never
    called tool_search -- so an unchanged agent that did call it, locally,
    was refused as a stub bypass."""
    local = [s for s in w.spans if s["operation_Id"] == w.replay_ops[1]
             and s["name"] == "execute_tool load_skill"][0]
    search = dict(local, id="added-tool-search",
                  name="execute_tool tool_search")
    dims = _dims(search)
    dims["gen_ai.tool.name"] = "tool_search"
    _set_dims(search, dims)
    assert "tool_search" not in [
        s["name"].split(" ", 1)[1] for s in _load(OPS_TRACE)
        if s["operation_Id"] == w.ops[1] and s["name"].startswith("execute_tool")]
    assert w.attribute(spans=w.spans + [search]) == 0


def test_the_models_parallel_wrapper_is_not_expected_at_the_stub():
    """gpt models sometimes emit `multi_tool_use.parallel` as a tool call.
    Every replay of triage-analysis-agent on 2026-10-02 did; the spans had
    no children and an empty result, and attribution refused all five runs
    as stub bypasses. It is in the shipped config, so it is local."""
    with open(os.path.join(REPO, "eval-config.json"), encoding="utf-8") as fh:
        config = json.load(fh)
    assert "multi_tool_use.parallel" in config["local_tools"]
    row = {"tool_names": ["multi_tool_use.parallel", PREFIXED + "cw_search"]}
    every, remote = ar.tool_counts(row, config["local_tools"])
    assert every["multi_tool_use.parallel"] == 1
    assert dict(remote) == {"cw_search": 1}


def test_local_tools_come_from_config_and_every_recording_of_the_agent(
        tmp_path, monkeypatch):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    other = tmp_path / "c.json"
    a.write_text(json.dumps({"agents": [OPS], "interactions": [
        {"tool": "load_skill"}, {"tool": PREFIXED + "cw_query"}]}), encoding="utf-8")
    b.write_text(json.dumps({"agents": [OPS], "interactions": [
        {"tool": "tool_search"}]}), encoding="utf-8")
    other.write_text(json.dumps({"agents": ["someone-else"], "interactions": [
        {"tool": "their_local_thing"}]}), encoding="utf-8")
    monkeypatch.setattr(rr, "CONFIG", {"local_tools": ["run_skill_script"]})
    assert rr.recorded_local_tools(str(a)) == [
        "load_skill", "run_skill_script", "tool_search"]


def test_a_manifest_without_local_tools_is_refused(w):
    old = dict(w.manifests[0])
    del old["local_tools"]
    assert w.attribute(manifests=[old, w.manifests[1]]) == ar.EXIT_FAILED


def test_an_unattributable_session_is_refused(w):
    """Calls landed in the server's shared bucket: its journal is not this
    run's, so neither completeness nor stub routing can be shown."""
    manifests = [dict(w.manifests[0], session_honoured=False), w.manifests[1]]
    assert w.attribute(manifests=manifests) == ar.EXIT_FAILED


def test_an_a2a_call_is_refused(w, capsys):
    def with_a2a(rows):
        rows = list(rows)
        for r in rows:
            if r["orchestration_id"] == w.replay_ops[0]:
                r["a2a_calls"] = [{"agent": "triage-analysis-agent"}]
        return rows
    assert w.attribute(rows=with_a2a) == ar.EXIT_FAILED
    assert "A2A" in capsys.readouterr().out


def test_another_agent_in_the_replayed_operation_is_refused(w, capsys):
    """A child run in the replay's own operation ran unstubbed."""
    child = [dict(s, operation_Id=w.replay_ops[0]) for s in _load(FULL_TRACE)
             if s["operation_Id"].startswith("4dda7f4fa5f0")
             and "triage-analysis-agent" in s["customDimensions"]]
    assert child
    assert w.attribute(spans=w.spans + child) == ar.EXIT_FAILED
    assert "triage-analysis-agent" in capsys.readouterr().out


# --------------------------------------------------------- ingestion lag

def test_a_partly_ingested_run_waits(w, capsys):
    """One cw_resolve call not ingested yet: fewer in the trace than the stub
    journalled. Scoring now would call its absence a regression."""
    drop = _calls_to(w.spans, w.replay_ops[0], "cw_resolve")[0]
    spans = [s for s in w.spans if s is not drop
             and s.get("operation_ParentId") != drop["id"]]
    assert w.attribute(spans=spans) == ar.EXIT_NOT_YET
    assert "cw_resolve x1" in capsys.readouterr().out


def test_a_run_not_ingested_at_all_waits_and_names_what_was_there(w, capsys):
    spans = [s for s in w.spans if s["operation_Id"] != w.replay_ops[0]]
    assert w.attribute(spans=spans) == ar.EXIT_NOT_YET
    out = capsys.readouterr().out
    assert f"no scored run of {REPLAY_AGENT} v98" in out
    assert f"{OPS} v16" in out          # the evidence


def test_local_tools_are_not_expected_at_the_stub(w):
    """load_skill and tool_search are in the trace and never reach the
    server; a per-tool comparison must not wait for them."""
    assert "load_skill" not in w.manifests[0]["journal_tools"]
    assert w.attribute() == 0


def test_a_hard_failure_beside_a_retryable_one_is_not_retried(w):
    """Waiting fixes ingestion lag; it does not fix a missing baseline."""
    spans = [s for s in w.spans if s["operation_Id"] != w.replay_ops[0]]
    manifests = [w.manifests[0], dict(w.manifests[1], replay_agent=None)]
    assert w.attribute(spans=spans, manifests=manifests) == ar.EXIT_FAILED


# ----------------------------------------------------- everything else fails

def test_a_recording_with_no_baseline_is_a_hard_failure(w, tmp_path):
    empty = tmp_path / "no-baselines"
    empty.mkdir()
    assert w.attribute(baselines=str(empty)) == ar.EXIT_FAILED


def test_a_recording_in_two_baselines_is_refused(w, tmp_path):
    two = tmp_path / "two"
    two.mkdir()
    ops = open(os.path.join(BASELINES, "ops-worst-case-2026-09-18.json"), encoding="utf-8").read()
    (two / "a.json").write_text(ops, encoding="utf-8")
    (two / "b.json").write_text(ops, encoding="utf-8")
    assert w.attribute(baselines=str(two)) == ar.EXIT_FAILED


def test_two_replays_of_one_recording_are_refused(w):
    manifests = [w.manifests[0], dict(w.manifests[1],
                                      recorded_orchestration_id=w.ops[0])]
    assert w.attribute(manifests=manifests) == ar.EXIT_FAILED


def test_one_run_claimed_by_two_manifests_is_refused(w, capsys):
    manifests = [w.manifests[0], dict(w.manifests[0],
                                      recorded_orchestration_id=w.ops[1])]
    assert w.attribute(manifests=manifests) == ar.EXIT_FAILED
    assert "also attributed" in capsys.readouterr().out


def test_two_candidate_rows_are_refused(w):
    recorded = _load(OPS_TRACE)
    extra = as_replayed(recorded, w.ops[0], "twin" + "0" * 28, version="98",
                        toolbox="replay-989898989898")
    assert w.attribute(spans=w.spans + extra) == ar.EXIT_FAILED


def test_an_old_manifest_is_refused(w):
    old = dict(w.manifests[0])
    del old["replay_agent"]
    assert w.attribute(manifests=[old, w.manifests[1]]) == ar.EXIT_FAILED


def test_a_manifest_without_journal_counts_is_refused(w):
    old = dict(w.manifests[0])
    del old["journal_tools"]
    assert w.attribute(manifests=[old, w.manifests[1]]) == ar.EXIT_FAILED


def test_no_manifest_at_all_is_a_failure(w):
    _, runs = _convert(w.spans, w.tmp / "window")
    assert ar.main([str(w.tmp / "nothing-*.json"), "--runs", runs,
                    "--out-runs", str(w.tmp / "r.jsonl"),
                    "--out-baseline", str(w.tmp / "b.json")]) == ar.EXIT_FAILED


def test_nothing_is_written_when_anything_fails(w):
    spans = [s for s in w.spans if s["operation_Id"] != w.replay_ops[1]]
    assert w.attribute(spans=spans) != 0
    assert not (w.tmp / "replay.jsonl").exists()
    assert not (w.tmp / "replay-spans.json").exists()


# ---------------------------------------------------- what a red build shows

def test_a_hard_attribution_failure_is_explained_in_the_summary(w, tmp_path):
    empty = tmp_path / "none"
    empty.mkdir()
    summary = tmp_path / "summary.md"
    assert w.attribute(baselines=str(empty),
                       extra=["--summary", str(summary)]) == ar.EXIT_FAILED
    assert "no committed baseline" in summary.read_text(encoding="utf-8")


def test_a_retryable_failure_is_summarised_only_on_the_final_attempt(w, tmp_path):
    spans = [s for s in w.spans if s["operation_Id"] != w.replay_ops[0]]
    summary = tmp_path / "summary.md"
    assert w.attribute(spans=spans, extra=["--summary", str(summary)]) \
        == ar.EXIT_NOT_YET
    assert not summary.exists()
    assert w.attribute(spans=spans, extra=["--summary", str(summary),
                                           "--final"]) == ar.EXIT_NOT_YET
    assert "no scored run" in summary.read_text(encoding="utf-8")


def test_a_strict_failure_is_explained_in_the_summary(w, tmp_path):
    _, runs = _convert(w.spans, w.tmp / "window")
    summary = tmp_path / "summary.md"
    assert ev.main([runs, "--expected", EXPECTED, "--strict-baseline",
                    "--summary", str(summary), "--baseline",
                    os.path.join(BASELINES, "ops-worst-case-2026-09-18.json")]) == 1
    text = summary.read_text(encoding="utf-8")
    assert "Strict comparison failed" in text and "no baseline row" in text


# ------------------------------------------------------------- strict diff

def _row(op, checks):
    return {"orchestration_id": op, "run_agent": "a", "checks": checks,
            "passed": True}


def test_strict_fails_when_only_some_runs_were_compared():
    fails = ev.strict_failures([_row("op1", {}), _row("op2", {})],
                               [("op2", "a")], [])
    assert any("no baseline row" in s for s in fails)


def test_strict_fails_on_a_baseline_row_that_was_not_scored():
    assert ev.strict_failures([_row("op1", {})], [], [("op9", "a")])


def test_strict_fails_when_nothing_was_scored():
    assert "no run was scored" in ev.strict_failures([], [], [])


def test_strict_passes_when_everything_was_compared():
    assert ev.strict_failures([_row("op1", {})], [], []) == []


def test_strict_needs_a_baseline(tmp_path):
    runs = tmp_path / "r.jsonl"
    runs.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        ev.main([str(runs), "--strict-baseline"])


def _scored(checks, op="op1"):
    return {"orchestration_id": op, "run_agent": "a", "traj_key": "a",
            "intent": None, "failed_gating": [], "passed": True,
            "checks": checks}


@pytest.fixture(autouse=False)
def quiet_report(monkeypatch):
    monkeypatch.setattr(ev, "print_report", lambda rows: None)


def test_lost_coverage_on_a_reporting_check_does_not_fail(tmp_path, monkeypatch, quiet_report):
    base = [_scored({"no_truncation": {"passed": True, "reason": ""},
                     "trajectory": {"passed": True, "reason": ""}})]
    now = [_scored({"no_truncation": {"passed": None, "reason": "n/a"},
                    "trajectory": {"passed": True, "reason": ""}})]
    monkeypatch.setattr(ev, "score", lambda runs, cfg: now)
    (tmp_path / "runs.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "base.json").write_text(json.dumps(base), encoding="utf-8")
    summary = tmp_path / "summary.md"
    assert ev.main([str(tmp_path / "runs.jsonl"), "--baseline",
                    str(tmp_path / "base.json"), "--summary", str(summary)]) == 0
    text = summary.read_text(encoding="utf-8")
    assert "| `no_truncation` | no | stopped scoring |" in text


def test_lost_coverage_on_a_gating_check_fails(tmp_path, monkeypatch, quiet_report):
    base = [_scored({"trajectory": {"passed": True, "reason": ""}})]
    now = [_scored({"trajectory": {"passed": None, "reason": "no expected"}})]
    monkeypatch.setattr(ev, "score", lambda runs, cfg: now)
    (tmp_path / "runs.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "base.json").write_text(json.dumps(base), encoding="utf-8")
    summary = tmp_path / "summary.md"
    assert ev.main([str(tmp_path / "runs.jsonl"), "--baseline",
                    str(tmp_path / "base.json"), "--summary", str(summary)]) == 1
    assert "| `trajectory` | yes | stopped scoring |" in summary.read_text(encoding="utf-8")


def test_a_reporting_regression_is_marked_as_not_gating_in_the_summary(
        tmp_path, monkeypatch, quiet_report):
    base = [_scored({"no_tool_errors": {"passed": True, "reason": ""}})]
    now = [_scored({"no_tool_errors": {"passed": False, "reason": "1/9"}})]
    monkeypatch.setattr(ev, "score", lambda runs, cfg: now)
    (tmp_path / "runs.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "base.json").write_text(json.dumps(base), encoding="utf-8")
    summary = tmp_path / "summary.md"
    assert ev.main([str(tmp_path / "runs.jsonl"), "--baseline",
                    str(tmp_path / "base.json"), "--summary", str(summary)]) == 0
    assert "| `no_tool_errors` | no | **regressed** |" in summary.read_text(encoding="utf-8")


def test_only_gating_entries_fail_a_build():
    entries = [(("o", "a"), "no_truncation", ""), (("o", "a"), "trajectory", "")]
    assert ev.gating_only(entries) == [entries[1]]


# ------------------------------------------------------------ run_replay side

def test_the_manifest_records_which_recording_it_replayed(tmp_path):
    import argparse
    cassette = tmp_path / "c.json"
    cassette.write_text(json.dumps({"orchestration_id": "rec123",
                                    "agents": [OPS, "child"]}), encoding="utf-8")
    args = argparse.Namespace(agent=OPS, cassette=str(cassette),
                              server_url="https://r.example.net/mcp",
                              manifest=str(tmp_path / "m.json"))
    journal = [{"tool": "cw_query"}, {"tool": "cw_query"},
               {"tool": f"{PREFIXED}cw_resolve"}]
    payload = rr.write_manifest(
        str(tmp_path / "m.json"), args, "29", "98",
        {"replayed_calls": 3, "journal": journal},
        {"replay_agent": REPLAY_AGENT, "replay_toolbox": ["replay-x", "1"]})
    assert payload["recorded_orchestration_id"] == "rec123"
    assert payload["recorded_agent"] == OPS
    assert payload["journal_tools"] == {"cw_query": 2, "cw_resolve": 1}
    assert payload["replay_agent"] == REPLAY_AGENT


def test_an_unreadable_cassette_leaves_the_identity_empty(tmp_path):
    assert rr.recorded_identity(str(tmp_path / "missing.json")) == (None, None)


def test_the_replay_binds_under_the_recordings_server_label(tmp_path):
    """Foundry names MCP tools `<server_label>___<tool>`. Any other label is a
    renamed tool, a changed trajectory and a changed contract."""
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"interactions": [
        {"tool": f"{PREFIXED}cw_query"}, {"tool": "load_skill"}]}), encoding="utf-8")
    assert rr.recorded_server_label(str(c)) == PREFIXED.rstrip("_")


def test_a_recording_with_no_mcp_call_falls_back_to_config(tmp_path):
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"interactions": [{"tool": "load_skill"}]}), encoding="utf-8")
    assert rr.recorded_server_label(str(c)) == rr.REPLAY_TOOL_LABEL


def test_a_recording_of_two_mcp_servers_is_refused(tmp_path):
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"interactions": [
        {"tool": "a___x"}, {"tool": "b___y"}]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        rr.recorded_server_label(str(c))


def test_the_clone_is_never_a_version_of_the_agent_under_test():
    assert rr.replay_agent_name(OPS) == REPLAY_AGENT
    with pytest.raises(SystemExit):
        rr.replay_agent_name("a" * 60)


class _Agents:
    def __init__(self, latest, rules=None):
        from types import SimpleNamespace as NS
        selector = NS(version_selection_rules=rules) if rules else None
        self.details = NS(versions=NS(latest=NS(version=latest)),
                          agent_endpoint=NS(version_selector=selector))
        self.uploaded = None

    def get(self, name):
        return self.details

    def download_code(self, name, agent_version):
        self.downloaded = (name, agent_version)
        return [b"zip"]

    def create_version_from_code(self, agent_name, **kw):
        self.uploaded = agent_name
        return None


def test_the_replay_agent_must_resolve_to_the_clone_before_invoking():
    assert rr.routing_problem(_Agents("98"), REPLAY_AGENT, "98") is None
    assert "resolves to v97" in rr.routing_problem(_Agents("97"),
                                                   REPLAY_AGENT, "98")


def test_a_version_selector_on_the_replay_agent_is_refused():
    from types import SimpleNamespace as NS
    rules = [NS(agent_version="5", traffic_percentage=100)]
    assert "version selector" in rr.routing_problem(
        _Agents("98", rules), REPLAY_AGENT, "98")


def test_foundrys_default_latest_selector_is_not_a_problem():
    """Every new agent gets `@latest` at 100%. The first gate run in staging
    refused its own clone for it: 'has a version selector (v@latest (100%))'."""
    from types import SimpleNamespace as NS
    latest = [NS(agent_version="@latest", traffic_percentage=100)]
    assert rr.routing_problem(_Agents("98", latest), REPLAY_AGENT, "98") is None
    # ...but @latest must still be the clone.
    assert "resolves to v97" in rr.routing_problem(
        _Agents("97", latest), REPLAY_AGENT, "98")


def test_a_selector_pinned_to_the_clone_is_safe_and_a_split_is_not():
    from types import SimpleNamespace as NS
    pinned = [NS(agent_version="98", traffic_percentage=100)]
    assert rr.routing_problem(_Agents("98", pinned), REPLAY_AGENT, "98") is None
    split = [NS(agent_version="@latest", traffic_percentage=50),
             NS(agent_version="5", traffic_percentage=50)]
    assert "version selector" in rr.routing_problem(
        _Agents("98", split), REPLAY_AGENT, "98")


def test_the_hosted_clone_takes_the_base_agents_code_and_the_replay_name():
    agents = _Agents("98")
    rr.HostedBinding().create(agents, REPLAY_AGENT, {}, description="",
                              metadata={}, base_version="29", source_agent=OPS)
    assert agents.downloaded == (OPS, "29")
    assert agents.uploaded == REPLAY_AGENT


def test_the_dry_run_shows_the_replay_agent_and_the_recorded_label(tmp_path,
                                                                   capsys):
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"orchestration_id": "rec", "agents": [OPS],
                             "query": "q",
                             "interactions": [{"tool": f"{PREFIXED}cw_query"}]}), encoding="utf-8")
    assert rr.main(["--cassette", str(c), "--server-url",
                    "https://replay.example.net/mcp/x", "--dry-run"]) == 0
    out = capsys.readouterr().out
    plan = json.loads(out[out.index("{"):out.rindex("}") + 1])
    assert plan["replay_agent"] == REPLAY_AGENT
    assert plan["tools_replaced_with"][0]["server_label"] == PREFIXED.rstrip("_")
