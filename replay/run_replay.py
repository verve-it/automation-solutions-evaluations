#!/usr/bin/env python3
"""
run_replay.py — the agent-change gate. Stubbed tools, no live service.

This is the gate, not the smoke test. An agent under test is bound to the
replay server instead of ConnectwiseMCP, so every tool call is answered from
a recorded run:

  * no ConnectWise request is made, for reads or writes
  * a write returns the response the real write returned, and writes nothing
  * the same ticket replays identically however the live system has moved on

There is no mode in which this repo invokes an agent against real tools. The
workflow that did (`staging-replay.yml`, `microsoft/ai-agent-evals`) is gone,
and so is `--allow-live-children`: an eval that writes to a system of record
is not an eval. Everything here either replays a cassette or scores a
recorded trace.

    # one command, if the server is somewhere Foundry can reach
    python3 replay/run_replay.py --cassette cassettes/2026-09-03-4dda7f4fa5f0.json \\
        --agent triage-orchestrator --server-url https://replay.example.net/mcp

    # see exactly what it would do to the project, touch nothing
    python3 replay/run_replay.py --cassette ... --agent ... --dry-run

What it does, in order
----------------------
1. Serves the cassette (locally with --serve, or you host it).
2. Reads the agent version under test and clones its definition, replacing
   the ConnectWise toolbox binding with an MCP tool pointing at the replay
   server, bound under the server_label the RECORDING used. Everything else
   is copied verbatim -- change anything else and you are evaluating a
   different agent.
3. Creates that clone as a temporary version of `<agent>-replay` -- a
   separate agent nothing else calls, so production traffic to the agent
   under test can never reach it -- and checks that the replay agent's name
   now resolves to the clone.
4. Invokes it with the query from the cassette.
5. Collects /summary: matched prefix, first divergence, writes attempted,
   and what reached the stub per tool.
6. Deletes the temporary version. Always, including on failure.

Reachability is the one real constraint
---------------------------------------
Foundry calls the replay server; the replay server does not call Foundry. So
the URL has to be reachable FROM Azure. `localhost` will not do, and this
script refuses it rather than letting you discover that as a timeout inside an
agent run. Host it, or put a tunnel in front of it.
"""

from __future__ import annotations

import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import argparse, collections, json, re, subprocess, time, urllib.error, urllib.request, uuid
import datetime as _dt

import evalconfig


def _utcnow():
    """ISO-8601 UTC to the second, the form export_traces --since/--until takes."""
    return (_dt.datetime.now(_dt.timezone.utc)
               .replace(microsecond=0).isoformat())

CONFIG = evalconfig.load()

# The server_label to bind under when the recording does not say. It usually
# does -- see recorded_server_label() -- and the recording wins, because the
# label is not cosmetic: Foundry names every MCP tool `<server_label>___<tool>`,
# so a different label is a different tool name, a different trajectory and a
# different contract from the one the agent's skills describe.
REPLAY_TOOL_LABEL = evalconfig.replay_tool_label(CONFIG)
TEMP_MARKER = "eval-replay-temp"

# The clone is a version of a SEPARATE agent, never of the agent under test.
#
# A new version of the production agent is what that agent's name resolves to
# while it exists: the SDK documents that only DRAFT versions are "excluded
# from default 'latest' resolution", and create_version_from_code -- the only
# way to create a hosted version -- takes no draft flag. So a clone of
# connectwise-operations-agent would receive every production call that
# reaches that agent by name for as long as the replay ran, and answer it from
# the recording: production writes, silently not performed. And pinning the
# production agent with a version selector would not help, because then the
# replay's own by-name call would reach the production version and its live
# toolbox. Neither way is safe.
#
# Under its own name nothing but this script ever calls it, so "latest" IS the
# clone -- checked before invoking, see routing_problem() -- and production
# routing is never touched.
REPLAY_AGENT_SUFFIX = "-replay"
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def replay_agent_name(agent):
    """`connectwise-operations-agent` -> `connectwise-operations-agent-replay`.

    Foundry agent names are 1-63 characters, alphanumeric at both ends,
    hyphens between. A name that cannot take the suffix is refused rather
    than truncated: two agents truncating to one replay name would share it.
    """
    name = f"{agent}{REPLAY_AGENT_SUFFIX}"
    if not _AGENT_NAME_RE.match(name):
        raise SystemExit(
            f"{agent!r} cannot take the replay suffix: {name!r} is not a "
            "valid agent name (1-63 characters, alphanumeric at both ends, "
            "hyphens between). Pass --replay-agent with a name of your own.")
    return name


def recorded_server_label(cassette_path):
    """The server_label the recorded run's MCP tools were bound under.

    The recording names every MCP call `<server_label>___<tool>`, so the label
    is in the cassette and does not have to be configured or guessed. A
    replay bound under any other label shows the agent differently named
    tools than its skills and tool_search results describe -- a changed
    contract, so a changed trajectory, blamed on the agent.

    Falls back to eval-config.json's replay_tool_label only when the
    recording has no prefixed call at all. Refuses a recording that used more
    than one MCP server: one stub cannot answer under two labels.
    """
    with open(cassette_path, encoding="utf-8") as fh:
        data = json.load(fh)
    labels = sorted({i["tool"].rsplit("___", 1)[0]
                     for i in data.get("interactions") or []
                     if "___" in (i.get("tool") or "")})
    if len(labels) > 1:
        raise SystemExit(
            f"{os.path.basename(cassette_path)} records calls to "
            f"{len(labels)} MCP servers ({', '.join(labels)}). One replay "
            "toolbox answers under one server_label, so the others would be "
            "renamed and every call to them would diverge.")
    return labels[0] if labels else REPLAY_TOOL_LABEL


def agent_references(payload, known_agents, own_name):
    """(variable, agent) pairs where the clone's environment names another
    agent in the project.

    A hosted agent reaches another over A2A by name -- the orchestrator's
    TRIAGE_ANALYSIS_AGENT_NAME and friends -- and a name resolves to that
    agent's production version, bound to the live toolbox. The replay stubs
    only the agent it clones, so a clone that can name a child would send the
    child's calls, writes included, to ConnectWise. Refused before anything
    is created, not discovered in the trace afterwards.
    """
    env = (payload or {}).get("environment_variables") or {}
    return sorted((k, v) for k, v in env.items()
                  if isinstance(v, str) and v in known_agents and v != own_name)


