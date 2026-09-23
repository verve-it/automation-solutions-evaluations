#!/usr/bin/env bash
# Provision the infrastructure and publish the replay server into it.
#
#   export REPLAY_TOKEN=$(openssl rand -hex 32)
#   ./deploy.sh my-resource-group
#
# Idempotent: re-running redeploys the template and republishes the package.
# The token is read from the environment rather than stored, because the
# cassettes it protects carry ticket and company identifiers. Redeploying?
# Export the SAME token the callers hold (the GitHub environments' REPLAY_TOKEN);
# a new one locks every caller out until they are updated too.
#
#   REPLAY_STORAGE_AUTH  identity (default) | connectionString
#   REPLAY_ASSIGN_ROLE   true (default) | false once someone else granted the
#                        identity its role (infra/rbac.bicep)
set -euo pipefail

RG="${1:-}"
if [ -z "$RG" ]; then
  echo "usage: REPLAY_TOKEN=<token> $0 <resource-group> [location]" >&2
  exit 2
fi
# Empty means "decide below": an existing group's own region, else eastus2.
# A resource group's location says where its metadata lives; the resources
# inside it may sit anywhere. So a group in a region Flex Consumption does not
# serve is not a reason to make a second group.
LOCATION="${2:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "${REPLAY_TOKEN:-}" ]; then
  echo "REPLAY_TOKEN is not set. Without it the replay server is open to" >&2
  echo "anyone who finds the URL, and the cassettes carry ticket and" >&2
  echo "company identifiers. Generate one: openssl rand -hex 32" >&2
  exit 2
fi

command -v az >/dev/null || { echo "az CLI not found" >&2; exit 2; }

EXISTING=$(az group show -n "$RG" --query location -o tsv 2>/dev/null || true)
if [ -n "$EXISTING" ]; then
  echo "==> resource group $RG exists in $EXISTING"
  LOCATION="${LOCATION:-$EXISTING}"
else
  LOCATION="${LOCATION:-eastus2}"
  echo "==> resource group $RG ($LOCATION)"
  az group create -n "$RG" -l "$LOCATION" -o none
fi

# Ask Azure rather than hardcoding a list that goes stale. Flex Consumption is
# region-limited, and finding that out from a template failure three resources
# in is worse than finding it out now.
echo "==> checking Flex Consumption is available in $LOCATION"
SUPPORTED=$(az functionapp list-flexconsumption-locations --query "[].name" -o tsv \
            | tr -d " " | tr "[:upper:]" "[:lower:]" | sort -u)
if ! echo "$SUPPORTED" | grep -qx "$(echo "$LOCATION" | tr -d " " | tr "[:upper:]" "[:lower:]")"; then
  echo >&2
  echo "Flex Consumption is not available in $LOCATION." >&2
  echo >&2
  echo "The resource group can stay where it is -- pass a supported region and" >&2
  echo "the resources go there instead:" >&2
  echo "  $0 $RG <region>" >&2
  echo >&2
  echo "Available:" >&2
  echo "$SUPPORTED" | sed "s/^/  /" >&2
  exit 2
fi
echo "    ok"

