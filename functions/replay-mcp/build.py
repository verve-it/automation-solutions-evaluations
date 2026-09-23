#!/usr/bin/env python3
"""
build.py — assemble the deployment package.

    python3 build.py                                    # every cassette
    python3 build.py ../../cassettes/2026-09-03-....json  # just this one

The function shares mcp_core.py and state_store.py with replay_server.py on
purpose: two copies of the playback logic would be two answers to the same
question, and this repo gates on that answer. So nothing in .build/ is
authored — it is copied from the one place each file lives. If you find
yourself editing a file under .build/, you are editing a copy that the next
build overwrites.

Python rather than shell because the other half of this repo's users are on
PowerShell, and a second implementation of the build would drift from the
first exactly like a second implementation of the playback would.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(HERE, ".build")

# Shared, not forked. Each is copied from the single place it lives.
SHARED = [
    os.path.join(ROOT, "replay", "mcp_core.py"),
    os.path.join(ROOT, "replay", "state_store.py"),
    os.path.join(ROOT, "replay", "make_cassette.py"),
    os.path.join(ROOT, "trace_to_eval.py"),
    # trace_to_eval and make_cassette import evalconfig at module level, so
    # a package without it dies on import -- every request a 502. It did,
    # from the commit that added evalconfig until this line. The config file
    # rides along so the hosted import reads what a checkout reads.
    os.path.join(ROOT, "evalconfig.py"),
    os.path.join(ROOT, "eval-config.json"),
]

OWN = ["server.py", "host.json", "requirements.txt"]

# Everything the server serves, in one file at the package root.
#
# The package must contain NO DIRECTORIES. The deployment keeps files at the
# root of wwwroot and drops subdirectories -- lib/ went that way first, and
# tool_manifests/ went the same way once lib/ was flattened, each time as a
# 502 with a stack trace behind it. A flat package cannot lose a directory
# because it does not have one.
PAYLOAD_NAME = "replay_payload.json"
PAYLOAD_SCHEMA = "verve/replay-payload@1"


def build_cassettes():
    """Cassettes are derived from the committed traces, not committed.

    Rebuilt on every package, not only when the directory is empty. A
    half-populated cassettes/ is the common state -- someone ran
    `make cassettes` against one trace, or `make replay` on a single file --
    and copying whatever happens to be there ships a package that silently
    covers fewer replays than the repo does. Cassettes are derived artefacts;
    deriving them is cheap and removes the question.
    """
    print("building cassettes from the committed traces")
    subprocess.run([sys.executable, os.path.join(ROOT, "replay",
                                                 "make_cassette.py"),
                    os.path.join(ROOT, "traces",
                                 "2026-09-03-full-triage.json"),
                    "-o", os.path.join(ROOT, "cassettes")], check=True)
    subprocess.run([sys.executable, os.path.join(ROOT, "replay",
                                                 "make_cassette.py"),
                    os.path.join(ROOT, "traces",
                                 "2026-09-15-ops-worst-case.json"),
                    "-o", os.path.join(ROOT, "cassettes")], check=True)


def refuse_lossy(cassettes):
    """A lossy cassette is a corrupted fixture.

    It carries a truncated result, so the agent under test reasons over less
    than the recorded run did and the difference is scored as its fault.
    Better to refuse here than to discover it inside a gate.
    """
    bad = []
    for name, data in sorted(cassettes.items()):
        if data.get("lossy"):
            bad.append((name, data.get("warnings", [])))
    if bad:
        for name, warnings in bad:
            print(f"LOSSY {name}")
            for warning in warnings:
                print(f"        {warning}")
        sys.exit("refusing to package a lossy cassette; rebuild it from a "
                 "complete trace or name the cassettes you want explicitly")


def main(argv):
    selected = argv[1:]

    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    for name in OWN:
        shutil.copy2(os.path.join(HERE, name), OUT)
    # Flat, beside server.py. Python puts a script's own directory on
    # sys.path, so this also removes the path logic that was wrong before.
    for path in SHARED:
        shutil.copy2(path, OUT)

    manifests = []
    manifest_dir = os.path.join(ROOT, "tool_manifests")
    for name in sorted(os.listdir(manifest_dir)):
        if name.endswith(".json"):
            with open(os.path.join(manifest_dir, name), encoding="utf-8") as fh:
                manifests.append(json.load(fh))

    if selected:
        chosen = list(selected)
    else:
        source = os.path.join(ROOT, "cassettes")
        build_cassettes()
        chosen = [os.path.join(source, name)
                  for name in sorted(os.listdir(source))
                  if name.endswith(".json")]

    cassettes = {}
    for path in chosen:
        with open(path, encoding="utf-8") as fh:
            cassettes[os.path.basename(path)[:-5]] = json.load(fh)

    refuse_lossy(cassettes)

    with open(os.path.join(OUT, PAYLOAD_NAME), "w", encoding="utf-8") as fh:
        json.dump({"schema": PAYLOAD_SCHEMA,
                   "cassettes": cassettes,
                   "tool_manifests": manifests}, fh, ensure_ascii=False)

    # Asserted, not assumed. This is the failure that cost three deploys.
    nested = [name for name in os.listdir(OUT)
              if os.path.isdir(os.path.join(OUT, name))]
    if nested:
        sys.exit(f"the package contains directories, which do not survive "
                 f"deployment: {', '.join(nested)}")

    print(f"package -> {OUT}")
    print(f"  {len(cassettes)} cassette(s), {len(manifests)} manifest(s) "
          f"in {PAYLOAD_NAME}")
    for name in sorted(os.listdir(OUT)):
        print(f"  ./{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
