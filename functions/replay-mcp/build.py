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
]

OWN = ["server.py", "host.json", "requirements.txt"]


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


def refuse_lossy(directory):
    """A lossy cassette is a corrupted fixture.

    It carries a truncated result, so the agent under test reasons over less
    than the recorded run did and the difference is scored as its fault.
    Better to refuse here than to discover it inside a gate.
    """
    bad = []
    for name in sorted(os.listdir(directory)):
        with open(os.path.join(directory, name), encoding="utf-8") as fh:
            data = json.load(fh)
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
    for sub in ("cassettes", "tool_manifests"):
        os.makedirs(os.path.join(OUT, sub))

    for name in OWN:
        shutil.copy2(os.path.join(HERE, name), OUT)
    # Flat, beside server.py, not in lib/. A lib/ subdirectory did not survive
    # the remote build -- the app came up with sys.path pointing at /home and
    # no module to import -- and a file beside the entry point cannot be
    # dropped without dropping the entry point too. Python puts a script's own
    # directory on sys.path, so this also removes the path logic that was
    # wrong in the first place.
    for path in SHARED:
        shutil.copy2(path, OUT)

    manifests = os.path.join(ROOT, "tool_manifests")
    for name in sorted(os.listdir(manifests)):
        if name.endswith(".json"):
            shutil.copy2(os.path.join(manifests, name),
                         os.path.join(OUT, "tool_manifests"))

    if selected:
        for path in selected:
            shutil.copy2(path, os.path.join(OUT, "cassettes"))
    else:
        source = os.path.join(ROOT, "cassettes")
        build_cassettes()
        for name in sorted(os.listdir(source)):
            if name.endswith(".json"):
                shutil.copy2(os.path.join(source, name),
                             os.path.join(OUT, "cassettes"))

    refuse_lossy(os.path.join(OUT, "cassettes"))

    print(f"package -> {OUT}")
    for base, _dirs, files in sorted(os.walk(OUT)):
        for name in sorted(files):
            full = os.path.join(base, name)
            print("  ." + full[len(OUT):].replace(os.sep, "/"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
