"""No value read from a recording reaches a public channel.

The repository is public, so the gate's job log, its step summary and its
uploaded artifacts are readable by anyone. A canary is planted in every place
a replayed run carries customer content -- the agent's input, a tool's
arguments and result, an enum value the schema rejects, the name of a skill
that failed to load -- and the attribution and scoring steps are run on it as
the gate runs them. The canary must reach the raw files under out/ (or the
test proves nothing) and must not reach stdout, stderr, a summary, or any
file the workflow uploads.
"""
import json
import os
import re
import subprocess
import sys

import pytest

from conftest import REPO
from leakcheck import found
from test_attribute_runs import Window, _calls_to, _dims, _set_dims

CANARY = "CANARYLEAK7f3aQZ"
GATE = os.path.join(REPO, ".github", "workflows", "agent-gate.yml")


def _plant(w):
    """The first replay's spans, with the canary in each kind of content."""
    op = w.replay_ops[0]
    spans = [dict(s) for s in w.spans]
    by_id = {id(s): i for i, s in enumerate(w.spans)}

    def edit(span, fn):
        i = by_id[id(span)]
        dims = _dims(spans[i])
        fn(dims)
        _set_dims(spans[i], dims)

    def inner_args(update):
        def fn(dims):
            outer = json.loads(dims["gen_ai.tool.call.arguments"])
            outer["arguments"].update(update)
            dims["gen_ai.tool.call.arguments"] = json.dumps(outer)
        return fn

    resolves = _calls_to(w.spans, op, "cw_resolve")
    edit(resolves[0], inner_args({"query": f"Acme {CANARY} Ltd"}))
    edit(resolves[1], inner_args({"reference_type": CANARY}))      # bad enum
    edit(_calls_to(w.spans, op, "cw_get_ticket")[0], lambda d: d.update({
        "gen_ai.tool.call.result": json.dumps({"summary": CANARY})}))

    for s in w.spans:
        if s["operation_Id"] != op:
            continue
        if s["name"].startswith("invoke_agent"):
            edit(s, lambda d: d.update({"gen_ai.input.messages": json.dumps(
                [{"role": "user", "content": f"triage {CANARY}"}])}))
        elif _dims(s).get("gen_ai.tool.name") == "load_skill":
            edit(s, lambda d: d.update({
                "gen_ai.tool.call.arguments": json.dumps(
                    {"skill_name": CANARY}),
                "gen_ai.tool.call.result": f"Error: no skill {CANARY}"}))
            break
    return spans


def _upload_globs():
    import yaml
    with open(GATE, encoding="utf-8") as fh:
        steps = yaml.safe_load(fh)["jobs"]["replay"]["steps"]
    upload = [s for s in steps
              if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    return [g.strip() for g in upload[0]["with"]["path"].splitlines()
            if g.strip()]


def test_no_recorded_value_reaches_a_public_channel(tmp_path, capsys):
    w = Window(tmp_path)
    spans = _plant(w)
    summary = tmp_path / "summary.md"
    gate_json = tmp_path / "artifacts" / "gate.json"

    w.attribute(spans=spans, extra=("--summary", str(summary)))
    w.gate("--summary", str(summary), "--json", str(gate_json))
    out = capsys.readouterr()

    raw = (tmp_path / "replay.jsonl").read_text(encoding="utf-8")
    assert CANARY in raw, "the canary never reached the scored rows"
    gate = json.loads(gate_json.read_text(encoding="utf-8"))
    assert any(not r["checks"]["valid_tool_args"]["passed"]
               for r in gate), "the bad enum was not scored"

    public = {"stdout": out.out, "stderr": out.err,
              "summary": summary.read_text(encoding="utf-8")}
    # Every file the gate would upload, as the workflow names them. The
    # attribution outputs are written next to the artifacts here.
    for g in _upload_globs():
        name = os.path.basename(g)
        rx = re.compile("^" + re.escape(name).replace(r"\*", ".*") + "$")
        for root, _, files in os.walk(tmp_path):
            for f in files:
                if rx.match(f):
                    p = os.path.join(root, f)
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        public[os.path.relpath(p, tmp_path)] = fh.read()
    assert "artifacts/gate.json" in public
    assert not found(public, CANARY), found(public, CANARY)


# ------------------------------------------------- crashes in a public log

def _crash(env):
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from evalconfig import public_main\n"
            "def main():\n"
            "    raise ValueError('cannot parse %s')\n"
            "sys.exit(public_main(main))\n") % (REPO, CANARY)
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=dict(os.environ, **env))


def test_a_crash_in_ci_names_itself_without_its_message():
    r = _crash({"GITHUB_ACTIONS": "true"})
    assert r.returncode == 1
    assert CANARY not in r.stdout + r.stderr
    assert "ValueError at <string>:4" in r.stderr


def test_a_crash_locally_keeps_its_traceback():
    r = _crash({"GITHUB_ACTIONS": ""})
    assert r.returncode == 1
    assert CANARY in r.stderr and "Traceback" in r.stderr


def test_every_script_the_gate_runs_withholds_crash_messages():
    with open(GATE, encoding="utf-8") as fh:
        scripts = sorted(set(re.findall(r"python (\S+\.py)", fh.read())))
    assert len(scripts) >= 6, scripts
    for script in scripts:
        with open(os.path.join(REPO, script), encoding="utf-8") as fh:
            assert "sys.exit(public_main(main))" in fh.read(), script
