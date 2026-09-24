# Traces

> ## ACTION REQUIRED: three of the four committed traces were never scrubbed
>
> Not "scrubbed imperfectly" — **never run through `scrub_trace.py` at all.**
> A scrubbed trace carries `PERSON_`/`EMAIL_`/`COMPANY_` pseudonym tokens
> where the data used to be. Three files have zero.
>
> | file | size | tokens | real e-mails | |
> |---|---|---|---|---|
> | `2026-09-03-full-triage.json` | 25 MB | 8,417 | 0 | scrubbed |
> | `2026-09-03-full-triage.csv` | 5.1 MB | **0** | **67** | **not scrubbed** |
> | `2026-09-03-full-triage-rejoined.json` | 25 MB | **0** | **71** | **not scrubbed** |
> | `2026-09-15-ops-worst-case.csv` | 1.3 MB | **0** | **2** | **not scrubbed** |
>
> The 67 addresses in the `.csv` are real customer contacts at real customer
> domains, alongside personal names and ~43 phone numbers. The two `.csv`
> files are the ones every baseline and every CI run scores against, and they
> have been in the repo since the first commits.
>
> `2026-09-03-full-triage-rejoined.json` is the raw `AppGenAIContent` rejoin.
> It is untruncated production content by construction — see
> `docs/TELEMETRY.md`, which says so — and it went in unmodified.
>
> `tests/test_committed_traces.py` fails on all three. That failure is the
> accurate state of the repository, not a broken test. It passes for
> `2026-09-03-full-triage.json`, which shows the workflow does work when it
> is run.
>
> To remediate, per file:
>
> ```bash
> python3 scrub_trace.py traces/<file> --learn candidates.json
> # review candidates.json by hand -- delete every entry that is ConnectWise
> # vocabulary rather than customer data; protected terms are withheld for you
> python3 scrub_trace.py traces/<file> \
>     --redact-file candidates.json --verify -o traces/<file>
> ```
>
> Then `make baselines` and confirm no verdict moved.
>
> Decide separately whether `-rejoined.json` should be committed at all. It is
> 25 MB of raw production content whose only consumer is a one-off truncation
> comparison, and `2026-09-03-full-triage.json` already covers the scored
> path.
>
> **Rewriting the files does not remove them from git history, and the branch
> is pushed.** Treat the data as disclosed to everyone with repository access
> and decide with whoever owns the repo whether history needs rewriting.



Raw span exports. Dated, named for what they contain, and **committed** — each
one is the input to a frozen baseline in `baselines/`, so the suite is
reproducible without Azure access.

| File | Captured | What it is |
|---|---|---|
| `2026-09-03-full-triage.json` | 2026-09-03 | Two complete Full Triage orchestrations, 7 agent runs, 715 spans. The known-good set. Exported with the `AppGenAIContent` join, so tool results are untruncated. |
| `2026-09-15-ops-worst-case.json` | 2026-09-15 | The two worst observed `connectwise-operations-agent` runs, unlinked (single-agent traces). The known-bad set. |
| `2026-09-23-triage-analysis.json` | 2026-09-23 | Five standalone `triage-analysis-agent` runs (`Automated flow: triage ticket N`, agent v98 and v99), 663 spans. Every ConnectWise call in them is a read, so each replays fully stubbed with no write to guard: the agent gate's first cassettes for this agent. Exported with the `AppGenAIContent` join; no truncated results. A sixth run from the same hour is deliberately absent: it carries a third party's legal and identity details, which pseudonyms do not de-identify. |

All are JSON. The `.csv` forms were the original portal exports and are gone:
they were never scrubbed, and `scrub_trace.py` writes JSON whatever it reads,
so scrubbing one renames it anyway. `load_spans` has always read both.

> **The 2026-09-03 and 2026-09-15 sets predate `cwpsa-mcp` `cbf4e2b`** (see
> `docs/MCP-SERVER-FINDINGS.md`). Several failures in the known-bad set —
> the empty `type`/`subtype`/`item` resolves, the four-deep `cw_resolve`
> cascade they caused — are server bugs that no longer exist. That does
> not weaken these sets as the regression gate for the **eval code**,
> which is what they are for: the spans are fixed input and the scores
> must not drift. It does mean they are no longer reproducible agent
> behaviour. Do not read `ops-worst-case` as a live statement about how
> the ops agent behaves today; capture a fresh known-bad set once there
> are post-fix traces worth freezing.

`traces/auto/` is where `export_traces.py` lands unattended exports. It is
gitignored: promote a run into a dated file here by hand, with a row in this
table, when it is worth freezing a baseline against.

## Exporting by hand