echo "==> template (storage auth: ${REPLAY_STORAGE_AUTH:-identity}, assign role: ${REPLAY_ASSIGN_ROLE:-true})"
# One parameter source only: the CLI will not take a .bicepparam file and
# inline -p overrides in the same deployment. main.bicepparam reads these.
export REPLAY_LOCATION="$LOCATION"
if ! OUT=$(az deployment group create \
  -g "$RG" \
  -n "replay-mcp-$(date -u +%Y%m%d%H%M%S)" \
  -f "$HERE/infra/main.bicep" \
  -p "$HERE/infra/main.bicepparam" \
  --query properties.outputs -o json 2>&1); then
  echo "$OUT" >&2
  # The one failure with a specific answer. Assigning a role needs Role Based
  # Access Control Administrator, User Access Administrator or Owner;
  # Contributor stops exactly here, after the storage account already exists.
  if echo "$OUT" | grep -q "RoleAssignmentExists"; then
    echo >&2
    echo "The identity already has its role, granted outside this template" >&2
    echo "(e.g. az role assignment create, under another name). Deploy without" >&2
    echo "declaring it, now and every time after:" >&2
    echo >&2
    echo "  REPLAY_ASSIGN_ROLE=false $0 $RG $LOCATION" >&2
  elif echo "$OUT" | grep -q "roleAssignments"; then
    echo >&2
    echo "That is the role assignment, and it is the only step Contributor" >&2
    echo "cannot do. Two ways on:" >&2
    echo >&2
    echo "  1. Deploy without it -- a storage key goes into app settings" >&2
    echo "     instead of the identity being granted a role:" >&2
    echo >&2
    echo "       REPLAY_STORAGE_AUTH=connectionString $0 $RG $LOCATION" >&2
    echo >&2
    echo "  2. Have someone with Role Based Access Control Administrator," >&2
    echo "     User Access Administrator or Owner grant it (infra/rbac.bicep" >&2
    echo "     says how), then redeploy without declaring it again:" >&2
    echo >&2
    echo "       REPLAY_ASSIGN_ROLE=false $0 $RG $LOCATION" >&2
    echo >&2
    echo "     The key disappears from configuration and nothing else changes." >&2
    echo "     Their values:" >&2
    echo "       az storage account list -g $RG --query \"[].name\" -o tsv" >&2
    echo "       az identity list -g $RG --query \"[].{name:name, principalId:principalId}\" -o table" >&2
    echo >&2
    echo "Nothing is half-built: the deployment is incremental and re-running" >&2
    echo "it is safe." >&2
  fi
  exit 1
fi

output() { echo "$OUT" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1']['value'])"; }
APP=$(output functionAppName)
HOSTNAME=$(output functionAppHostName)

echo "==> package"
python3 "$HERE/build.py"

echo "==> waiting for $APP to resolve"
# ARM returns before a new app is consistently readable, and publishing into
# that window fails in a way that reads like the app was never created.
for _ in 1 2 3 4 5 6; do
  az functionapp show -g "$RG" -n "$APP" -o none 2>/dev/null && break
  sleep 5
done
az functionapp show -g "$RG" -n "$APP" -o none || {
  echo "$APP is not readable in $RG. The template reported success, so check" >&2
  echo "the subscription the CLI is pointed at: az account show" >&2
  exit 1
}

echo "==> publish to $APP"
ZIP="$(mktemp -d)/package.zip"
( cd "$HERE/.build" && zip -qr "$ZIP" . )

# az, not Core Tools, and on purpose.
#
# `func azure functionapp publish <name>` finds the app by searching the
# subscription, and reads only the first page of results. Past ~999 resources
# it reports "Can't find app with name" for an app that plainly exists. az
# takes the resource group explicitly and does not search.
#
# No --build-remote, because there is nothing to build: requirements.txt is
# empty and the server is stdlib only. A remote build installed packages into
# .python_packages, which a custom handler never looks in -- an install that
# appeared to succeed and left the app without its dependency. Despite the
# command's name this routes to Flex Consumption package deployment, the only
# deployment technology Flex supports.
if ! az functionapp deployment source config-zip \
     -g "$RG" -n "$APP" --src "$ZIP" -o none; then
  rm -f "$ZIP"
  echo >&2
  echo "The package uploaded; the app did not come up." >&2
  echo >&2
  python3 "$HERE/diagnose.py" -g "$RG" >&2 || true
  exit 1
fi
rm -f "$ZIP"

echo
echo "==> deployed"
echo "  MCP      https://$HOSTNAME/mcp/<cassette-id>"
echo "  summary  https://$HOSTNAME/summary/<cassette-id>"
echo "  storage  $(output storageAccountName) (auth: $(output storageAuthMode))"
echo "  identity $(output identityName) (principal $(output identityPrincipalId))"

if [ -n "${SKIP_VERIFY:-}" ]; then
  echo
  echo "Skipped verification. Deployed is not the same as right:"
  echo "  python3 $HERE/verify.py https://$HOSTNAME -g $RG"
  exit 0
fi

# Deploying it is not the same as it being right, and finding that out in a
# second command is a second round trip. verify.py waits for the app to come
# up, replays every cassette against it, and runs the diagnosis itself if the
# server never answers.
echo
echo "==> verifying"
python3 "$HERE/verify.py" "https://$HOSTNAME" -g "$RG"

echo
echo "Run the gate:"
echo "  python3 replay/run_replay.py \\"
echo "      --cassette cassettes/<cassette-id>.json \\"
echo "      --server-url https://$HOSTNAME/mcp/<cassette-id> \\"
echo "      --token \"\$REPLAY_TOKEN\""