def replay_agent_exists(agents, replay_agent):
    """True, False, or SystemExit when the answer cannot be had.

    Asked before anything is created. The replay agent is made on first use
    -- adding a first version creates an agent, which is how Microsoft's own
    hosted-agent sample creates one -- and deleted after the run when this
    run made it, so nothing has to be set up per agent and no agent is left
    behind for anything to call by name.
    """
    try:
        agents.get(replay_agent)
        return True
    except Exception as exc:
        status = getattr(exc, "status_code", None) or getattr(
            getattr(exc, "response", None), "status_code", None)
        if status == 404 or type(exc).__name__ == "ResourceNotFoundError":
            return False
        raise SystemExit(f"cannot read {replay_agent} to learn whether it "
                         f"exists: {type(exc).__name__}: {exc}. Nothing was "
                         "created.")


# A hosted version is provisioned after create_version_from_code returns, and
# create_session on it is refused until then: `(agent_version_not_ready)
# Agent version is still being provisioned`. Seen on the first gate run in
# staging, immediately after the clone was created.
NOT_READY = "agent_version_not_ready"
READY_TIMEOUT_S = 600
READY_POLL_S = 10
_sleep = time.sleep


def _is_not_ready(exc):
    code = getattr(getattr(exc, "error", None), "code", None)
    return code == NOT_READY or NOT_READY in str(exc)


def create_session_when_ready(agents, replay_agent, version_indicator,
                              timeout_s=None, poll_s=None):
    """create_session, waiting while the new version is provisioned. Any
    other error, and still not ready after `timeout_s`, is raised."""
    timeout_s = READY_TIMEOUT_S if timeout_s is None else timeout_s
    poll_s = READY_POLL_S if poll_s is None else poll_s
    waited = 0
    while True:
        try:
            return agents.create_session(agent_name=replay_agent,
                                         version_indicator=version_indicator)
        except Exception as exc:
            if not _is_not_ready(exc) or waited >= timeout_s:
                raise
        if waited == 0:
            print(f"  {replay_agent} is still being provisioned; waiting "
                  f"up to {timeout_s}s", flush=True)
        _sleep(poll_s)
        waited += poll_s


def routing_problem(agents, replay_agent, temp_version):
    """Why calling `replay_agent` by name might not reach the clone, or None.

    Checked after the clone is created and before anything is invoked, so a
    wrong answer costs a deleted version, not a call to the wrong agent.
    """
    try:
        details = agents.get(replay_agent)
    except Exception as exc:
        return f"cannot read {replay_agent}: {type(exc).__name__}: {exc}"
    endpoint = getattr(details, "agent_endpoint", None)
    selector = getattr(endpoint, "version_selector", None) if endpoint else None
    rules = getattr(selector, "version_selection_rules", None) if selector else None
    # Foundry gives every new agent a default selector: one rule, "@latest",
    # 100% of traffic. That routes by name to the newest version, which is
    # what the check below confirms is the clone, so it is not a problem. A
    # rule pinning the clone's own version is equally safe. Anything else --
    # another version, or traffic split -- can reach a version that is not
    # the clone.
    live = [r for r in (rules or [])
            if str(getattr(r, "traffic_percentage", 100)) not in ("0", "0.0")]
    if live and all(str(getattr(r, "agent_version", "")).lstrip("v")
                    in ("@latest", str(temp_version)) for r in live):
        rules = None
    if rules:
        routed = ", ".join(
            f"v{getattr(r, 'agent_version', '?')} "
            f"({getattr(r, 'traffic_percentage', '?')}%)" for r in rules)
        return (f"{replay_agent} has a version selector ({routed}), so its "
                "name does not resolve to the newest version. Remove the "
                "selector: nothing but a replay should ever call this agent.")
    latest = getattr(getattr(getattr(details, "versions", None), "latest",
                             None), "version", None)
    if str(latest) != str(temp_version):
        return (f"{replay_agent} resolves to v{latest}, not the clone "
                f"v{temp_version}. Another replay may be running against the "
                "same replay agent; the gate serialises its own runs, a local "
                "run beside it does not.")
    return None


# ------------------------------------------------------------- reachability

LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")


def is_locally_scoped(url):
    """True when Azure could not reach this URL.

    Foundry calls the replay server, not the other way round. A localhost URL
    produces a tool call that times out inside an agent run, which surfaces as
    an agent failure rather than as a configuration mistake.
    """
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".local")


# ------------------------------------------------------------------ serving

def serve(cassette, port, tool_defs, journal):
    """Start replay_server.py as a subprocess and wait for it to answer."""
    cmd = [sys.executable, os.path.join(REPO_ROOT, "replay", "replay_server.py"),
           cassette, "--port", str(port)]
    if tool_defs:
        cmd += ["--tool-defs", tool_defs]
    if journal:
        cmd += ["--journal", journal]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://localhost:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base, timeout=1).read()
            return proc, base
        except urllib.error.URLError:
            time.sleep(0.2)
        except Exception:
            time.sleep(0.2)
    proc.kill()
    raise SystemExit(f"replay server did not come up on {base}")


def summary_url(server_url):
    """Where the journal for this replay lives.

    Locally the server is one cassette on one port and /summary is enough.
    Hosted, the cassette is a path segment -- https://host/mcp/<id> -- and the
    journal for it is https://host/summary/<id>. Deriving it here keeps the
    caller from having to pass two URLs that can disagree.
    """
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(server_url)
    parts = [p for p in parsed.path.split("/") if p]
    if "mcp" in parts:
        parts[len(parts) - 1 - parts[::-1].index("mcp")] = "summary"
    else:
        parts.append("summary")
    return urlunparse(parsed._replace(path="/" + "/".join(parts), query="",
                                      fragment=""))


def summary(base, token=None, session=None):
    req = urllib.request.Request(summary_url(base))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if session:
        req.add_header("Mcp-Session-Id", session)
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as exc:
        # The body says why -- `replay state unavailable: ...` on a 503 --
        # and a bare traceback drops it.
        body = exc.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"GET {summary_url(base)} returned {exc.code}: "
                         f"{body or exc.reason}")


# ------------------------------------------------------------------- binding

def agent_version_details(agents, name, version=None):
    """The AgentVersionDetails, which is what carries `definition`.

    `agents.get(name)` returns AgentDetails -- the agent, not a version of it
    -- and it has no `definition`. The versions hang off it, and the one to
    clone is `versions.latest` unless a version was named.
    """
    if version:
        return agents.get_version(name, version)
    details = agents.get(name)
    latest = getattr(getattr(details, "versions", None), "latest", None)
    if latest is None:
        raise RuntimeError(
            f"agent {name!r} has no versions to clone "
            f"({type(details).__name__} carried none)")
    return latest


