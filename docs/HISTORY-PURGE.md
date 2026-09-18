# Purging the leaked traces from git history

Three trace files and two derived artifacts were committed unscrubbed and
pushed. They are gone from `HEAD`, but git keeps every version of every file
ever committed, so they are still in the repository until history is rewritten.

This is the runbook. It was rehearsed on a mirror clone of this repo before
being written down — every command below ran, and the verification at the end
is the one that caught the mistake described in "Why by blob id".

## What is being removed

| blob | size | addresses | path |
|---|---|---|---|
| `8a33f75dfbc9` | 25 MB | 71 | `traces/2026-09-03-full-triage-rejoined.json` |
| `137052e18fee` | 5.1 MB | 67 | `traces/2026-09-03-full-triage.csv`, **also `query_data.csv`** |
| `9a2ad1d3dd35` | 1.3 MB | 2 | `traces/2026-09-15-ops-worst-case.csv`, **also `ops_bad.csv`** |
| `cc63982036f3` | 0.3 MB | 2 | `out/eval_runs.jsonl` |
| `4c2173b94203` | 0.1 MB | 1 | `out-ops/eval_runs.jsonl` |
| `8cb1a3e33ebc` | small | 0 | `…rejoined.json.meta.json` (goes with its file) |

All three branches carry them: `develop`, `main` and `staging`.

## Why by blob id, not by path

The obvious command is `--invert-paths --paths-from-file`. **It is wrong
here.** The initial commit `fa74a72` holds the same two CSV blobs at the root
as `query_data.csv` and `ops_bad.csv`; they were moved into `traces/` later. A
path-based purge rewrites history, reports success, and leaves both blobs
reachable under their old names.

That is not hypothetical — it is what the first rehearsal did. The verification
step caught it. `--strip-blobs-with-ids` removes the content wherever it has
ever lived, which is the property you actually want.

Note also that `git rev-list --all --objects` prints one path per object, so it
will not show you the second name. Do not use it to convince yourself a blob
has only one path.

## Before you start

- Everyone with a clone must be told. A rewrite changes every commit id; their
  branches will not fast-forward and a careless `git pull` re-introduces the
  old objects.
- Close or merge open work. There are no open PRs as of this writing; PRs #1
  and #2 are closed unmerged.
- The rewrite drops the `origin` remote on purpose, so you cannot push by
  reflex.

## The procedure

```bash
pip install git-filter-repo

# 1. A fresh mirror. Never run this on a working clone.
git clone --mirror https://github.com/verve-it/automation-solutions-evaluations.git purge.git
cd purge.git

# 2. The blob list, full 40-character ids.
cat > /tmp/leaked-blobs.txt <<'IDS'
8a33f75dfbc912eb24d879c52ce2e4ab4905d7c1
137052e18fee5b318084efc9c73b33a7a029987d
9a2ad1d3dd353f9b02a2cfec3ce692a2be26a22d
cc63982036f33d930a4e728a17e00f7106d1a629
4c2173b94203a9a770821f07b21da76668e5e204
8cb1a3e33ebc86943ff5156e68447853d276ee6b
IDS

# 3. Rewrite.
git filter-repo --strip-blobs-with-ids /tmp/leaked-blobs.txt --force
```

## Verify before pushing

Content-based, not path-based. Anything else checks the wrong thing:

```bash
python3 - <<'PY'
import subprocess, re
MAIL = re.compile(rb'[\w.+-]+@[\w-]+\.[\w.-]+')
SAFE = (b'example.com', b'example.org', b'invalid', b'localhost')
bad = 0
for line in subprocess.run(['git','rev-list','--all','--objects'],
                           capture_output=True, text=True).stdout.splitlines():
    parts = line.split(maxsplit=1)
    if len(parts) < 2:
        continue
    sha = parts[0]
    if subprocess.run(['git','cat-file','-t',sha],
                      capture_output=True, text=True).stdout.strip() != 'blob':
        continue
    raw = subprocess.run(['git','cat-file','-p',sha], capture_output=True).stdout
    hits = {m for m in MAIL.findall(raw)
            if not any(m.lower().endswith(s) for s in SAFE)}
    if hits:
        bad += 1
        print('STILL LEAKED', sha[:12], parts[1], sorted(hits)[:2])
print('clean' if not bad else f'{bad} blob(s) remain')
PY
```

Expect four hits on old versions of `tests/test_scrub.py` containing
`eli@verveit.com`. **Leave them.** That address is the author e-mail on every
commit in the repository, so removing it from a test fixture while `git log`
publishes it is theatre. The current fixture uses `tech@example.com`.

Then check the repo still works:

```bash
git clone purge.git check && cd check && python3 -m pytest -q   # 270 passed
```

## Push

```bash
cd purge.git
git remote add origin https://github.com/verve-it/automation-solutions-evaluations.git
git push --force --all origin
git push --force --tags origin
```

## Afterwards, and this part matters

**The force-push does not delete anything from GitHub.** The old objects stay
reachable by SHA through the web UI and the API — including via the refs for
closed PRs #1 and #2, which a branch force-push does not touch. Only GitHub
can remove them.

Open a support request at <https://support.github.com/> asking them to garbage
collect unreachable objects, quoting the six blob ids above and the repository
name. Until they confirm, treat the data as still published.

Everyone with a clone then runs:

```bash
git fetch origin
git reset --hard origin/<branch>
```

or re-clones. A `git pull` on an old clone merges the old history back and
undoes the work.

## Is a rewrite enough?

No. The data was public to everyone with repository access for the period it
was pushed, and a rewrite cannot retract what was already fetched. Treat this
as a disclosure: the addresses, names and phone numbers of real ConnectWise
contacts were readable by anyone with access to this repository. Whether that
needs reporting is a question for whoever owns data protection here, not a
technical one.

## Not repeating it

`tests/test_committed_traces.py` fails if any tracked file under `traces/`
lacks pseudonym tokens or contains a real address. It runs in CI. That is the
check whose absence let this happen: `scrub_trace.py` worked, was tested and
was documented — nothing ever verified it had been *run*.
