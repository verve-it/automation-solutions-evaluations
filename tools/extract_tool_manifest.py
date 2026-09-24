#!/usr/bin/env python3
"""
extract_tool_manifest.py — build `tool_manifests/<toolbox>-v<n>.json`.

`gen_ai.tool.definitions` only ever covers A2A agent registrations, so no
schema exists in telemetry for any ConnectWise tool, and both Tool Input
Accuracy and the generated argument validation in run_evals.py have nothing to
run against. The toolbox is versioned, so this is a one-time extraction per
version, not a per-run capture.

Two inputs, best first:

  --from-url <endpoint>      call `tools/list` on the running MCP server. The
                             ground truth: it is the DEPLOYED contract, not
                             what the source implies. Needs a token unless the
                             server holds its own credentials.

  --from-tools-list <file>   the raw JSON-RPC `tools/list` response, or the
                             toolbox definition exported from the Foundry
                             portal. Produces a complete manifest.

  --from-source <repo>       a checkout of the MCP server. Registers every tool
                             in-process against a FastMCP instance with dummy
                             credentials and reads back the generated
                             inputSchema. Complete, needs no network and no
                             credentials, and is exact as long as the deployment
                             matches the commit — which is the catch: it is the
                             contract the source implies, not the deployed one.

  --from-trace <file>        a span export. Produces a SKELETON: every tool the
                             agents actually called, with the description
                             telemetry carries and the argument keys observed,
                             but `parameters: null`. Fill those in by hand or
                             re-run with --from-tools-list. A null schema is
                             skipped by the validator rather than guessed at.

    python3 extract_tool_manifest.py --from-trace traces/2026-09-03-full-triage.json \\
        --toolbox ConnectwiseMCP --version 5 -o tool_manifests/connectwisemcp-v5.json
"""

from __future__ import annotations

# This script lives in a subdirectory but imports the converter and scorer
# from the repo root, so put the root on sys.path before those imports. Keeps
# `python3 tools/extract_tool_manifest.py` working from anywhere, with no package
# conversion and no editable install. REPO_ROOT is also how sibling
# directories such as foundry_evaluators/ are located.
import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import argparse, datetime as _dt, json, os, subprocess, sys
import urllib.error, urllib.request


def _git_rev(repo):
    """Short commit of the checkout, so a manifest says what it came from."""
    try:
        out = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
from collections import defaultdict

from trace_to_eval import (K_TOOL, K_TOOL_ARGS, K_TOOL_DESC, base_tool_name,
                           find_toolboxes, is_tool_span, load_spans,
                           unwrap_call_tool, AGENT_NAMES)


DEFAULT_SCOPE = "https://ai.azure.com/.default"
# MCP streamable HTTP replies with either JSON or an SSE stream.
ACCEPT = "application/json, text/event-stream"


def _token(explicit, scope):
    if explicit:
        return explicit
    env = os.environ.get("FOUNDRY_TOKEN")
    if env:
        return env
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError:
        sys.exit("azure-identity not installed and no --token/FOUNDRY_TOKEN. "
                 "pip install -r requirements.txt, or paste a token from "
                 f"`az account get-access-token --scope {scope}`")
    return DefaultAzureCredential().get_token(scope).token


