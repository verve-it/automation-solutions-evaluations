#!/usr/bin/env python3
"""
evalconfig.py — the facts that are about YOUR project, not about evaluating.

Everything here was once a constant in the middle of a script: the agent
names, which tools write, what the toolbox environment variables are called.
That works exactly once, for one project, and fails silently for the next one:

  * an agent missing from the agent list has its A2A hand-off scored as an
    ordinary tool call, and its run collapses into its caller's
  * a write tool missing from the write list is counted as a read, so a gate
    reports "0 writes, none performed" about a run that attempted several

Neither announces itself. Both are the kind of wrong that looks like a pass.

So the project-specific parts live in `eval-config.json` beside this file, and
what can be derived from the data is derived instead of configured:

  agents        every distinct `gen_ai.agent.name` in the trace IS an agent.
                The config only adds names that a partial export missed.
  write tools   MCP tools carry `annotations.readOnlyHint` and
                `destructiveHint`. Where a manifest has them, they decide.
                Names and prefixes in the config cover a server that does not.

The defaults here are deliberately empty. A default that happens to fit one
project is how the constants got embedded in the first place.
"""

from __future__ import annotations

import json
import os

CONFIG_NAME = "eval-config.json"

DEFAULTS = {
    # Extra agent names, for a trace that does not contain every agent. The
    # trace is the primary source; this is a supplement, not a whitelist.
    "agents": [],

    # Tools that change the system under test. Used for "N writes, none
    # performed" -- so a missing one understates what a replay stubbed.
    "write_tools": {"names": [], "prefixes": []},

    # For a hosted agent that resolves its tools through a Foundry toolbox:
    # the environment variables its code reads to find one.
    "toolbox_env": {"name": "", "version": ""},

    # The server_label a replay binds under. Cosmetic, but it appears in
    # traces, so it should say something true about this project.
    "replay_tool_label": "replay",
}


def repo_root():
    return os.path.dirname(os.path.abspath(__file__))


def load(path=None):
    """Config merged over the defaults. A missing file is not an error.

    Not raising on a missing file is deliberate: a fresh checkout should run,
    and every consumer degrades to deriving from the data. What is NOT
    tolerated is a silent partial merge -- a config naming `write_tools`
    without `prefixes` gets the default for `prefixes`, not a KeyError three
    calls later.
    """
    path = path or os.path.join(repo_root(), CONFIG_NAME)
    merged = json.loads(json.dumps(DEFAULTS))
    if not os.path.isfile(path):
        return merged
    with open(path, encoding="utf-8") as fh:
        loaded = json.load(fh)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def agent_names(config=None, seen=()):
    """Agents named in the config, plus every agent the trace itself shows.

    The trace is authoritative. An agent that appears in a recording is an
    agent whether or not anyone remembered to list it, and that is the case
    the old hard-coded set got wrong.
    """
    config = config if config is not None else load()
    return set(config.get("agents") or []) | {a for a in seen if a}


def is_write_tool(name, config=None, manifests=()):
    """Does this tool change the system under test?

    Order of authority: what the server says about itself, then what the
    project configured, then no.
    """
    bare = name.split("___")[-1]

    for manifest in manifests or ():
        for tool in manifest.get("tools") or []:
            if tool.get("name", "").split("___")[-1] != bare:
                continue
            hints = tool.get("annotations") or {}
            if "readOnlyHint" in hints:
                return not hints["readOnlyHint"]
            if hints.get("destructiveHint") or hints.get("idempotentHint") is False:
                return True

    config = config if config is not None else load()
    rules = config.get("write_tools") or {}
    if bare in set(rules.get("names") or []):
        return True
    return any(bare.startswith(p) for p in (rules.get("prefixes") or []))


def toolbox_env(config=None):
    config = config if config is not None else load()
    env = config.get("toolbox_env") or {}
    return env.get("name") or "", env.get("version") or ""


def replay_tool_label(config=None):
    config = config if config is not None else load()
    return config.get("replay_tool_label") or DEFAULTS["replay_tool_label"]