Portal → App Insights → Logs. **Keep `customDimensions` intact** — a flattening
projection strips every `gen_ai.*` attribute and the export becomes worthless.

```kusto
let ids = dynamic(["<operation_id>", "..."]);
dependencies
| where timestamp > ago(30d)
| where operation_Id in (ids)
| project timestamp, name, id, operation_Id, operation_ParentId,
          duration, success, customDimensions
| order by timestamp asc
```

To find orchestrations worth exporting, ranked by how evaluable they are:

```kusto
let newAgents = dynamic(["triage-orchestrator","triage-analysis-agent",
                         "connectwise-operations-agent","triage-evaluation-agent"]);
dependencies
| where timestamp > ago(14d)
| extend d = customDimensions
| extend agent = tostring(d["gen_ai.agent.name"])
| where agent in (newAgents)
| summarize started = min(timestamp), agents = make_set(agent),
            agent_count = dcount(agent), spans = count(),
            tool_calls = countif(name startswith "execute_tool"),
            has_tool_defs = countif(isnotempty(tostring(d["gen_ai.tool.definitions"])))
  by operation_Id
| extend usable = case(agent_count > 1 and tool_calls > 0 and has_tool_defs > 0, "1-full",
                       tool_calls > 0 and has_tool_defs > 0, "2-single agent",
                       tool_calls > 0, "3-no tool defs", "4-thin")
| order by usable asc, started desc
```

`export_traces.py --dry-run` prints both of these for whatever window and
filters you give it.

## Format notes

- **JSON export is safer than CSV.** CSV escaping of `customDimensions` has
  caused parse failures. The converter reads both.
- The portal's CSV download names the time column `timestamp [UTC]`, not
  `timestamp`. The converter handles the alias and normalises the locale
  format (`9/3/2026, 5:29:42.893 PM`) to sortable UTC ISO.
- **Foundry Traces retains 90 days.** Dataverse rows are permanent. Materialise
  eval rows on a rolling basis or old traces age out before their reviews land.

## A known blind spot in the scrubber

`sweep()` rewrites **strings**. A value that appears in a payload as a JSON
number is never touched:

```json
{"conditions": {"field": "id", "op": "=", "value": 4597}}
```

Declaring `4597` in a redaction list produces a `RESIDUAL` report rather than
a silent pass, so the gap is visible — but it is a gap. ConnectWise record ids
are deliberately left in: an opaque integer is not identifying on its own, and
retyping one to a string token risks changing what the converter parses out of
a tool argument.

If a phone number or an account number ever arrives as a JSON number rather
than a string, this is where it will survive. The residual check will say so.

## The salt

`scrub_trace.py` needs `--salt` or `$SCRUB_SALT`, at least 16 characters. It
refuses without one, because unsalted tokens are a plain hash of the value and
trivially reversible against a name list.

**One salt for this repo, for ever.** The same salt gives the same person the
same token in every trace, so a cascade of retries against one customer still
reads as one customer after scrubbing — across files, not just within one.
Change the salt and that property silently disappears.

Keep it where CI can read it and people cannot: a GitHub Actions secret named
`SCRUB_SALT`, mirrored into your password manager. It is **not** in this repo
and must never be. Generate one with:

```bash
python3 -c "import secrets; print(secrets.token_hex(16))"
```

### Telling whether two traces share a salt

Every scrub writes `<trace>.scrub.json` beside the output:

```json
{"salt_fingerprint": "f0039a8c92d3", "literals": 18, "tokens_issued": 18,
 "source": "2026-09-15-ops-worst-case.json", "scrubbed_utc": "..."}
```

The fingerprint is a hash of a fixed constant under the salt. It identifies
which salt was used without storing it and without helping anyone recover a
token. Two traces with the same fingerprint are comparable; different
fingerprints mean the same person has two different tokens and you should not
read across them.

> **Current state:** `2026-09-03-full-triage.json` and
> `2026-09-15-ops-worst-case.json` were scrubbed with **different** salts, and
> both predate the sidecar, so neither carries a fingerprint.
> `2026-09-23-triage-analysis.json` is committed with its sidecar, scrubbed
> with the repo salt. `test_every_committed_scrub_used_the_one_repo_salt`
> fails the day a sidecar with a second fingerprint is committed. Nothing joins
> across traces today so no check is affected. The two older traces still
> want re-scrubbing with the repo salt from their reviewed redaction lists;
> the scrub is deterministic, so the result is byte-stable and their sidecars
> will then agree with this one.
>
> **A new trace also needs a replay-server redeploy** before it reaches
> `main`: the gate replays every cassette `make cassettes` builds, and
> `verify.py` fails, naming the cassette, if the server was never given it.
