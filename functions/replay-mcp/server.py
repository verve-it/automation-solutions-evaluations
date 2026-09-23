#!/usr/bin/env python3
"""
server.py — the replay MCP server, as an Azure Functions custom handler.

Same cassette, same answers, same guarantee as `replay/replay_server.py`:
**no ConnectWise request is made and no write is ever performed.** The only
difference is that this one is reachable from Azure, which is the one thing
`run_replay.py` cannot solve for you — Foundry calls the replay server, so
`localhost` can never work.

Why a custom handler and not the Functions MCP extension
--------------------------------------------------------
The MCP extension is the right way to build a *new* tool server, and it is
what `cwpsa-mcp` should use if it ever moves to Functions. It is the wrong
way to build a *stub of an existing one*, because its `toolProperties` is a
flat list of `{propertyName, propertyType, description, isRequired, isArray}`
with nowhere to put an `enum`.

Eight of our advertised properties are enums, and one of them is
`cw_resolve.reference_type` — the twenty-value enum whose arrival is the only
reason `valid_tool_args` can fail at all. Advertising it as a bare string
tells the agent under test it may send values production would reject, so
divergence would be something our stub caused. A stub that changes the tool
contract is not a stub.

Hosting an MCP SDK server on Functions with a custom handler is itself the
documented Microsoft path ("Host servers built with MCP SDKs on Azure
Functions"), and `host.json` carries the `mcp-custom-handler` configuration
profile for exactly this. So this is native hosting of a faithful stub rather
than extension hosting of a lossy one.

Configuration, all through app settings
---------------------------------------
    REPLAY_CASSETTE_DIR     directory of cassettes (default: ./cassettes)
    REPLAY_CASSETTE         default cassette id when the URL names none
    REPLAY_TOOL_DEFS        tool manifest directory (default: ./tool_manifests)
    REPLAY_TOKEN            require `Authorization: Bearer <token>`
    REPLAY_ON_EXHAUSTED     repeat | diverge   (default: repeat)
    REPLAY_STATE_SAS        container URL with a SAS — the one that needs no
                            SDK, and so no build step
    REPLAY_STATE_ACCOUNT    https://<account>.blob.core.windows.net
    REPLAY_STATE_CONNECTION storage connection string, where nobody could
                            assign the identity a role
    REPLAY_STATE_CONTAINER  container for per-session replay state

Routes
------
    POST /mcp                  MCP, using REPLAY_CASSETTE
    POST /mcp/<cassette-id>    MCP, using that cassette
    GET  /summary[?session=]   the replay journal
    GET  /                     health, and nothing else
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))

# Everything the server serves, in one file at the package root. The platform
# keeps root files and drops directories, so a directory in the package is a
# 502 waiting to happen.
PAYLOAD_NAME = "replay_payload.json"


# Oryx installs dependencies into .python_packages/lib/site-packages, and the
# Functions *Python worker* is what puts that on sys.path. A custom handler is
# `python server.py` and gets none of that setup, which is why the app logged
# `ModuleNotFoundError: No module named 'azure'` with the package plainly
# deployed. Nothing here needs a dependency any more, but an installed one
# should be usable rather than invisible.
_ORYX = os.path.join(HERE, ".python_packages", "lib", "site-packages")
if os.path.isdir(_ORYX) and _ORYX not in sys.path:
    sys.path.append(_ORYX)


def _package_listing(root, limit=60):
    """What is actually deployed, as the failure's own evidence.

    A ModuleNotFoundError names the module it wanted. It does not say what it
    got instead, and the difference between "the build dropped a file" and
    "the path logic is wrong" is exactly that. Printing the directory turns
    the next deployment into its own diagnosis.
    """
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".python_packages")]
        for name in sorted(files):
            out.append(os.path.relpath(os.path.join(base, name), root)
                       .replace(os.sep, "/"))
            if len(out) >= limit:
                return out + ["..."]
    return out


def _import_shared():
    """The shared modules, wherever they are.

    They sit beside this file in a deployment -- flat, not in lib/, because a
    subdirectory did not survive the remote build and the failure was a 502
    with `sys.path` as its only clue. Python puts a script's own directory on
    `sys.path`, so flat needs no path logic at all and cannot be dropped
    without dropping server.py too.

    Running from a checkout they are in replay/ and the repo root instead, so
    fall back to that rather than making local use need a build step.
    """
    try:
        return _bind()
    except ImportError:
        root = os.path.dirname(os.path.dirname(HERE))
        for path in (os.path.join(root, "replay"), root):
            if path not in sys.path:
                sys.path.insert(0, path)
        return _bind()


def _bind():
    from mcp_core import Cassette, handle_rpc, parse_error, tool_definitions
    from state_store import Conflict, open_store
    from trace_to_eval import load_tool_manifests
    return (Cassette, handle_rpc, parse_error, tool_definitions, Conflict,
            open_store, load_tool_manifests)


# Reported, not raised. A custom handler that dies on import is a 502, and a
# 502 says nothing: it looks the same whether the package is missing a file,
# the interpreter is wrong, or the port is.
try:
    (Cassette, handle_rpc, parse_error, tool_definitions, Conflict,
     open_store, load_tool_manifests) = _import_shared()
except ImportError as exc:
    print(f"FATAL  {exc}", flush=True)
    print(f"FATAL  python {sys.version.split()[0]} at {sys.executable}",
          flush=True)
    print(f"FATAL  server.py is in {HERE}", flush=True)
    print(f"FATAL  looked in: {sys.path[:4]}", flush=True)
    print("FATAL  the package should carry mcp_core.py, state_store.py, "
          "make_cassette.py and trace_to_eval.py beside server.py. "
          "build.py puts them there.", flush=True)
    print("FATAL  what is actually deployed:", flush=True)
    for entry in _package_listing(HERE):
        print(f"FATAL    {entry}", flush=True)
    raise


class Config:
    """Everything the handler needs, read once at start-up."""

    def __init__(self, env=None):
        env = env or os.environ
        get = lambda k, d=None: (env.get(k) or d)          # noqa: E731
        # One flat file beside server.py, because a subdirectory does not
        # survive the deployment: the Oryx repackage keeps files at the
        # package root and drops directories. lib/ went that way, and then
        # tool_manifests/ did. Directories are still the local layout, so
        # both are read.
        self.payload = get("REPLAY_PAYLOAD",
                           os.path.join(HERE, PAYLOAD_NAME))
        self.cassette_dir = get("REPLAY_CASSETTE_DIR",
                                os.path.join(HERE, "cassettes"))
        self.tool_defs = get("REPLAY_TOOL_DEFS",
                             os.path.join(HERE, "tool_manifests"))
        self.default_cassette = get("REPLAY_CASSETTE")
        self.token = get("REPLAY_TOKEN")
        self.on_exhausted = get("REPLAY_ON_EXHAUSTED", "repeat")
        self.state_sas = get("REPLAY_STATE_SAS")
        self.state_account = get("REPLAY_STATE_ACCOUNT")
        self.state_connection = get("REPLAY_STATE_CONNECTION")
        self.state_container = get("REPLAY_STATE_CONTAINER")
        self.port = int(get("FUNCTIONS_CUSTOMHANDLER_PORT", "8000"))


class Source:
    """Where the cassettes and the tool manifests come from.

    A deployed package is flat: `replay_payload.json` beside server.py holds
    every cassette and every manifest, because the platform drops
    subdirectories and a missing one is a 502 with a stack trace in it.

    A checkout is not flat -- cassettes/ and tool_manifests/ are real
    directories there and `make cassettes` writes into them -- so the payload
    is preferred when present and the directories are used when it is not.
    One shape for deployment, one for development, one reader.
    """

    def __init__(self, config):
        self.config = config
        self.payload = None
        if os.path.isfile(config.payload):
            with open(config.payload, encoding="utf-8") as fh:
                self.payload = json.load(fh)

    @property
    def origin(self):
        return (f"payload {os.path.basename(self.config.payload)}"
                if self.payload else f"directories under {HERE}")

    def cassette_ids(self):
        if self.payload is not None:
            return sorted(self.payload.get("cassettes") or {})
        directory = self.config.cassette_dir
        if not os.path.isdir(directory):
            return []
        return sorted(f[:-5] for f in os.listdir(directory)
                      if f.endswith(".json"))

    def cassette(self, cassette_id):
        if self.payload is not None:
            return (self.payload.get("cassettes") or {}).get(cassette_id)
        path = os.path.join(self.config.cassette_dir, f"{cassette_id}.json")
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def manifests(self):
        if self.payload is not None:
            return list(self.payload.get("tool_manifests") or [])
        return load_tool_manifests([self.config.tool_defs])


def resolve_cassette_id(config, source, from_path):
    """URL wins, then the app setting, then the only one in the package.

    Falling back to "the only one" is not cleverness: a deployment carrying a
    single cassette is the common case, and making it work without also
    setting REPLAY_CASSETTE removes a way to deploy something that answers
    404 to everything.
    """
    if from_path:
        return from_path
    if config.default_cassette:
        return config.default_cassette
    available = source.cassette_ids()
    return available[0] if len(available) == 1 else None


class Library:
    """Cassette recordings and their advertised tools, loaded once each.

    The recording is immutable, so one parsed copy serves every session; only
    the cursor and journal are per-session, and those live in the store.
    """

    def __init__(self, config, source=None):
        self.config = config
        self.source = source or Source(config)
        self.manifests = self.source.manifests()
        self._cache = {}

    def get(self, cassette_id):
        if cassette_id not in self._cache:
            data = self.source.cassette(cassette_id)
            if data is None:
                return None
            tools, missing = tool_definitions(
                Cassette(data, self.config.on_exhausted), self.manifests)
            self._cache[cassette_id] = (data, tools, missing)
        return self._cache[cassette_id]

    def cassette(self, cassette_id):
        """A fresh Cassette object; its state is loaded from the store."""
        entry = self.get(cassette_id)
        if entry is None:
            return None, None
        data, tools, _missing = entry
        return Cassette(data, self.config.on_exhausted), tools


class Handler(BaseHTTPRequestHandler):
    config: Config = None
    library: Library = None
    store = None

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        # The Functions host already logs every request. Repeating it here
        # puts canonicalised tool arguments — ticket and company identifiers —
        # into stdout a second time, which App Insights then keeps.
        pass

    # ------------------------------------------------------------ plumbing

    def _send(self, payload, status=200, session=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        token = self.config.token
        if not token:
            return True
        if self.headers.get("Authorization", "") == f"Bearer {token}":
            return True
        # x-functions-key is what an Azure Functions client reaches for, so
        # accept the same value there rather than making callers learn a
        # second convention.
        return self.headers.get("x-functions-key", "") == token

    def _path(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        # The Functions host may or may not prefix a route; matching on the
        # tail keeps /mcp, /api/mcp and /runtime/... all working.
        return parts, parse_qs(parsed.query)

    # -------------------------------------------------------------- routes

    def do_GET(self):
        parts, query = self._path()
        if "summary" in parts:
            if not self._authorized():
                return self._send({"error": "unauthorized"}, 401)
            return self._summary(parts, query)
        self._send({"status": "ok", "mode": "replay",
                    "cassettes": self.library.source.cassette_ids(),
                    "writes": "never performed",
                    # Which backend the replay state actually got. `unresolved`
                    # until the first tools/call, `MemoryStore` if blob storage
                    # could not be reached -- which is a working server with a
                    # weaker ordering guarantee, and worth saying out loud.
                    "state": getattr(self.store, "backend",
                                     type(self.store).__name__)})

    def _summary(self, parts, query):
        """The journal for one replay.

            GET /summary                       default cassette, shared session
            GET /summary/<cassette-id>         that cassette
            GET /summary?cassette=&session=    both named explicitly

        The session comes from `Mcp-Session-Id` or `?session=`, never from the
        path: the path slot is the cassette, so that a caller who knows only
        which cassette it ran can still read the result.
        """
        index = len(parts) - 1 - parts[::-1].index("summary")
        tail = parts[index + 1] if index + 1 < len(parts) else None
        cassette_id = resolve_cassette_id(
            self.config, self.library.source,
            query.get("cassette", [None])[0] or tail)
        if cassette_id is None:
            return self._send({"error": "no_cassette", "message":
                               "name a cassette: /summary/<cassette-id>",
                               "available":
                                   self.library.source.cassette_ids()}, 404)
        cassette, _tools = self.library.cassette(cassette_id)
        if cassette is None:
            return self._send({"error": "unknown_cassette",
                               "cassette": cassette_id}, 404)
        session = (query.get("session", [None])[0]
                   or self.headers.get("Mcp-Session-Id"))
        state, _version = self.store.load(_state_key(cassette_id, session))
        cassette.load_state(state)
        summary = cassette.summary()
        summary["session"] = session or "default"
        self._send(summary)

    def do_POST(self):
        if not self._authorized():
            return self._send({"error": "unauthorized"}, 401)

        parts, _query = self._path()
        from_path = None
        if "mcp" in parts:
            index = len(parts) - 1 - parts[::-1].index("mcp")
            if index + 1 < len(parts):
                from_path = parts[index + 1]
        cassette_id = resolve_cassette_id(self.config, self.library.source,
                                          from_path)
        if cassette_id is None:
            return self._send({"error": "no_cassette", "message":
                               "set REPLAY_CASSETTE or name one in the URL",
                               "available":
                                   self.library.source.cassette_ids()}, 404)

        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(parse_error())

        # A client that has not been given a session id yet gets one on
        # initialize; one that ignores the header shares a single session per
        # cassette, which is the old single-process behaviour rather than a
        # failure.
        session = self.headers.get("Mcp-Session-Id")
        if not session and req.get("method") == "initialize":
            session = uuid.uuid4().hex

        cassette, tools = self.library.cassette(cassette_id)
        if cassette is None:
            return self._send({"error": "unknown_cassette",
                               "cassette": cassette_id}, 404)

        key = _state_key(cassette_id, session)

        # Only a tools/call advances anything. Writing state for every
        # initialize and tools/list would multiply blob writes for no gain.
        if req.get("method") != "tools/call":
            state, _version = self.store.load(key)
            cassette.load_state(state)
            return self._send(handle_rpc(req, cassette, tools),
                              session=session)

        with _session_lock(key):
            for attempt in range(SAVE_ATTEMPTS):
                cassette, tools = self.library.cassette(cassette_id)
                state, version = self.store.load(key)
                cassette.load_state(state)
                payload = handle_rpc(req, cassette, tools)
                try:
                    self.store.save(key, cassette.dump_state(), version)
                    return self._send(payload, session=session)
                except Conflict:
                    # Another instance advanced this session between our load
                    # and save. Reload and answer again from its state.
                    time.sleep(random.uniform(0.005, 0.05) * (attempt + 1))

        # Still losing after every retry: something is contending far harder
        # than a fan-out can. Say so rather than answer from stale state.
        return self._send(
            {"jsonrpc": "2.0", "id": req.get("id"),
             "error": {"code": -32002, "message":
                       f"replay state conflict: lost {SAVE_ATTEMPTS} races "
                       "to save this session's state. The replay is not "
                       "ordered and its result cannot be trusted."}},
            409, session)


# Concurrent calls in one session are normal, not a fault: the ops agent fans
# out, and its recordings show up to nine MCP calls in flight at once (nine
# cw_resolve calls starting within 5 ms, each ~1.8 s). The state is loaded,
# advanced and saved conditionally on its ETag, so without this every call
# but one in a burst lost the race, got a 409 and was never journalled -- the
# agent saw errors where the recording saw results, and attribution saw calls
# the stub "never received".
#
# Within an instance a per-session lock serialises them. Across instances the
# save stays conditional and a lost race reloads and re-applies the call: calls
# with different keys commute, and same-key calls in one burst had no order in
# the recording either. Only a race still lost after every retry is a 409.
_SESSION_LOCKS = {}
_SESSION_LOCKS_GUARD = threading.Lock()
SAVE_ATTEMPTS = 25


def _session_lock(key):
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(key, threading.Lock())


def _state_key(cassette_id, session):
    return f"{cassette_id}.{session or 'default'}"


def main():
    config = Config()
    library = Library(config)
    Handler.config = config
    Handler.library = library
    Handler.store = open_store(config.state_account, config.state_container,
                               config.state_connection, config.state_sas)

    # Printed before anything can go wrong, so App Insights shows how far
    # start-up got even when it does.
    print(f"python    : {sys.version.split()[0]} at {sys.executable}",
          flush=True)
    print(f"cwd       : {os.getcwd()}", flush=True)
    print(f"handler   : {HERE}", flush=True)
    print(f"package   : {', '.join(_package_listing(HERE, limit=25))}",
          flush=True)

    available = library.source.cassette_ids()
    print(f"source    : {library.source.origin}")
    print(f"cassettes : {len(available)}")
    if not available:
        # A server with no cassettes answers 404 to every replay, which reads
        # like the gate found nothing rather than like a broken deployment.
        # Say it once, loudly, with the evidence.
        print("WARNING   no cassettes. Every replay will 404. The package "
              f"listing above is what actually arrived; {PAYLOAD_NAME} "
              "should be in it.", flush=True)
    for cid in available:
        entry = library.get(cid)
        if entry:
            data, tools, missing = entry
            flag = " LOSSY" if data.get("lossy") else ""
            print(f"  {cid}  {len(data['interactions'])} interactions, "
                  f"{data['writes']} write(s), {len(tools)} tool(s){flag}")
            if missing:
                print(f"    WARNING no schema for {', '.join(missing)} — "
                      "advertised with an empty schema, so this is not a "
                      "faithful stand-in. Fill tool_manifests/.")
    print(f"state     : {type(Handler.store).__name__} "
          f"(resolved on first call)")
    print(f"auth      : {'bearer token required' if config.token else 'OPEN'}")
    print(f"listening : 0.0.0.0:{config.port}")
    print("No ConnectWise request is made and no write is performed.",
          flush=True)

    ThreadingHTTPServer(("0.0.0.0", config.port), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        print("FATAL  the replay handler did not start:", flush=True)
        traceback.print_exc()
        sys.stdout.flush()
        raise