def _post(url, token, payload, session=None):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Accept": ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return (resp.read().decode("utf-8", "replace"),
                    resp.headers.get("Mcp-Session-Id"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        sys.exit(f"HTTP {exc.code} from {url}\n{detail}\n\n"
                 "401/403 usually means the wrong token scope — try "
                 f"--scope https://cognitiveservices.azure.com/.default")


def _parse(raw):
    """A streamable-HTTP endpoint may answer with SSE; take the last frame."""
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        return json.loads(raw)
    frames = [l[len("data:"):].strip() for l in raw.splitlines()
              if l.startswith("data:")]
    if not frames:
        sys.exit(f"unrecognised response: {raw[:300]}")
    return json.loads(frames[-1])


def fetch_tools(url, token):
    """initialize -> notifications/initialized -> tools/list, per the MCP
    streamable-HTTP handshake. The session id comes back on the initialize
    response and must be echoed on every later request."""
    raw, session = _post(url, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "triage-automation-evals",
                                  "version": "1"}},
    })
    _parse(raw)
    _post(url, token,
          {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)

    raw, _ = _post(url, token,
                   {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                    "params": {}}, session)
    reply = _parse(raw)
    if "error" in reply:
        sys.exit(f"tools/list failed: {reply['error']}")
    return reply.get("result", {}).get("tools", [])


def fetch_tools_via_sdk(host, project, toolbox, revision):
    """Let the SDK talk to the toolbox, so it supplies the api-version.

    The raw endpoint rejects a request without `?api-version=`, and guessing
    the right value is a round-trip each time.
    """
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    endpoint = f"{host.rstrip('/')}/api/projects/{project}"
    client = AIProjectClient(endpoint=endpoint,
                             credential=DefaultAzureCredential())
    try:
        box = client.toolboxes.get_version(name=toolbox, version=str(revision))
    except AttributeError:
        box = client.toolboxes.get(name=toolbox)

    for attr in ("tools", "tool_definitions", "definitions"):
        found = getattr(box, attr, None)
        if found:
            return [t if isinstance(t, dict) else
                    getattr(t, "as_dict", lambda: vars(t))() for t in found]
    raise SystemExit(
        "the toolbox object carries no tool list under tools / "
        "tool_definitions / definitions. Its attributes are: "
        + ", ".join(a for a in dir(box) if not a.startswith("_")))


def from_tools_list(payload):
    """Accept the JSON-RPC envelope, its `result`, or a bare tool array."""
    if isinstance(payload, dict):
        payload = payload.get("result", payload)
        payload = payload.get("tools", payload)
    if not isinstance(payload, list):
        raise ValueError("expected a list of tools, or {result:{tools:[...]}}")
    return [{
        "name": base_tool_name(t.get("name", "")),
        "description": t.get("description", ""),
        "parameters": t.get("inputSchema") or t.get("parameters"),
        # MCP defines readOnlyHint and destructiveHint for exactly the
        # question "does calling this change anything". Dropping them meant
        # which tools write had to be written down by hand somewhere else,
        # and that list was wrong. Keep what the server says about itself.
        "annotations": t.get("annotations") or None,
    } for t in payload]


def from_source(repo, register_globs=("src/cwpsa/tools/tier*/[!_]*.py",)):
    """Register every tool in-process and read back the generated inputSchema.

    Imports the server's own modules, so it produces exactly what FastMCP would
    serve from tools/list for that commit. Needs the server's dependencies
    importable and its settings constructible — hence the dummy credentials.
    """
    import asyncio, glob, importlib.util, os as _os, re

    repo = _os.path.abspath(repo)
    src = _os.path.join(repo, "src")
    if not _os.path.isdir(src):
        raise SystemExit(f"{repo} has no src/ — is this the MCP server repo?")
    sys.path.insert(0, src)

    # config.py resolves secrets at import time and raises when one is missing.
    # Nothing is ever called, so the values only have to exist. Discover the
    # names from the source rather than hardcoding them, so a secret added
    # upstream does not silently break extraction six months from now.
    _os.environ.setdefault("CW_LOCAL_SECRETS", "1")
    config_py = _os.path.join(src, "cwpsa", "config.py")
    if _os.path.isfile(config_py):
        with open(config_py, encoding="utf-8") as fh:
            body = fh.read()
        names = set(re.findall(r'get_secret\(\s*["\']([^"\']+)["\']', body))
        for name in sorted(names):
            _os.environ.setdefault(name.upper().replace("-", "_"), "dummy")
        if names:
            print(f"stubbed {len(names)} secret(s) for import: "
                  f"{', '.join(sorted(names))}")

    try:
        import fastmcp.tools.tool  # noqa: F401
    except ModuleNotFoundError:
        # Moved between fastmcp majors; some modules still import the old path.
        import fastmcp.tools.base as _base
        sys.modules["fastmcp.tools.tool"] = _base
    from fastmcp import FastMCP

    mcp = FastMCP("manifest-extract")
    modules = sorted(f for g in register_globs
                     for f in glob.glob(_os.path.join(repo, g)))
    if not modules:
        raise SystemExit(f"no tool modules matched {register_globs} under {repo}")
    for path in modules:
        name = "cwpsa_tool_" + _os.path.basename(path)[:-3]
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "register"):
            mod.register(mcp)

    listed = asyncio.run(mcp.list_tools())
    tools = []
    for t in listed:
        schema = getattr(t, "parameters", None) or t.inputSchema
        tools.append({"name": t.name,
                      "description": t.description,
                      "parameters": schema})
    return sorted(tools, key=lambda t: t["name"])


