# ASSERT, and where it fits

`responsibleai/ASSERT` — *Adaptive Spec-driven Scoring for Evaluation and
Regression Testing*. Microsoft Research, MIT-licensed, open-sourced at Build
2026. It matters here because it overlaps our trajectory check, and because
it is native in the sense that matters: Microsoft-published, framework
agnostic, and it reads OTel traces, which is exactly what this repo already
produces.

## What it does

Plain-language behaviour spec → inspectable taxonomy of acceptable and
unacceptable behaviours → **generated** stratified test cases → run against
the target → score each failure against the policy statement that produced
it. It records the path taken, intermediate actions and tool calls included,
so a failure points at where it happened rather than just that it happened.

It targets hosted models, callable wrappers, or OTel-traced agents, and it
has regression testing built in: swap a model or edit a prompt, and it
reports the behavioural drift.

## It does not replace the cassette replay

The two differ on the axis that matters, and it is not quality:

| | ASSERT | cassette replay |
|---|---|---|
| Inputs | **generated** from a spec | **fixed**, recorded |
| Tools | real, or whatever the target uses | **stubbed**, recorded responses |
| Question | does the agent obey policy across many scenarios? | did *this change* alter *this* trajectory? |
| Repeatability | stochastic by design | byte-identical |
| Answers "is it safe to ship?" | broadly | precisely |

ASSERT explores. The replay pins. A generated scenario that fails tells you
the agent violates a policy somewhere in a space; a replay divergence tells
you which call changed between version 82 and 83. Neither substitutes for the
other, and a suite with only the first cannot attribute a regression to a
change.

## Where it would earn its keep here

**`expected.json` is hand-written, and that is its ceiling.** Four trajectory
expectations, curated by us, covering the intents we thought of. ASSERT
generates scenarios from a spec instead — and the spec already exists in
prose, in the orchestration and classification skills the agents load. That
is the honest gap in the current suite: coverage is bounded by imagination.

**The reason codes are a taxonomy already.** §8 of `docs/HANDOFF.md` notes
that reviewers have been using Human Corrected Classification, Source Data
Incorrect, Business Rule Exception and the rest for months. ASSERT's model is
policy statement → generated cases → failures scored against the statement
that produced them. Those reason codes are policy statements that have
already survived contact with reality.

**It is trace-aware.** `export_traces.py` already produces OTel spans with
`gen_ai.*` attributes. Feeding those to ASSERT is plausibly a conversion, not
an integration.

## Can a generated failure be pinned? Yes.

This was the question that decided whether the graduation path is real, and
the answer is that ASSERT already works this way:

- **Every stage writes local artifacts** under
  `artifacts/results/<suite>/<run>/`. Local-first by design, not a dashboard
  you have to scrape.
- **Existing test cases, taxonomies and transcripts are reused when
  applicable.** Generated cases are durable inputs, not throwaway.
- **`assert-ai-action` gates pull requests against a cached baseline**, using
  a paired-binary McNemar test for whether a change is a real regression
  rather than noise — which is the same problem `--baseline-agent-id` solves
  in the live staging replay, solved properly.
- There is an **ASSERT → Foundry exporter** in flight (PR #267), so results
  need not stay local.

So the shape works: ASSERT explores, a failing scenario becomes a saved case,
and the ones worth pinning deterministically graduate into `replay/` as
cassettes. Exploration finds the case; the cassette makes it repeatable and
attributable.

The open question is narrower than it was — not *can* a case be pinned, but
whether a failing ASSERT scenario can be **recorded against live tools once**
to produce the cassette. That is the same capture `make_cassette.py` already
does from a trace, so the likely answer is: run the generated scenario once
in staging, export the trace, make a cassette from it. No new machinery.

## Recommendation

Spend an hour on it before extending `trajectory` or hand-writing more
`expected.json` entries. Specifically worth establishing:

1. Can it consume App Insights `gen_ai.*` spans directly, or does it want its
   own harness around a callable?
2. Does its regression mode compare two agent *versions*, or a version
   against a spec? Only the first competes with the replay.
3. Can a generated scenario be pinned once it finds a failure, so it becomes
   a fixed regression case rather than a lottery ticket?

If (3) is yes, the natural shape is ASSERT generating cases, failures
graduating into `replay/` as cassettes, and the deterministic gate growing
from what exploration finds. That would fix the coverage ceiling without
giving up attribution.

Not adopted yet. Recorded so the decision is deliberate rather than
forgotten.

Sources: <https://github.com/responsibleai/ASSERT>,
<https://devblogs.microsoft.com/foundry/build-2026-open-trust-stack-ai-agents/>
