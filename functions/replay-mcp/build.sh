#!/usr/bin/env bash
# Assemble the deployment package.
#
# The function shares mcp_core.py and state_store.py with replay_server.py on
# purpose: two copies of the playback logic would be two answers to the same
# question, and this repo gates on that answer. So nothing here is authored --
# it is copied from the one place each file lives, into .build/lib/.
#
#   ./build.sh                          # every committed cassette
#   ./build.sh cassettes/2026-09-03-4dda7f4fa5f0.json   # just this one
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/.build"

rm -rf "$OUT"
mkdir -p "$OUT/lib" "$OUT/cassettes" "$OUT/tool_manifests"

cp "$HERE/server.py" "$HERE/host.json" "$HERE/requirements.txt" "$OUT/"

# Shared, not forked. If you find yourself editing a file in .build/, you are
# editing a copy that the next build overwrites.
cp "$ROOT/replay/mcp_core.py"     "$OUT/lib/"
cp "$ROOT/replay/state_store.py"  "$OUT/lib/"
cp "$ROOT/replay/make_cassette.py" "$OUT/lib/"
cp "$ROOT/trace_to_eval.py"       "$OUT/lib/"

cp "$ROOT"/tool_manifests/*.json "$OUT/tool_manifests/"

# Cassettes are derived from the committed traces, not committed themselves
# (.gitignore), so a fresh checkout has none. Build them rather than shipping
# an app that answers 404 to everything.
if [ "$#" -eq 0 ] && ! compgen -G "$ROOT/cassettes/*.json" > /dev/null; then
  echo "no cassettes yet -- building them from the committed traces"
  make -C "$ROOT" cassettes
fi

if [ "$#" -gt 0 ]; then
  for c in "$@"; do cp "$c" "$OUT/cassettes/"; done
else
  cp "$ROOT"/cassettes/*.json "$OUT/cassettes/"
fi

# A lossy cassette carries a truncated result: the agent under test would
# reason over less than the recorded run did and the difference would be
# scored as its fault. Refuse to ship one rather than discover it in a gate.
python3 - "$OUT/cassettes" <<'PY'
import json, os, sys
bad = []
for name in sorted(os.listdir(sys.argv[1])):
    with open(os.path.join(sys.argv[1], name), encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("lossy"):
        bad.append((name, data.get("warnings", [])))
if bad:
    for name, warnings in bad:
        print(f"LOSSY {name}")
        for w in warnings:
            print(f"        {w}")
    sys.exit("refusing to package a lossy cassette; rebuild it from a "
             "complete trace or pass the cassettes you want explicitly")
PY

echo "package -> $OUT"
find "$OUT" -type f | sed "s|$OUT|  .|" | sort
