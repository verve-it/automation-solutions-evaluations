#!/usr/bin/env python3
"""
diagnose.py — what the replay app is actually doing, when it is not serving.

    python3 functions/replay-mcp/diagnose.py -g <resource-group>

A 502 from a Functions custom handler says one thing: the host is up and the
handler is not answering on its port. It does not say whether the process
crashed, never started, started too slowly, or bound somewhere else. What the
handler printed on the way down says all of that, and it is in Application
Insights rather than anywhere the CLI shows by default.

Prints, in order: the app's state and plan, its configured settings, the last
deployment, and the handler's own stdout. Never prints a setting's value --
REPLAY_TOKEN and the storage connection string are in there, and a diagnostic
that leaks them is worse than the fault it is diagnosing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

EXPECTED_SETTINGS = [
    # The mcp-custom-handler profile is preview-flagged and Bicep sets this
    # because Microsoft's sample does. It was NOT the cause of the early
    # 502s: host 4.1054.250.26428 honoured the profile without it. Listed so
    # its presence is visible, not because its absence explains anything.
    "AzureWebJobsFeatureFlags",
    "REPLAY_TOKEN", "REPLAY_CASSETTE", "REPLAY_ON_EXHAUSTED",
    "REPLAY_STATE_CONTAINER", "REPLAY_STATE_ACCOUNT",
    "REPLAY_STATE_SAS", "AzureWebJobsStorage",
    "AzureWebJobsStorage__accountName", "AZURE_CLIENT_ID",
    "APPLICATIONINSIGHTS_CONNECTION_STRING",
]


def _az_executable():
    """Find az, including on Windows where it is a .cmd.

    subprocess without shell=True will not resolve `az` to `az.cmd`, so this
    script died with WinError 2 on the machine it was written for. Resolve it
    properly rather than turning on shell=True, which would make every
    argument a quoting problem.
    """
    for candidate in ("az", "az.cmd", "az.bat"):
        found = shutil.which(candidate)
        if found:
            return found
    sys.exit("az CLI not found on PATH. Install it, or run the query by hand "
             "-- the command is at the bottom of this file's docstring.")


AZ = None


def az(*args, parse=True):
    global AZ
    if AZ is None:
        AZ = _az_executable()
    result = subprocess.run([AZ, *args], capture_output=True, text=True)
    if result.returncode != 0:
        return None, (result.stderr or result.stdout).strip()
    if not parse:
        return result.stdout.strip(), None
    try:
        return json.loads(result.stdout or "null"), None
    except json.JSONDecodeError:
        return result.stdout.strip(), None


def find_app(group, name):
    if name:
        return name
    apps, err = az("functionapp", "list", "-g", group,
                   "--query", "[].name", "-o", "json")
    if err:
        sys.exit(f"could not list function apps in {group}: {err}")
    candidates = [a for a in (apps or []) if "replay" in a]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        sys.exit(f"no function app with 'replay' in its name in {group}. "
                 f"Found: {', '.join(apps or []) or 'none'}")
    sys.exit(f"several candidates in {group}: {', '.join(candidates)}. "
             "Name one with --name.")


def section(title):
    print(f"\n=== {title} ===")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-g", "--resource-group", required=True)
    ap.add_argument("-n", "--name", help="function app; found automatically "
                                         "when only one matches")
    ap.add_argument("--minutes", type=int, default=30,
                    help="how far back to read the handler's output")
    args = ap.parse_args(argv)

    app = find_app(args.resource_group, args.name)
    print(f"app: {app}  (resource group {args.resource_group})")

    section("app")
    info, err = az("functionapp", "show", "-g", args.resource_group,
                   "-n", app, "--query",
                   "{state:state, host:defaultHostName, kind:kind, "
                   "sku:sku, runtime:siteConfig.linuxFxVersion, "
                   "https:httpsOnly}", "-o", "json")
    print(err or json.dumps(info, indent=1))

    section("settings (names only — values are secrets)")
    settings, err = az("functionapp", "config", "appsettings", "list",
                       "-g", args.resource_group, "-n", app,
                       "--query", "[].name", "-o", "json")
    if err:
        print(err)
    else:
        present = set(settings or [])
        for name in EXPECTED_SETTINGS:
            mark = "set" if name in present else "-"
            print(f"  {name:<42} {mark}")
        extra = sorted(present - set(EXPECTED_SETTINGS))
        if extra:
            print(f"  other: {', '.join(extra)}")
        if "AzureWebJobsFeatureFlags" not in present:
            print("\n  AzureWebJobsFeatureFlags is not set. infra/main.bicep "
                  "sets it (EnableMcpCustomHandlerPreview) because Microsoft's "
                  "sample does, but host 4.1054.250.26428 honoured the "
                  "mcp-custom-handler profile without it: its absence was not "
                  "the cause of the earlier 502s. Read the handler output "
                  "below before blaming it.")

    section("last deployment")
    deployments, err = az("functionapp", "deployment", "list-publishing-"
                          "profiles", "-g", args.resource_group, "-n", app,
                          "--query", "[0].publishMethod", "-o", "json")
    print(err or deployments)

    section(f"handler output, last {args.minutes} minutes")
    insights, err = az("monitor", "app-insights", "component", "show",
                       "-g", args.resource_group,
                       "--query", "[?contains(name, 'replay')].name | [0]",
                       "-o", "tsv", parse=False)
    if err or not insights:
        print("could not find the Application Insights component "
              f"({err or 'no match'}).")
        print("Query it directly once you know its name:")
        print(f'  az monitor app-insights query -g {args.resource_group} '
              f'--app <name> --analytics-query "traces | where timestamp > '
              f'ago({args.minutes}m) | project timestamp, message | order by '
              f'timestamp desc | take 100"')
        return 1

    query = (f"traces | where timestamp > ago({args.minutes}m) "
             "| project timestamp, message | order by timestamp asc "
             "| take 200")
    rows, err = az("monitor", "app-insights", "query",
                   "-g", args.resource_group, "--app", insights,
                   "--analytics-query", query, "-o", "json")
    if err:
        print(err)
        print("\nThe application-insights extension may not be installed:")
        print("  az extension add --name application-insights")
        return 1

    printed = 0
    for table in (rows or {}).get("tables", []):
        for row in table.get("rows", []):
            print("  " + "  ".join(str(cell) for cell in row))
            printed += 1
    if not printed:
        print("  nothing logged. The handler may never have started, which "
              "usually means the interpreter or the entry point in host.json "
              "is wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