def invoke_agent(client, name, session_id, query):
    """Run the agent so the replay actually happens.

    A session binds a run to an agent version; it does not run anything. The
    run goes through the agent's own OpenAI-compatible endpoint, which
    `get_openai_client(agent_name=...)` returns.

    How a response is pinned to an existing session is the one part of this
    chain not confirmed against the SDK offline -- `responses.create` has no
    session parameter, so the id is passed through `extra_body`, which is how
    an OpenAI client carries anything the schema does not name. If the service
    ignores or rejects it, the replay still ran against the temporary version;
    the journal on the replay server is the record either way, and that is
    what the verdict is computed from.
    """
    try:
        openai_client = client.get_openai_client(agent_name=name)
    except Exception as exc:
        print(f"\nWARNING  could not open the agent endpoint: {exc}")
        print("WARNING  the agent was not invoked, so the journal below is "
              "whatever was already there.")
        return None

    extra = {"agent_session_id": session_id} if session_id else {}
    try:
        response = openai_client.responses.create(
            model="", input=query, extra_body=extra)
        print("invoked; response id "
              f"{getattr(response, 'id', '?')}")
        return response
    except Exception as exc:
        print(f"\nWARNING  invoke failed: {type(exc).__name__}: "
              f"{str(exc)[:200]}")
        print("WARNING  the temporary version was still created and is still "
              "cleaned up. If this is a schema complaint about "
              "agent_session_id, the session is bound to the version already "
              "and the id may not need passing at all.")
        return None


def read_journal(base, token, session):
    """The journal for this replay, whichever bucket it landed in.

    If the MCP client forwarded our session header, the journal is under that
    id. If it did not, every call fell into the server's shared default and
    the id we chose has nothing in it. Rather than guess which, read ours and
    fall back -- and say which answered, because the difference is whether
    two replays can run at once.
    """
    ours = summary(base, token, session)
    if ours.get("replayed_calls"):
        ours["session_honoured"] = True
        return ours
    shared = summary(base, token)
    shared["session_honoured"] = False
    if shared.get("replayed_calls"):
        print("\nNOTE: the MCP client did not forward Mcp-Session-Id, so this "
              "replay used the server's shared session. It is correct on its "
              "own; two replays at once would consume each other's queue.")
        return shared
    return ours


