# Traces

> ## ACTION REQUIRED: both committed sets still carry customer data
>
> `scrub_trace.py --learn` scanned only for e-mail addresses in any payload
> that failed `json.loads`. **40% of the payloads in these files are in that
> category** — 239 of 598 in `2026-09-03-full-triage.csv`, including 117 tool
> results and 60 input-message blobs, which is exactly where customer data
> lives. Names and phone numbers there were never proposed, so never
> reviewed, never redacted, and never reported as residual by `--verify`.
> The scrub passed and the data shipped.
>
> The bug is fixed (see `scrub_trace.py`'s `scan_text`), but **the fix does
> not retroactively clean these files.** Re-running the learner over them now
> proposes 302 candidates against the previous 104, and the new ones include
> real personal names, customer company names, and 43 real phone numbers.
>
> To remediate:
>
> ```bash
> python3 scrub_trace.py traces/2026-09-03-full-triage.csv --learn candidates.json
> # review candidates.json by hand -- delete every entry that is ConnectWise
> # vocabulary rather than customer data; protected terms are withheld for you
> python3 scrub_trace.py traces/2026-09-03-full-triage.csv \
>     --redact-file candidates.json --verify -o traces/2026-09-03-full-triage.csv
> ```
>
> Then re-freeze the baselines and confirm no verdict moved. Repeat for
> `2026-09-15-ops-worst-case.csv`. Until that is done, treat this directory as
> containing production customer data and do not share the repo outside the
> people who already have ConnectWise access.
>
> Rewriting the files is not enough on its own — the data is in git history
> too. Decide with whoever owns the repo whether history needs rewriting or
> the repo re-creating.


Raw span exports. Dated, named for what they contain, and **committed** — each
one is the input to a frozen baseline in `baselines/`, so the suite is
reproducible without Azure access.

| File | Captured | What it is |
|---|---|---|
| `2026-09-03-full-triage.csv` | 2026-09-03 | Two complete Full Triage orchestrations, 7 agent runs, 715 spans. The known-good set. |
| `2026-09-15-ops-worst-case.csv` | 2026-09-15 | The two worst observed `connectwise-operations-agent` runs, unlinked (single-agent traces). The known-bad set. |

> **Both sets predate `cwpsa-mcp` `cbf4e2b`** (see
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
