#!/usr/bin/env bash
# Provision the infrastructure and publish the replay server into it.
#
#   export REPLAY_TOKEN=$(openssl rand -hex 32)
#   ./deploy.sh my-resource-group
#
# Idempotent: re-running redeploys the template and republishes the package.
# The token is read from the environment rather than stored, because the
# cassettes it protects carry ticket and company identifiers.
set -euo pipefail

RG="${1:-}"
if [ -z "$RG" ]; then
  echo "usage: REPLAY_TOKEN=<token> $0 <resource-group> [location]" >&2
  exit 2
fi
LOCATION="${2:-eastus2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "${REPLAY_TOKEN:-}" ]; then
  echo "REPLAY_TOKEN is not set. Without it the replay server is open to" >&2
  echo "anyone who finds the URL, and the cassettes carry ticket and" >&2
  echo "company identifiers. Generate one: openssl rand -hex 32" >&2
  exit 2
fi

command -v az >/dev/null || { echo "az CLI not found" >&2; exit 2; }

echo "==> resource group $RG ($LOCATION)"
az group create -n "$RG" -l "$LOCATION" -o none

echo "==> template"
OUT=$(az deployment group create \
  -g "$RG" \
  -n "replay-mcp-$(date -u +%Y%m%d%H%M%S)" \
  -f "$HERE/infra/main.bicep" \
  -p "$HERE/infra/main.bicepparam" \
  -p location="$LOCATION" replayToken="$REPLAY_TOKEN" \
  --query properties.outputs -o json)

APP=$(echo "$OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["functionAppName"]["value"])')
HOSTNAME=$(echo "$OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["functionAppHostName"]["value"])')

echo "==> package"
"$HERE/build.sh"

echo "==> publish to $APP"
if command -v func >/dev/null; then
  ( cd "$HERE/.build" && func azure functionapp publish "$APP" --no-build )
else
  # Core Tools is the documented path; zip deploy is the fallback when it is
  # not installed. Both land on the same OneDeploy endpoint for Flex.
  ZIP="$(mktemp -d)/package.zip"
  ( cd "$HERE/.build" && zip -qr "$ZIP" . )
  az functionapp deployment source config-zip -g "$RG" -n "$APP" --src "$ZIP" -o none
fi

echo
echo "==> deployed"
echo "  MCP      https://$HOSTNAME/mcp/<cassette-id>"
echo "  summary  https://$HOSTNAME/summary/<cassette-id>"
echo
echo "Check it answers, without touching ConnectWise:"
echo "  curl -s https://$HOSTNAME/ | python3 -m json.tool"
echo
echo "Run the gate:"
echo "  python3 replay/run_replay.py \\"
echo "      --cassette cassettes/<cassette-id>.json \\"
echo "      --agent <agent-name> \\"
echo "      --server-url https://$HOSTNAME/mcp/<cassette-id> \\"
echo "      --token \"\$REPLAY_TOKEN\""