def replay_tools(server_url, models, token=None, session=None,
                 server_label=None):
    """The only difference between the agent under test and production.

    The token travels as a header rather than in the URL. `server_url` is
    stored on the agent version and repeated in every span, so a token in the
    query string would end up in App Insights and in anything exported from
    it.

    The session id is ours, not the server's. The hosted server issues one on
    `initialize` and keys the cursor and journal by it, which is what stops
    two replays consuming each other's queue -- but then only the MCP client
    knows the id, and this script is not the MCP client. Sending a known one
    means the journal can be read back afterwards. Without it, `/summary`
    answers for a session nobody used and reports a replay that did nothing.
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["Mcp-Session-Id"] = session
    kwargs = {"headers": headers} if headers else {}
    return [models.MCPTool(
        server_label=server_label or REPLAY_TOOL_LABEL,
        server_url=server_url,
        server_description="Recorded ConnectWise responses. No live service.",
        require_approval="never",
        **kwargs,
    )]


# ------------------------------------------------- binding, by agent kind
#
# An agent's tools live somewhere different depending on what kind of agent it
# is, so there is no one way to point one at the replay server:
#
#   prompt, voice   `tools` is a field of the definition. Swap it.
#   hosted          the definition has NO tools -- the code is uploaded and
#                   the binding is inside it. The definition does carry
#                   `environment_variables`, so the lever is a variable the
#                   code reads, and the new version must be created from the
#                   SAME code bytes through the multipart endpoint.
#   workflow,       neither tools nor environment. Nothing to rebind.
#   external
#
# Sending a definition of the wrong shape gets `code_configuration is not
# supported with application/json`, which explains nothing, so each kind is
# handled explicitly and an unknown one is refused by name.

# How the hosted agents in this project reach ConnectWise. Not a guess:
# --inspect-code reported every one of them reading these, and the only host
# in their code is ai.azure.com -- they resolve a Foundry TOOLBOX by name and
# version, and never hold an endpoint at all.
#
# Which is the better arrangement to replay against: a temporary toolbox
# pointing at the replay server, and two environment variables naming it. No
# code change, and the agent cannot tell the difference.
TOOLBOX_NAME_VAR, TOOLBOX_VERSION_VAR = evalconfig.toolbox_env(CONFIG)

# Kept for a hosted agent that does hold a URL. Setting a variable nothing
# reads changes nothing, so these cost only clarity -- none are set unless
# asked for.
DEFAULT_REPLAY_VARS = ()


def definition_payload(version_details):
    d = version_details.definition
    return d.as_dict() if hasattr(d, "as_dict") else dict(d or {})


def definition_kind(payload):
    if not isinstance(payload, dict):
        payload = (payload.as_dict() if hasattr(payload, "as_dict")
                   else dict(payload or {}))
    return payload.get("kind") or "unknown"


class PromptBinding:
    """`tools` is part of the definition, so the swap is the definition."""

    kinds = ("prompt", "voice")

    def rebind(self, payload, *, server_url, models, token, session,
               server_label=None):
        clone = dict(payload)
        clone["tools"] = [t.as_dict() if hasattr(t, "as_dict") else t
                          for t in replay_tools(server_url, models, token,
                                                session, server_label)]
        return clone

    def create(self, agents, name, definition, *, description, metadata,
               base_version, source_agent=None):
        """`name` is the replay agent; the definition already came from the
        agent under test, so there is nothing else to fetch."""
        return agents.create_version(agent_name=name, definition=definition,
                                     description=description,
                                     metadata=metadata)

    def describe_plan(self, payload):
        return (f"swap {len(payload.get('tools') or [])} tool(s) in the "
                "definition")


class HostedBinding:
    """A temporary toolbox, and two environment variables naming it.

    These agents resolve ConnectWise through a Foundry toolbox by name and
    version -- CONNECTWISE_TOOLBOX_NAME and CONNECTWISE_TOOLBOX_VERSION -- and
    hold no endpoint themselves. So the replay does not need to change any
    code: it creates a toolbox whose one tool points at the replay server,
    names that toolbox in the clone's environment, and deletes it afterwards.

    The code is re-uploaded byte for byte from `download_code`, so the clone
    differs from its base version by exactly those variables and nothing else
    -- the same guarantee the prompt path gives by copying every other field.

    The replay server's bearer token and session go in the TOOLBOX's headers,
    not the agent's environment: the agent authenticates to Foundry, and
    Foundry is what calls the replay server.
    """

    kinds = ("hosted",)

    def __init__(self, variables=(), name_var=None, version_var=None):
        self.variables = tuple(variables)
        self.name_var = name_var or TOOLBOX_NAME_VAR
        self.version_var = version_var or TOOLBOX_VERSION_VAR
        self.toolbox = None
        self._client = None

    def prepare(self, client, *, server_url, token, session, models, label,
                server_label=None):
        """Create the toolbox the clone will name. Torn down in teardown()."""
        if not (self.name_var and self.version_var):
            raise SystemExit(
                "this agent is hosted, so it finds its tools through "
                "something its code reads from the environment -- but "
                "eval-config.json does not say which variables.\n\n"
                "Find out, then put the names in toolbox_env:\n"
                f"    {_py()} replay/run_replay.py --describe --inspect-code")
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if session:
            headers["Mcp-Session-Id"] = session
        tool = models.MCPToolboxTool(
            server_label=server_label or REPLAY_TOOL_LABEL,
            server_url=server_url,
            server_description="Recorded ConnectWise responses. No live "
                               "service.",
            headers=headers or None)
        created = client.toolboxes.create_version(
            name=label, tools=[tool],
            description="temporary: stubbed-tool replay",
            metadata={"purpose": TEMP_MARKER})
        self._client = client
        self.toolbox = (getattr(created, "name", label),
                        str(getattr(created, "version", "1")))
        print(f"toolbox    : {self.toolbox[0]} v{self.toolbox[1]} -> "
              f"{server_url}")
        return self.toolbox

    def rebind(self, payload, *, server_url, token, session, models=None,
               server_label=None):
        if self.toolbox is None:
            raise RuntimeError("prepare() must run before rebind(): the clone "
                               "names a toolbox that has to exist first")
        clone = dict(payload)
        env = dict(clone.get("environment_variables") or {})
        env[self.name_var], env[self.version_var] = self.toolbox
        for name in self.variables:
            env[name] = server_url
        clone["environment_variables"] = env
        return clone

    def teardown(self):
        if self.toolbox and self._client is not None:
            try:
                self._client.toolboxes.delete(self.toolbox[0])
                print(f"deleted toolbox {self.toolbox[0]}")
            except Exception as exc:
                # Worth saying: a toolbox left behind points at a replay
                # server and would answer a real agent with recorded data.
                print(f"WARNING  could not delete toolbox "
                      f"{self.toolbox[0]}: {exc}")

    def create(self, agents, name, definition, *, description, metadata,
               base_version, source_agent=None):
        """Upload the code of `source_agent` v`base_version` as a version of
        `name`, the replay agent -- byte for byte, so the clone differs only
        in the environment variables rebind() set."""
        import io
        code = io.BytesIO(b"".join(
            agents.download_code(source_agent or name,
                                 agent_version=str(base_version))))
        # The SDK documents that the stream "must expose a name attribute ...
        # and that name must end with .zip"; unnamed, the multipart part is
        # sent as `code`, which the service may refuse.
        code.name = f"{source_agent or name}-v{base_version}.zip"
        code.seek(0)
        return agents.create_version_from_code(
            agent_name=name, definition=definition, code=code,
            description=description, metadata=metadata)

    def describe_plan(self, payload):
        named = [self.name_var, self.version_var, *self.variables]
        return ("point " + ", ".join(named)
                + " at a temporary toolbox and re-upload the code unchanged")


def prepare_binding(binding, **kwargs):
    """Some bindings need something to exist before the clone can name it."""
    if hasattr(binding, "prepare"):
        return binding.prepare(**kwargs)
    return None


def teardown_binding(binding):
    if hasattr(binding, "teardown"):
        binding.teardown()


def binding_for(payload, variables=DEFAULT_REPLAY_VARS):
    kind = definition_kind(payload)
    for binding in (PromptBinding(), HostedBinding(variables)):
        if kind in binding.kinds:
            return binding
    raise SystemExit(
        f"agent definition kind={kind!r} has neither `tools` nor "
        "`environment_variables`, so there is nothing to point at the replay "
        "server.\n\n"
        "Replayable kinds: prompt and voice (tools in the definition), hosted "
        "(an environment variable its code reads). See what this one has:\n"
        f"    {_py()} replay/run_replay.py --describe --agent <name>")


def describe_agent(agents, name, inspect_code=False):
    """What kind of agent this is and where its tools come from. Read only.

    Environment variable NAMES are printed, never values: they are the likely
    home of an endpoint and also the likely home of a secret.
    """
    details = agents.get(name)
    latest = getattr(getattr(details, "versions", None), "latest", None)
    if latest is None:
        print(f"{name}: no versions")
        return None

    payload = definition_payload(latest)
    kind = definition_kind(payload)
    version = getattr(latest, "version", "?")
    print(f"\n{name}  version {version}  kind={kind}")

    tools = payload.get("tools")
    if tools is not None:
        print(f"  tools ({len(tools)}) — in the DEFINITION, so a replay can "
              "swap them:")
        for tool in tools:
            bits = {k: v for k, v in tool.items()
                    if k in ("type", "server_label", "server_url",
                             "toolbox_name", "connection_id")}
            print(f"    {bits}")
    else:
        print("  tools: not part of this definition kind")

    env = payload.get("environment_variables")
    if env is not None:
        print(f"  environment variables ({len(env)}) — NAMES ONLY, values may "
              "be secrets:")
        for key in sorted(env):
            print(f"    {key}")

    code = payload.get("code_configuration")
    if code:
        print(f"  code: runtime={code.get('runtime')} "
              f"entry_point={code.get('entry_point')}")
        if inspect_code:
            report_code_binding(agents, name, version)
        else:
            print("  the tool binding lives in that code — --inspect-code "
                  "reads it")

    other = sorted(k for k in payload
                   if k not in ("kind", "tools", "environment_variables",
                                "code_configuration"))
    print(f"  other definition fields: {', '.join(other) or 'none'}")
    return payload


def report_code_binding(agents, name, version):
    """How the uploaded code reaches its MCP server. Read only, nothing saved.

    The whole hosted path turns on one question -- does the code take its
    endpoint from an environment variable, or is it fixed? -- and the code is
    downloadable, so it is a question with an answer rather than a guess.

    Only the shape is printed: which environment variables are read and which
    hosts appear. Not the source, which is the customer's, and not any value.
    """
    import io
    import re
    import zipfile

    try:
        blob = b"".join(agents.download_code(name, agent_version=str(version)))
    except Exception as exc:
        print(f"  code: cannot download ({type(exc).__name__}: "
              f"{str(exc)[:90]})")
        return

    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        print("  code: downloaded bytes are not a zip")
        return

    env_reads, hosts = set(), set()
    for item in archive.namelist():
        if not item.endswith(".py"):
            continue
        try:
            text = archive.read(item).decode("utf-8", "replace")
        except Exception:
            continue
        env_reads.update(re.findall(
            r"""(?:environ(?:\.get)?\(|getenv\()\s*["']([A-Z0-9_]+)["']""",
            text))
        hosts.update(re.findall(r"https?://([A-Za-z0-9.\-]+)", text))

    print(f"  code reads {len(env_reads)} environment variable(s):")
    for key in sorted(env_reads):
        print(f"    {key}")
    if hosts:
        print("  hosts appearing in the code:")
        for host in sorted(hosts):
            print(f"    {host}")
    if not env_reads:
        print("  nothing read from the environment — the endpoint is fixed in "
              "the code, so a replay needs a code change, not a variable")


def refuse_unstubbed_children(cassette_path, agents_in_cassette):
    """A multi-agent orchestration cannot be fully stubbed. Say so, loudly.

    The orchestrator reaches its children over A2A, and it names them --
    TRIAGE_ANALYSIS_AGENT_NAME and friends -- by NAME. A name resolves to the
    agent's own default version, not to anything this script created, and that
    version's environment still points at the real ConnectWise toolbox.

    So replaying an orchestration would stub the orchestrator's own calls and
    send every child's calls to live ConnectWise -- including its writes. That
    is precisely the thing this whole apparatus exists to prevent, and it
    would not be visible in the result: the journal would show the
    orchestrator's calls matching, and say nothing about the rest.

    The single-agent cassettes replay safely today. Fixing this for
    orchestrations needs the children addressable by version, which the agent
    code decides, not this script.

    There is deliberately no override. This refusal is the only thing standing
    between a multi-agent cassette and a live write, and a flag that disables
    it is one hurried run away from being set.
    """
    children = list(agents_in_cassette[1:])
    if not children:
        return
    raise SystemExit(
        f"{os.path.basename(cassette_path)} records "
        f"{len(agents_in_cassette)} agents: "
        f"{', '.join(agents_in_cassette)}.\n\n"
        "Only the entry agent can be stubbed. It reaches the others over A2A "
        "BY NAME, a name resolves to that agent's own default version, and "
        "those versions still point at the real ConnectWise toolbox -- so "
        "their calls, including writes, would go to the live service.\n\n"
        "The gate would not show it: the journal only sees the entry agent.\n\n"
        "Single-agent cassettes replay fully stubbed"
        + (":\n" + "\n".join(single_agent_cassettes(cassette_path))
           if single_agent_cassettes(cassette_path) else ".")
        + "\n\nThere is no override. Nothing in this repo may reach "
          "ConnectWise; gate an orchestration by making its children "
          "addressable by version, which is the agent code's decision.")


def single_agent_cassettes(beside):
    """The cassettes that CAN be replayed fully stubbed, found not listed."""
    directory = os.path.dirname(os.path.abspath(beside))
    found = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                agents = (json.load(fh).get("agents") or [])
        except Exception:
            continue
        if len(agents) == 1:
            found.append(f"    {os.path.join(directory, name)}  ({agents[0]})")
    return found


def cassette_agent(cassette_path):
    """The agent the recording was of.

    `agents` is every agent that appeared, in first-seen order, so the first
    one is the entry point -- the orchestrator on a full triage, or the
    operations agent on a cassette recorded from that agent alone. Nobody
    should have to know which: the recording does.

    Child agents are not a choice here. A multi-agent orchestration replays by
    running its entry agent; the children are reached over A2A, which
    make_cassette.py deliberately leaves out of the toolbox so the callee gets
    replayed as its own agent run rather than answered from the cassette.
    """
    with open(cassette_path, encoding="utf-8") as fh:
        data = json.load(fh)
    agents = data.get("agents") or []
    if not agents:
        raise SystemExit(
            f"{cassette_path} records no agent name, so there is nothing to "
            "replay. Pass --agent. (A cassette gets its agents from "
            "gen_ai.agent.name on the recorded spans.)")
    return agents[0], agents


def _py():
    """`python` on Windows, `python3` elsewhere.

    Printing a command the reader cannot run is the same bug as `openssl` and
    `export` were: correct advice for the wrong machine.
    """
    return "python" if os.name == "nt" else "python3"


def _runner():
    return ".\\tasks.ps1 cassettes" if os.name == "nt" else "make cassettes"


def cassette_query(cassette_path):
    """The input the recorded run was given, so the replay asks the same thing.

    Retyping it is not equivalent. A slightly different question produces a
    divergence the gate scores as the agent's, which is the failure this
    whole apparatus exists to avoid.
    """
    with open(cassette_path, encoding="utf-8") as fh:
        data = json.load(fh)
    for key in ("query", "input", "prompt"):
        if data.get(key):
            return data[key]

    # A cassette built before make_cassette.py recorded the input has no
    # `query` key at all; one built since, from a recording with no user
    # message on its invoke span, has the key set to null. Same symptom, two
    # different fixes, and telling them apart costs one `in`.
    if "query" not in data:
        raise SystemExit(
            f"{os.path.basename(cassette_path)} predates the recording of "
            "agent input, so it carries no query.\n\n"
            "Cassettes are derived from the committed traces -- rebuild "
            f"them:\n    {_runner()}\n\n"
            f"(or `{_py()} replay/make_cassette.py traces/<trace>.json -o "
            "cassettes` for one.)")

    inter = data.get("interactions") or []
    raise SystemExit(
        f"{os.path.basename(cassette_path)} was rebuilt but its recording "
        f"carries no user input ({len(inter)} interactions).\n\n"
        "make_cassette.py takes the query from gen_ai.input.messages on the "
        "entry agent's invoke_agent span. A trace exported without that "
        "attribute has nothing to take, so pass --query with the input the "
        "run was given.")


# ---------------------------------------------------------------- reporting

def verdict(s, allow_divergence_after=None):
    """Did this replay reproduce the recorded run?

    Divergence is not automatically failure -- an agent change that removes a
    wasted call SHOULD diverge, and that is the improvement you wanted. What
    matters is where it diverged and whether it attempted writes the recorded
    run did not.
    """
    matched = s.get("matched_prefix", 0)
    total = s.get("recorded_interactions", 0)
    div = s.get("first_divergence")
    lines = [
        f"  recorded interactions : {total}",
        f"  matched prefix        : {matched}",
        f"  writes attempted      : {s.get('writes_attempted', 0)} "
        f"(none performed)",
    ]
    if div:
        lines.append(f"  first divergence      : seq {div.get('seq')} "
                     f"{str(div.get('tool','')).split('___')[-1]}")
    ok = True
    if allow_divergence_after is not None and matched < allow_divergence_after:
        lines.append(f"  FAIL: diverged before seq {allow_divergence_after}")
        ok = False
    return ok, "\n".join(lines)


def recorded_local_tools(cassette_path):
    """Bare names the agent runs itself, which never reach the replay server.

    Every other tool in a replay's trace must have reached the stub. Never
    inferred from a missing prefix, because a write sent to ConnectWise through
    a locally built client has no prefix either. Instead, the union of:

      * eval-config.json's local_tools (load_skill, tool_search, ...);
      * every name this recording called unprefixed;
      * every name any other recording of the same agent, beside it, called
        unprefixed -- so a local tool this recording happened not to use does
        not make an unchanged agent look like a bypass.

    None when the cassette cannot be read: attribution refuses a manifest
    without the list rather than guessing.
    """
    def unprefixed(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return (data.get("agents") or [None])[0], {
            i["tool"] for i in data.get("interactions") or []
            if i.get("tool") and "___" not in i["tool"]}

    try:
        agent, names = unprefixed(cassette_path)
    except (OSError, ValueError):
        return None
    names |= set(evalconfig.local_tools(CONFIG))
    directory = os.path.dirname(os.path.abspath(cassette_path))
    for other in sorted(os.listdir(directory)):
        path = os.path.join(directory, other)
        if not other.endswith(".json") or os.path.samefile(path, cassette_path):
            continue
        try:
            other_agent, other_names = unprefixed(path)
        except (OSError, ValueError, AttributeError, TypeError):
            continue
        if other_agent == agent:
            names |= other_names
    return sorted(names)


def journal_tools(journal):
    """{bare tool name: calls} from the replay server's journal, or None when
    the summary carried no journal to count."""
    if journal is None:
        return None
    counts = collections.Counter(
        str(e.get("tool") or "").rsplit("___", 1)[-1] for e in journal)
    return dict(sorted(counts.items()))


def recorded_identity(cassette_path):
    """(orchestration_id, entry agent) of the run a cassette recorded.

    A replay is a new invocation, so App Insights gives it a new operation_Id,
    and a baseline is keyed by the id of the RECORDED run. Without this pair
    the gate cannot say which recording a replayed row is a replay of, and
    every row lands in "new runs, not compared" -- which is a gate that
    cannot fail. `replay/attribute_runs.py` re-keys on it.

    None for either when the cassette cannot be read: the manifest is still
    worth writing, and attribution refuses a manifest without it.
    """
    try:
        with open(cassette_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, None
    agents = data.get("agents") or [None]
    return data.get("orchestration_id"), agents[0]


def write_manifest(path, args, base_version, temp_version, s, run=None):
    """Record what was actually tested, because the agent version will not.

    Foundry evaluation objects are scoped to the PROJECT, not to an agent --
    `evals.create(name=...)` takes a dataset, not an agent id. So nothing an
    eval stores depends on the temporary version surviving, and deleting it
    loses no results.

    What does carry the temporary version's identity is the TRACE: App
    Insights records gen_ai.agent.id and its version for every span. Delete
    the version and that id resolves to nothing, so six weeks later a trace
    names an agent that cannot be looked up and nothing says what it was a
    clone of.

    This file is that record. Name any eval built from this run after
    `base_version` -- the thing under test -- rather than after the ephemeral
    clone, which is a fixture.

    `run` carries the window this replay occupied and the ids the service
    handed back. A gate exports traces afterwards and has to select THIS
    run's spans: a flat `--hours 1` also sweeps up whatever else the project
    produced in that hour, which scores unrelated traffic as if the agent
    change had caused it. `started_utc`/`finished_utc` bound the export, and
    `replay/attribute_runs.py` then picks the run out exactly by
    `(agent, temp_version)` and re-keys it to `recorded_orchestration_id`.
    """
    recorded_op, recorded_agent = recorded_identity(args.cassette)
    payload = {
        "agent": args.agent,
        "base_version": str(base_version),
        "temp_version": str(temp_version),
        "recorded_orchestration_id": recorded_op,
        "recorded_agent": recorded_agent,
        "temp_version_deleted": True,
        "cassette": os.path.basename(args.cassette),
        "cassette_id": s.get("cassette"),
        "server_url": args.server_url,
        "tools": "stubbed — no ConnectWise request, no write performed",
        "replayed_utc": _utcnow(),
        "replay_session": s.get("session"),
        "session_honoured": s.get("session_honoured"),
        "matched_prefix": s.get("matched_prefix"),
        "recorded_interactions": s.get("recorded_interactions"),
        # What reached the replay server, per tool (bare names). The trace
        # is checked against it before scoring: fewer calls there means App
        # Insights has not ingested them yet, and MORE means calls went
        # somewhere other than the stub. A total is not enough -- local tools
        # such as load_skill are in the trace and never reach the server.
        "replayed_calls": s.get("replayed_calls"),
        "journal_tools": journal_tools(s.get("journal")),
        "local_tools": recorded_local_tools(args.cassette),
        "writes_attempted": s.get("writes_attempted"),
        "first_divergence": s.get("first_divergence"),
        "suggested_eval_name": f"replay-{args.agent}-v{base_version}",
    }
    payload.update(run or {})
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    return payload


def describe(args):
    """--describe: what the project actually contains. Creates nothing."""
    if not args.project_endpoint:
        sys.exit("--project-endpoint or AZURE_AI_PROJECT_ENDPOINT is required")

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    client = AIProjectClient(endpoint=args.project_endpoint,
                             credential=DefaultAzureCredential(),
                             allow_preview=True)
    agents = client.agents

    names = [args.agent] if args.agent else None
    if not names and args.cassette:
        _entry, names = cassette_agent(args.cassette)
    if not names:
        names = sorted(a.name for a in agents.list())
        print(f"{len(names)} agent(s) in this project")

    for name in names:
        try:
            describe_agent(agents, name, inspect_code=args.inspect_code)
        except Exception as exc:
            print(f"\n{name}: cannot read ({type(exc).__name__}: "
                  f"{str(exc)[:120]})")
    print("\nnothing was created or changed.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cassette", help="the recording to replay. Not needed "
                                       "with --describe.")
    ap.add_argument("--agent", help="agent name under test. Defaults to the "
                                    "agent the cassette was recorded from.")
    ap.add_argument("--agent-version", help="base version to clone; "
                                            "default is the latest")
    ap.add_argument("--replay-agent",
                    help="the agent the clone is created under. Default: the "
                         f"agent under test + {REPLAY_AGENT_SUFFIX!r}. Never "
                         "the agent under test itself -- see "
                         "REPLAY_AGENT_SUFFIX.")
    ap.add_argument("--server-url", help="where FOUNDRY reaches the replay "
                                         "server. Not localhost.")
    ap.add_argument("--serve", action="store_true",
                    help="run replay_server.py locally as a subprocess")
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--tool-defs", default="tool_manifests/")
    ap.add_argument("--token", default=os.environ.get("REPLAY_TOKEN"),
                    help="bearer token the hosted replay server requires. "
                         "Sent as a header, never in the URL.")
    ap.add_argument("--journal", default="artifacts/replay-journal.json")
    ap.add_argument("--manifest", default="artifacts/replay-run.json",
                    help="provenance for this replay. Written even on "
                         "failure -- see the note on the temporary version.")
    ap.add_argument("--query", help="override the agent input")
    ap.add_argument("--min-matched-prefix", type=int,
                    help="fail if the replay diverges before this call")
    ap.add_argument("--project-endpoint",
                    default=os.environ.get("AZURE_AI_PROJECT_ENDPOINT"))
    ap.add_argument("--allow-local", action="store_true",
                    help="skip the reachability check (for testing this "
                         "script, not for a real replay)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the binding that would be created and exit")
    ap.add_argument("--describe", action="store_true",
                    help="print what kind each agent is and where its tools "
                         "come from, then exit. Reads only; creates nothing.")
    ap.add_argument("--inspect-code", action="store_true",
                    help="with --describe, download a hosted agent's code and "
                         "report which environment variables it reads and "
                         "which hosts it names. Nothing is saved.")
    ap.add_argument("--replay-env-var", action="append", metavar="NAME",
                    help="for a hosted agent, the environment variable its "
                         "code reads for the MCP endpoint. Repeatable. "
                         f"Default: {', '.join(DEFAULT_REPLAY_VARS)}")
    args = ap.parse_args(argv)

    if args.describe:
        return describe(args)

    if not args.cassette:
        ap.error("--cassette is required (or use --describe)")

    entry, recorded = cassette_agent(args.cassette)
    if args.agent and args.agent != entry:
        sys.exit(
            f"the cassette recorded {entry}; it cannot replay {args.agent}.\n\n"
            "A cassette's calls are the recorded agent's calls. Any other agent "
            "diverges from the first one, and an orchestrator replayed on a "
            "single-agent cassette reaches its children by name -- their "
            "production versions, their live toolbox, their writes. Replay the "
            "agent the cassette recorded, or record the one you mean to test.")
    if not args.agent:
        args.agent = entry
        others = list(recorded[1:])
        detail = (f" (entry point; {', '.join(others)} are reached over A2A)"
                  if others else "")
        print(f"agent      : {args.agent} — from the cassette{detail}")
    refuse_unstubbed_children(args.cassette, recorded)
    replay_agent = args.replay_agent or replay_agent_name(args.agent)
    if replay_agent == args.agent:
        sys.exit(f"--replay-agent must not be the agent under test: a clone "
                 f"under {args.agent!r} is what its own production callers "
                 "would reach by name.")
    server_label = recorded_server_label(args.cassette)
    print(f"replay as  : {replay_agent} (tools bound as {server_label}___*)")

    # Chosen here so the journal can be read back. See replay_tools().
    replay_session = uuid.uuid4().hex

    proc = None
    if args.serve:
        proc, base = serve(args.cassette, args.port, args.tool_defs,
                           args.journal)
        print(f"replay server on {base}")
        server_url = args.server_url or base
    else:
        server_url = args.server_url
        if not server_url:
            sys.exit("--server-url is required unless --serve is given")

    try:
        if is_locally_scoped(server_url) and not args.allow_local:
            sys.exit(
                f"{server_url} is not reachable from Azure.\n\n"
                "Foundry calls the replay server; the server does not call "
                "Foundry. A localhost URL becomes a tool call that times out "
                "inside the agent run, which looks like an agent failure "
                "rather than a configuration mistake. Host the server, or "
                "put a tunnel in front of it, and pass the public URL as "
                "--server-url. See docs/REPLAY.md.")

        query = args.query or cassette_query(args.cassette)

        if args.dry_run:
            print(json.dumps({
                "agent": args.agent,
                "replay_agent": replay_agent,
                "base_version": args.agent_version or "latest",
                "server_url": server_url,
                "tools_replaced_with": [
                    {"type": "mcp", "server_label": server_label,
                     "server_url": server_url, "require_approval": "never",
                     "headers": (["Authorization"] if args.token else [])
                                + ["Mcp-Session-Id"]}],
                "summary_url": summary_url(server_url),
                "replay_session": replay_session,
                "query": query,
                "temp_version_metadata": {"purpose": TEMP_MARKER},
            }, indent=1))
            print("\ndry run: nothing was created in the project.")
            return 0

        if not args.project_endpoint:
            sys.exit(
                "--project-endpoint or AZURE_AI_PROJECT_ENDPOINT is "
                "required.\n\n"
                "It is not deployed with the replay server and should not "
                "be: traffic is one way. Foundry calls the replay server; "
                "the server never calls Foundry. This is read by whoever "
                "runs the gate.\n\n"
                "CI already has it as the GitHub Actions variable "
                "AZURE_AI_PROJECT_ENDPOINT, on the staging and production "
                "environments -- see .github/workflows/evals.yml. Locally:\n"
                "  $env:AZURE_AI_PROJECT_ENDPOINT = "
                "'https://<resource>.services.ai.azure.com/api/projects/"
                "<project>'\n"
                "The exact value is in docs/CREDENTIALS.md, and the Foundry "
                "portal shows it on the project overview.")

        from azure.ai.projects import AIProjectClient, models
        from azure.identity import DefaultAzureCredential

        # allow_preview is what lets get_openai_client() point at an agent's
        # own endpoint, which is how a prompt agent is invoked.
        client = AIProjectClient(endpoint=args.project_endpoint,
                                 credential=DefaultAzureCredential(),
                                 allow_preview=True)
        agents = client.agents

        try:
            base = agent_version_details(agents, args.agent,
                                         args.agent_version)
        except Exception as exc:
            # "Not found" is not a useful answer when the caller did not
            # choose the name in the first place.
            known = []
            try:
                known = sorted(a.name for a in agents.list())
            except Exception:
                pass
            sys.exit(f"cannot read agent {args.agent!r}: {exc}\n"
                     + (f"\nAgents in this project:\n  "
                        + "\n  ".join(known) if known else ""))

        payload = definition_payload(base)
        binding = binding_for(payload,
                              args.replay_env_var or DEFAULT_REPLAY_VARS)
        if definition_kind(payload) in HostedBinding.kinds:
            try:
                known = {a.name for a in agents.list()}
            except Exception as exc:
                sys.exit(f"cannot list the project's agents to check that "
                         f"{args.agent} names none of them: {exc}")
            refs = agent_references(payload, known, args.agent)
            if refs:
                named = ", ".join(f"{k}={v}" for k, v in refs)
                sys.exit(
                    f"{args.agent} v{getattr(base, 'version', '?')} names other "
                    f"agents in its environment ({named}). It reaches them by "
                    "name, at their production versions, against the live "
                    "toolbox -- the replay can only stub the agent it clones. "
                    "Nothing was created.")
        base_version = getattr(base, "version", None) or "latest"
        print(f"binding    : kind={definition_kind(payload)} — "
              f"{binding.describe_plan(payload)}")
        replay_existed = replay_agent_exists(agents, replay_agent)
        print(f"replay as  : {replay_agent} "
              + ("(exists)" if replay_existed
                 else "(created by this run, deleted after it)"))

        prepare_binding(binding, client=client, server_url=server_url,
                        token=args.token, session=replay_session,
                        models=models,
                        label=f"replay-{replay_session[:12]}",
                        server_label=server_label)
        definition = binding.rebind(payload, server_url=server_url,
                                    models=models, token=args.token,
                                    session=replay_session,
                                    server_label=server_label)

        try:
            temp = binding.create(
                agents, replay_agent, definition,
                description=f"temporary: stubbed-tool replay of "
                            f"{args.agent} v{base_version}",
                metadata={"purpose": TEMP_MARKER,
                          "replays_agent": args.agent,
                          "base_version": str(base_version),
                          "cassette": os.path.basename(args.cassette)},
                base_version=base_version, source_agent=args.agent)
        except Exception as exc:
            teardown_binding(binding)
            sys.exit(
                f"could not create a version of {replay_agent}: "
                f"{type(exc).__name__}: {exc}\n\n"
                f"The clone is created under {replay_agent}, never under "
                f"{args.agent}, so production traffic to {args.agent} cannot "
                "reach it. A 403 means this identity may read agents but not "
                "create them in this project. The toolbox was deleted; "
                "nothing else was created.")
        temp_version = getattr(temp, "version", None) or getattr(temp, "id", None)
        print(f"created {replay_agent} v{temp_version} "
              f"(clone of {args.agent} v{base_version})")

        session = None
        run_ids = {"agent_session_id": None, "response_id": None,
                   "replay_agent": replay_agent,
                   "server_label": server_label,
                   "replay_toolbox": (list(binding.toolbox)
                                      if getattr(binding, "toolbox", None)
                                      else None),
                   "started_utc": _utcnow()}
        try:
            problem = routing_problem(agents, replay_agent, temp_version)
            if problem:
                sys.exit(f"not invoking: {problem}")
            # VersionRefIndicator, not VersionIndicator: the latter is the
            # abstract discriminated base and takes no version at all. The
            # field is agent_version.
            session = create_session_when_ready(
                agents, replay_agent,
                models.VersionRefIndicator(agent_version=str(temp_version)))
            session_id = getattr(session, "agent_session_id", None)
            run_ids["agent_session_id"] = session_id
            print(f"session {session_id} — query: {str(query)[:70]}")

            response = invoke_agent(client, replay_agent, session_id, query)
            run_ids["response_id"] = getattr(response, "id", None)
        finally:
            if session is not None:
                try:
                    agents.stop_session(replay_agent,
                                        getattr(session, "agent_session_id"))
                except Exception:
                    # A session that will not stop is not a reason to leave a
                    # temporary agent version behind.
                    pass
            try:
                # force: a hosted version with a session still winding down
                # is otherwise a 409, and the clone stays. Both are ours.
                if replay_existed:
                    agents.delete_version(replay_agent, temp_version,
                                          force=True)
                    print(f"deleted {replay_agent} v{temp_version}")
                else:
                    agents.delete(replay_agent, force=True)
                    print(f"deleted {replay_agent} (made for this run)")
            except Exception as exc:
                # Not a reason to leave the toolbox too: it carries the
                # replay token in its headers. A clone left under the replay
                # agent is reachable by nothing but a replay, and its toolbox
                # is about to stop existing.
                print(f"WARNING  could not delete {replay_agent} "
                      f"v{temp_version}: {exc}")
            # The toolbox goes last. On success that avoids a moment where
            # the clone names a toolbox that no longer exists; after a failed
            # delete the clone is left, reachable by no caller but a replay,
            # and its toolbox -- the thing holding the token -- still goes.
            teardown_binding(binding)
            run_ids["finished_utc"] = _utcnow()

        s = read_journal(server_url if not args.serve else base, args.token,
                         replay_session)
        write_manifest(args.manifest, args, base_version, temp_version, s,
                       run_ids)
        ok, text = verdict(s, args.min_matched_prefix)
        print("\nREPLAY")
        print(text)
        print(f"\nprovenance -> {args.manifest}")
        return 0 if ok else 1

    finally:
        if proc:
            proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
