#!/usr/bin/env python3
"""
preflight.py — check the gate can run, in seconds, before it creates anything.

Every one of these was found the slow way: after the replay server was
verified and clones were built, 5 to 25 minutes into a gate run, or as a
verdict that looked fine and was not:

  * an agent never deployed to the project, or deployed under another name
  * LOG_ANALYTICS_WORKSPACE_ID set to an ARM id instead of the workspace GUID
  * the identity missing Log Analytics Reader, or the role that reads
    AppGenAIContent, so arguments and results came back as placeholders
  * a hosted agent whose code does not read the toolbox variables, so the
    clone keeps its live toolbox
  * a gate-agents name with no recording, or a recording with no baseline
  * AZURE_JUDGE_DEPLOYMENT unset, so the Foundry run silently never happens

    python replay/preflight.py --cassettes cassettes --baselines baselines \\
        --gate-agents triage-analysis-agent \\
        --project-endpoint https://<acct>.services.ai.azure.com/api/projects/<p> \\
        --workspace <GUID> --judge-deployment <name> [--summary PATH]

`--offline` runs only the checks that need no Azure. Every line it prints is
an id, a name, a count or a role: nothing read from a recording (the log is
public). Exit 1 on any FAIL; a WARN does not fail.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "replay"))

import evalconfig  # noqa: E402
import run_replay as rr  # noqa: E402
from attribute_runs import index_baselines  # noqa: E402

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
BINDABLE = ("prompt", "voice", "hosted")


class Report:
    def __init__(self):
        self.lines = []

    def add(self, status, check, message):
        self.lines.append((status, check, message))
        print(f"{status:<5} {check:<12} {message}", flush=True)

    def failed(self):
        return any(s == "FAIL" for s, _, _ in self.lines)

    def markdown(self):
        out = ["### Preflight", "", "| | Check | Result |", "|---|---|---|"]
        out += [f"| {s} | {c} | {m} |" for s, c, m in self.lines]
        return "\n".join(out) + "\n"


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------ no Azure

def check_recordings(report, cassette_dir, baseline_dir, gate_agents):
    """{agent: [cassette paths]} for the cassettes the gate would replay."""
    index = index_baselines(baseline_dir)
    by_agent = {}
    for path in sorted(glob.glob(os.path.join(cassette_dir, "*.json"))):
        data = _load(path)
        agents = data.get("agents") or []
        if len(agents) != 1:
            continue
        if gate_agents and agents[0] not in gate_agents:
            continue
        by_agent.setdefault(agents[0], []).append(path)
        cid = os.path.basename(path)[:-5]
        try:
            rr.cassette_query(path)
            rr.recorded_server_label(path)
        except SystemExit as exc:
            report.add("FAIL", "recording", f"{cid}: {str(exc).splitlines()[0]}")
            continue
        if data.get("lossy"):
            report.add("FAIL", "recording", f"{cid}: lossy (a recorded result "
                       "was truncated), so its replay answers from a cut-off "
                       "result")
        rows = index.get((data.get("orchestration_id"), agents[0]), [])
        if not rows:
            report.add("FAIL", "baseline", f"{cid}: no baseline row for "
                       f"({str(data.get('orchestration_id'))[:12]}, "
                       f"{agents[0]}); it would fail attribution after the "
                       "replay")
        elif len(rows) > 1:
            report.add("FAIL", "baseline", f"{cid}: in {len(rows)} baseline "
                       f"files ({', '.join(f for f, _ in rows)})")

    for agent in sorted(gate_agents or []):
        if agent not in by_agent:
            report.add("FAIL", "recording", f"gate-agents names {agent}, but "
                       "there is no single-agent recording of it, so it "
                       "would pass untested")
    for agent, paths in sorted(by_agent.items()):
        try:
            rr.replay_agent_name(agent)
        except SystemExit:
            report.add("FAIL", "name", f"{agent}: {len(agent)} chars; "
                       f"{agent}{rr.REPLAY_AGENT_SUFFIX} is not a valid "
                       "Foundry agent name (63 max)")
            continue
        report.add("ok", "recording", f"{agent}: {len(paths)} recording(s)")
    return by_agent


def check_settings(report, args):
    if args.workspace is not None and not GUID_RE.match(args.workspace or ""):
        shape = ("an ARM resource id" if (args.workspace or "").startswith("/")
                 else f"{len(args.workspace or '')} chars")
        report.add("FAIL", "workspace", "LOG_ANALYTICS_WORKSPACE_ID is not a "
                   f"GUID ({shape}); use the workspace's Workspace ID "
                   "(customerId) from its Overview page")
    if args.judge_deployment is not None and not args.judge_deployment:
        report.add("WARN", "foundry", "AZURE_JUDGE_DEPLOYMENT is not set, so "
                   "the Foundry detail view will not be created")


# --------------------------------------------------------------- Azure

def check_workspace(report, workspace, query=None):
    """Read rights on the spans and on their content, one query each."""
    from datetime import timedelta
    if query is None:
        from azure.identity import DefaultAzureCredential
        from azure.monitor.query import LogsQueryClient
        client = LogsQueryClient(DefaultAzureCredential())

        def query(kql):
            return client.query_workspace(workspace, kql,
                                          timespan=timedelta(days=7))

    for table, role in (("AppDependencies", "Log Analytics Reader"),
                        ("AppGenAIContent", "Privileged Monitoring Data "
                                            "Reader")):
        try:
            result = query(f"{table} | take 1 | project TimeGenerated")
        except Exception as exc:
            report.add("FAIL", "workspace", f"cannot read {table} "
                       f"({rr._public_error(exc)}): the gate's "
                       f"identity needs {role} on the workspace")
            continue
        rows = sum(len(getattr(t, "rows", []) or [])
                   for t in getattr(result, "tables", None) or [])
        if not rows:
            report.add("WARN", "workspace", f"{table} has no rows in 7 "
                       "days: wrong workspace, or nothing ran")
        else:
            report.add("ok", "workspace", f"{table} readable")


def check_agents(report, agents_client, by_agent, toolbox_vars):
    name_var, version_var = toolbox_vars
    for agent in sorted(by_agent):
        try:
            details = rr.agent_version_details(agents_client, agent)
        except Exception as exc:
            report.add("FAIL", "agent", f"{agent} is not in the project "
                       f"({rr._public_error(exc)}). Deploy it, and check "
                       "azure.yaml's service `name:` is this name")
            continue
        version = getattr(details, "version", None)
        kind = rr.definition_kind(rr.definition_payload(details))
        if kind not in BINDABLE:
            report.add("FAIL", "agent", f"{agent} v{version}: kind={kind} "
                       "cannot be bound to the stub")
            continue
        if kind != "hosted":
            report.add("ok", "agent", f"{agent} v{version}: kind={kind}")
            continue
        try:
            reads, _hosts = rr.code_env_reads(agents_client, agent, version)
        except rr.CodeUnreadable as exc:
            report.add("FAIL", "agent", f"{agent} v{version}: code {exc}")
            continue
        missing = [v for v in (name_var, version_var) if v and v not in reads]
        if missing or not (name_var and version_var):
            report.add("FAIL", "agent", f"{agent} v{version}: its code does "
                       f"not read {', '.join(missing) or 'toolbox_env'}, so "
                       "a clone would keep its live toolbox. It reads: "
                       f"{', '.join(sorted(reads)) or 'nothing'}")
        else:
            report.add("ok", "agent", f"{agent} v{version}: kind=hosted, "
                       f"reads {name_var} and {version_var}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cassettes", default="cassettes")
    ap.add_argument("--baselines", default="baselines")
    ap.add_argument("--gate-agents", default="",
                    help="comma list; empty means every single-agent recording")
    ap.add_argument("--project-endpoint")
    ap.add_argument("--workspace")
    ap.add_argument("--judge-deployment")
    ap.add_argument("--summary", help="append the table here (markdown)")
    ap.add_argument("--offline", action="store_true",
                    help="only the checks that need no Azure")
    args = ap.parse_args(argv)

    gate_agents = {a.strip() for a in args.gate_agents.split(",") if a.strip()}
    report = Report()
    by_agent = check_recordings(report, args.cassettes, args.baselines,
                                gate_agents)
    check_settings(report, args)

    if not args.offline:
        if args.workspace and GUID_RE.match(args.workspace):
            check_workspace(report, args.workspace)
        if args.project_endpoint:
            from azure.ai.projects import AIProjectClient
            from azure.identity import DefaultAzureCredential
            client = AIProjectClient(endpoint=args.project_endpoint,
                                     credential=DefaultAzureCredential(),
                                     allow_preview=True)
            check_agents(report, client.agents, by_agent,
                         evalconfig.toolbox_env(rr.CONFIG))

    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(report.markdown())
    if report.failed():
        print("\npreflight failed: nothing was created. Fix the FAIL lines "
              "above and re-run.")
        return 1
    print("\npreflight ok")
    return 0


if __name__ == "__main__":
    from evalconfig import public_main
    sys.exit(public_main(main))