def from_trace(spans):
    """Names, descriptions and observed argument keys. No schemas — telemetry
    does not carry them, and inventing one would score runs against a fiction.
    """
    seen, keys = {}, defaultdict(set)
    for s in spans:
        if not is_tool_span(s):
            continue
        d = s["d"]
        name = d.get(K_TOOL, "") or s["name"].replace("execute_tool ", "", 1)
        name, args, _ = unwrap_call_tool(name, d.get(K_TOOL_ARGS, "") or "")
        if name in AGENT_NAMES:            # A2A hand-off, not an MCP tool
            continue
        bare = base_tool_name(name)
        seen.setdefault(bare, d.get(K_TOOL_DESC, "") or "")
        if not seen[bare]:
            seen[bare] = d.get(K_TOOL_DESC, "") or ""
        try:
            parsed = json.loads(args) if args else {}
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            keys[bare].update(parsed)

    return [{
        "name": name,
        "description": desc,
        "parameters": None,
        "observed_argument_keys": sorted(keys[name]),
    } for name, desc in sorted(seen.items())]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-url", metavar="ENDPOINT",
                     help="the MCP server's endpoint. Go at the server "
                          "directly: the Foundry toolbox path "
                          "(/api/projects/<p>/toolboxes/<t>/versions/<v>/mcp) "
                          "rejects az tokens and API keys with "
                          "AgenticIdentityToken and is reachable only from "
                          "inside an agent run.")
    src.add_argument("--from-tools-list", metavar="FILE")
    src.add_argument("--from-source", metavar="REPO",
                     help="a checkout of the MCP server")
    src.add_argument("--from-trace", metavar="FILE")
    ap.add_argument("--toolbox", required=True, help="e.g. ConnectwiseMCP")
    ap.add_argument("--version", default="*",
                    help='binding revisions this manifest covers. "*" (the '
                         "default) is normally right — the version in the "
                         "toolbox URL tracks when an agent's binding was last "
                         "edited, not the tool contract. Pin only with "
                         "evidence the contract differs.")
    ap.add_argument("--scope", default=DEFAULT_SCOPE,
                    help="token scope for --from-url")
    ap.add_argument("--token",
                    help="bearer token for --from-url; overrides "
                         "DefaultAzureCredential")
    ap.add_argument("--no-auth", action="store_true",
                    help="send no Authorization header. An MCP server may "
                         "hold its own credentials through the project "
                         "connection rather than expecting yours.")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    if args.from_url:
        token = None if args.no_auth else _token(args.token, args.scope)
        tools = from_tools_list({"tools": fetch_tools(args.from_url, token)})
        source = (f"tools/list against {args.from_url} on "
                  f"{_dt.date.today().isoformat()}. The deployed contract.")
    elif args.from_tools_list:
        with open(args.from_tools_list, encoding="utf-8") as fh:
            tools = from_tools_list(json.load(fh))
        source = f"tools/list: {os.path.basename(args.from_tools_list)}"
    elif args.from_source:
        tools = from_source(args.from_source)
        rev = _git_rev(args.from_source)
        source = (f"FastMCP tool registry of {os.path.basename(os.path.abspath(args.from_source))}"
                  f"{' at ' + rev if rev else ''}, read in-process on "
                  f"{_dt.date.today().isoformat()}. The server's own generated "
                  "inputSchema, not a live tools/list response, so it reflects "
                  "the source the toolbox is built from rather than the "
                  "deployed revision.")
    else:
        spans = load_spans(args.from_trace)
        tools = from_trace(spans)
        source = f"trace skeleton: {os.path.basename(args.from_trace)}"
        boxes = {f"{t}@{v}" for s in spans for t, v in find_toolboxes([s])}
        if boxes:
            print(f"toolbox versions in this trace: {', '.join(sorted(boxes))}")

    manifest = {
        "toolbox": args.toolbox,
        "versions": [v.strip() for v in str(args.version).split(",")],
        "source": source,
        "tools": tools,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, ensure_ascii=False)
        fh.write("\n")

    without = [t["name"] for t in tools if not t.get("parameters")]
    print(f"{len(tools)} tool(s) -> {args.out}")
    if without:
        print(f"{len(without)} without a schema (not validated until filled): "
              f"{', '.join(without)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
