#!/usr/bin/env python3
"""
fetch_tool_manifest.py — call `tools/list` on a Foundry toolbox MCP endpoint
and write tool_manifests/<toolbox>.json.

The endpoint is already in your traces, as the span name of every MCP call:

    POST /api/projects/automation-solutions/toolboxes/ConnectwiseMCP/versions/5/mcp

so the only thing you need to supply is the Foundry host.

    python3 fetch_tool_manifest.py \\
        --host https://<your-foundry-host> \\
        --project automation-solutions \\
        --toolbox ConnectwiseMCP --revision 5 \\
        -o tool_manifests/connectwisemcp.json

Auth is DefaultAzureCredential (`az login`), or paste a token:

    az account get-access-token --scope https://ai.azure.com/.default \\
        --query accessToken -o tsv

Before reaching for this, try `AIAgentConverter` from `azure-ai-evaluation`:
it takes an Agent Service thread + run id and returns `tool_definitions` read
from the Agent Service rather than from telemetry. If that covers your tools,
the manifest gap closes with no extraction at all. Your traces carry the
thread id as `gen_ai.conversation.id` (`conv_...`).

Stdlib only, so it runs anywhere `az` does.
"""

from __future__ import annotations
import argparse, json, os, sys, urllib.error, urllib.request

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
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json", "Accept": ACCEPT}
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


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True,
                    help="Foundry host, e.g. https://<resource>.services.ai.azure.com")
    ap.add_argument("--project", required=True)
    ap.add_argument("--toolbox", required=True)
    ap.add_argument("--revision", default="5",
                    help="binding revision from the span URL (default 5); "
                         "any revision serves the same tool list")
    ap.add_argument("--versions", default="*",
                    help="comma-separated binding revisions this manifest "
                         "covers. Default '*' — the revision is a binding "
                         "edit counter, not a schema version.")
    ap.add_argument("--scope", default=DEFAULT_SCOPE)
    ap.add_argument("--token", help="bearer token; overrides DefaultAzureCredential")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--print-url", action="store_true",
                    help="print the endpoint and exit")
    args = ap.parse_args()

    url = (f"{args.host.rstrip('/')}/api/projects/{args.project}"
           f"/toolboxes/{args.toolbox}/versions/{args.revision}/mcp")
    if args.print_url:
        print(url)
        return 0

    tools = fetch_tools(url, _token(args.token, args.scope))
    manifest = {
        "toolbox": args.toolbox,
        "versions": [v.strip() for v in args.versions.split(",")],
        "source": f"tools/list against {url}",
        "tools": [{"name": t.get("name", ""),
                   "description": t.get("description", ""),
                   "parameters": t.get("inputSchema") or t.get("parameters")}
                  for t in tools],
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, ensure_ascii=False)
        fh.write("\n")

    without = [t["name"] for t in manifest["tools"] if not t["parameters"]]
    print(f"{len(tools)} tool(s) -> {args.out}")
    if without:
        print(f"WARNING {len(without)} without a schema: {', '.join(without)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
