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
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))

# build.sh vendors the shared modules into lib/ so the deployment package is
# self-contained. Running straight from a checkout, fall back to the repo so
# `python3 functions/replay-mcp/server.py` works with no build step.
_LIB = os.path.join(HERE, "lib")
if os.path.isdir(_LIB):
    sys.path.insert(0, _LIB)
else:
    _ROOT = os.path.dirname(os.path.dirname(HERE))
    sys.path.insert(0, _ROOT)
    sys.path.insert(0, os.path.join(_ROOT, "replay"))

from mcp_core import (Cassette, handle_rpc, parse_error,  # noqa: E402
                      tool_definitions)
from state_store import Conflict, open_store                # noqa: E402
from trace_to_eval import load_tool_manifests                # noqa: E402


class Config:
    """Everything the handler needs, read once at start-up."""

    def __init__(self, env=None):
        env = env or os.environ
        get = lambda k, d=None: (env.get(k) or d)          # noqa: E731
        self.cassette_dir = get("REPLAY_CASSETTE_DIR",
                                os.path.join(HERE, "cassettes"))
        self.tool_defs = get("REPLAY_TOOL_DEFS",
                             os.path.join(HERE, "tool_manifests"))
        self.default_cassette = get("REPLAY_CASSETTE")
        self.token = get("REPLAY_TOKEN")
        self.on_exhausted = get("REPLAY_ON_EXHAUSTED", "repeat")
        self.state_account = get("REPLAY_STATE_ACCOUNT")
        self.state_connection = get("REPLAY_STATE_CONNECTION")
        self.state_container = get("REPLAY_STATE_CONTAINER")
        self.port = int(get("FUNCTIONS_CUSTOMHANDLER_PORT", "8000"))


def cassette_ids(directory):
    if not os.path.isdir(directory):
        return []
    return sorted(f[:-5] for f in os.listdir(directory) if f.endswith(".json"))


def resolve_cassette_id(config, from_path):
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
    available = cassette_ids(config.cassette_dir)
    return available[0] if len(available) == 1 else None


class Library:
    """Cassette recordings and their advertised tools, loaded once each.

    The recording is immutable, so one parsed copy serves every session; only
    the cursor and journal are per-session, and those live in the store.
    """

    def __init__(self, config):
        self.config = config
        self.manifests = load_tool_manifests([config.tool_defs])
        self._cache = {}

    def get(self, cassette_id):
        if cassette_id not in self._cache:
            path = os.path.join(self.config.cassette_dir, f"{cassette_id}.json")
            if not os.path.isfile(path):
                return None
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
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
                    "cassettes": cassette_ids(self.config.cassette_dir),
                    "writes": "never performed"})

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
            self.config, query.get("cassette", [None])[0] or tail)
        if cassette_id is None:
            return self._send({"error": "no_cassette", "message":
                               "name a cassette: /summary/<cassette-id>",
                               "available": cassette_ids(
                                   self.config.cassette_dir)}, 404)
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
        cassette_id = resolve_cassette_id(self.config, from_path)
        if cassette_id is None:
            return self._send({"error": "no_cassette", "message":
                               "set REPLAY_CASSETTE or name one in the URL",
                               "available": cassette_ids(
                                   self.config.cassette_dir)}, 404)

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
        state, version = self.store.load(key)
        cassette.load_state(state)

        payload = handle_rpc(req, cassette, tools)

        # Only a tools/call advances anything. Writing state for every
        # initialize and tools/list would multiply blob writes for no gain.
        if req.get("method") == "tools/call":
            try:
                self.store.save(key, cassette.dump_state(), version)
            except Conflict:
                # Two instances answering one replay means the ordering this
                # gate depends on is already broken. Say so rather than
                # returning an answer that looks fine.
                return self._send(
                    {"jsonrpc": "2.0", "id": req.get("id"),
                     "error": {"code": -32002, "message":
                               "replay state conflict: another instance "
                               "advanced this session. The replay is not "
                               "ordered and its result cannot be trusted."}},
                    409, session)

        self._send(payload, session=session)


def _state_key(cassette_id, session):
    return f"{cassette_id}.{session or 'default'}"


def main():
    config = Config()
    library = Library(config)
    Handler.config = config
    Handler.library = library
    Handler.store = open_store(config.state_account, config.state_container,
                               config.state_connection)

    available = cassette_ids(config.cassette_dir)
    print(f"cassettes : {len(available)} in {config.cassette_dir}")
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
    print(f"state     : {type(Handler.store).__name__}")
    print(f"auth      : {'bearer token required' if config.token else 'OPEN'}")
    print(f"listening : 0.0.0.0:{config.port}")
    print("No ConnectWise request is made and no write is performed.",
          flush=True)

    ThreadingHTTPServer(("0.0.0.0", config.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
