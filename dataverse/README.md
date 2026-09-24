# Outcome evaluation

Every check in `run_evals.py` is process quality: did the agent behave tidily.
None say whether it was **right**. A run passes all eight having proposed
entirely the wrong company. §8 of `docs/HANDOFF.md` calls closing that the
single highest-value gap.

The ground truth already exists in production. It is not a metric anyone has
to design:

```
AI Orchestration   one workflow run against one entity (one ticket)
  └─ AI Run        one agent execution
       └─ AI Decision   one proposed field change
            └─ AI Review   human disposition + reason code (1:1, optional)
```

`AI Review` snapshots the AI's suggestion, records the human's final value
beside it, and carries a reason code for why it changed. Those codes — Human
Corrected Classification, Source Data Incorrect, Business Rule Exception — are
a failure taxonomy the reviewers have been using for months. **Operationalise
that; do not invent new metrics.**

The three-way label is more useful than it looks:

| disposition | what it means |
|---|---|
| approved, no override | gold trajectory **and** gold outcome |
| **modified, small changes** | gold trajectory, corrected outcome |
| rejected | negative example |

The middle bucket is the diagnostic one. Clean trajectories there mean the
tools and skills are fine and synthesis is the gap. Messy trajectories mean the
reverse.

## Status

Decisions are landing; reviews are thin — reviewers lag runs by days. So this
directory is built to be **correct and unscored** rather than approximately
scored. Nothing here reports a number it cannot stand behind.

## The join is the open question, and it is not a detail

A trace row is keyed by `orchestration_id`, the App Insights `operation_Id`.
A Dataverse orchestration row is keyed by its own guid. Nothing obliges them
to know about each other.

**Ticket id will not substitute.** In the committed frozen set, both
orchestrations ran against ticket **805392** — the same ticket triaged twice.
Joining on ticket maps one trace to two orchestrations and picks whichever
comes back first, silently. That produces confident wrong numbers rather than
an error, which is worse than having no join at all.

So the first thing to establish is whether the orchestration record carries the
`operation_Id`. `--probe` answers exactly that:

```bash
python3 dataverse/fetch_outcomes.py --probe
```

It lists the environment's entity sets, matches the four AI tables through
whatever publisher prefix your environment uses, dumps a sample row of each,
and reports which attributes on the orchestration row could be the join key —
recognising an `operation_Id` by shape (32 hex characters, no hyphens, which a
Dataverse guid is not).

Two outcomes:

- **A candidate is found.** Put it in `schema.json` as
  `fields.orchestration_key` and the join is real.
- **Nothing is found.** The fix belongs in the **agents repo**: write the
  `operation_Id` onto the orchestration record when it is created. Until that
  ships, outcome evaluation cannot be keyed reliably, and this tooling says so
  rather than guessing.

## schema.json

Every logical name in it is currently a **guess**, and `_probed` is `null` to
say so. A test asserts that, because scoring against wrong logical names
returns empty results that read as "no reviews yet" rather than as a
misconfiguration — the same failure mode as the vacuous `valid_tool_args`
that started this whole thread.

After probing, replace the names and set `_probed` to the date.

## Fetching

```bash
python3 dataverse/fetch_outcomes.py --since 2026-09-01 -o artifacts/outcomes.jsonl
```

One normalised row per review. It reports how many carry a join key, and says
plainly when none do.

## Credentials

`DATAVERSE_URL`, `DATAVERSE_CLIENT_ID`, `DATAVERSE_CLIENT_SECRET`,
`DATAVERSE_TENANT_ID`. See [`docs/CREDENTIALS.md`](../docs/CREDENTIALS.md).

`DATAVERSE_URL` must be **https**. The token scope is
`{DATAVERSE_URL}/.default`, and that has to be the registered resource
identifier — an `http://` URL yields a scope matching no resource and Entra
rejects the request with a 400 **from the token endpoint**, before Dataverse
is contacted at all, so it reads as a credential problem rather than a typo in
the host. The script normalises the scheme and strips a trailing slash, and
says so when it does.

The one that catches people: an app registration alone grants **nothing**. The
application user inside the Dataverse environment is what authorises. A 401
from this script says so in its error rather than leaving you reading network
traces.

## Not built yet, deliberately

The outcome checks themselves. They need the join settled first — a check that
cannot identify which run it is scoring is not a check. Once `--probe` names
the key, the checks are small: disposition rate per agent and intent, reason
code distribution, and the middle-bucket cross-tab against trajectory quality
that tells you whether the gap is tools or synthesis.
