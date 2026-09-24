# Shortcuts for the frozen sets and the pieces around them.
# `python` not `python3` on Windows.
PY ?= python3
FULL_TRIAGE := traces/2026-09-03-full-triage.json
OPS_WORST   := traces/2026-09-15-ops-worst-case.json
TRIAGE      := traces/2026-09-23-triage-analysis.json
FT_BASELINE := baselines/full-triage-2026-09-18.json
OW_BASELINE := baselines/ops-worst-case-2026-09-18.json
TA_BASELINE := baselines/triage-analysis-2026-09-23.json

.PHONY: help test evals evals-ops evals-triage baselines manifest-skeleton foundry foundry-dataset foundry-register cassettes replay replay-package replay-deploy replay-verify clean

help:
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  %-18s %s\n", $$1, $$2}'

test:  ## unit tests + frozen-set replay (no Azure, no network)
	$(PY) -m pytest tests/ -q

evals:  ## score the known-good set against its baseline
	$(PY) trace_to_eval.py $(FULL_TRIAGE) -o out --tool-defs tool_manifests/ \
	    --skill-registry skills
	$(PY) run_evals.py out/eval_runs.jsonl --expected expected.json \
	    --baseline $(FT_BASELINE) --json artifacts/full-triage.json

evals-ops:  ## score the known-bad set against its baseline
	$(PY) trace_to_eval.py $(OPS_WORST) -o out-ops --tool-defs tool_manifests/
	$(PY) run_evals.py out-ops/eval_runs.jsonl --expected expected.json \
	    --baseline $(OW_BASELINE) --json artifacts/ops-worst-case.json

evals-triage:  ## score the standalone triage-analysis set against its baseline
	$(PY) trace_to_eval.py $(TRIAGE) -o out-triage --tool-defs tool_manifests/
	$(PY) run_evals.py out-triage/eval_runs.jsonl --expected expected.json \
	    --baseline $(TA_BASELINE) --json artifacts/triage-analysis.json

baselines:  ## re-freeze every baseline from the committed traces
# Must use the same --tool-defs as `evals`/`evals-ops`, or every run reports
# evaluator_ready as a fix and valid_tool_args as newly scored.
	$(PY) trace_to_eval.py $(FULL_TRIAGE) -o out --tool-defs tool_manifests/ \
	    --skill-registry skills
	-$(PY) run_evals.py out/eval_runs.jsonl --expected expected.json \
	    --json $(FT_BASELINE)
	$(PY) trace_to_eval.py $(OPS_WORST) -o out-ops --tool-defs tool_manifests/
	-$(PY) run_evals.py out-ops/eval_runs.jsonl --expected expected.json \
	    --json $(OW_BASELINE)
	$(PY) trace_to_eval.py $(TRIAGE) -o out-triage --tool-defs tool_manifests/
	-$(PY) run_evals.py out-triage/eval_runs.jsonl --expected expected.json \
	    --json $(TA_BASELINE)

cassettes:  ## build replay cassettes from the committed traces
	$(PY) replay/make_cassette.py $(FULL_TRIAGE) -o cassettes
	$(PY) replay/make_cassette.py $(OPS_WORST) -o cassettes
	$(PY) replay/make_cassette.py $(TRIAGE) -o cassettes

replay:  ## serve a cassette as an MCP toolbox (no ConnectWise, no writes)
	@test -n "$(CASSETTE)" || { echo "usage: make replay CASSETTE=cassettes/<file>.json"; exit 2; }
	$(PY) replay/replay_server.py $(CASSETTE) --tool-defs tool_manifests/ \
	    --journal artifacts/replay-journal.json

replay-package:  ## assemble the Azure Function deployment package
	functions/replay-mcp/build.sh

replay-deploy:  ## provision and publish the hosted replay server
# REPLAY_TOKEN is required and deliberately not defaulted: the cassettes carry
# ticket and company identifiers and this is the only thing in front of them.
	@test -n "$(RG)" || { echo "usage: REPLAY_TOKEN=... make replay-deploy RG=<resource-group>"; exit 2; }
	functions/replay-mcp/deploy.sh $(RG) $(or $(LOCATION),eastus2)

replay-verify:  ## replay every cassette against the hosted server and compare
# Deploying it is not the same as it being right.
	@test -n "$(URL)" || { echo "usage: REPLAY_TOKEN=... make replay-verify URL=https://<app>.azurewebsites.net"; exit 2; }
	$(PY) functions/replay-mcp/verify.py $(URL)

foundry-dataset:  ## build the Foundry evaluation dataset from the frozen set
	$(PY) foundry/to_foundry_dataset.py $(FULL_TRIAGE) --expected expected.json \
	    --tool-defs tool_manifests/ -o artifacts/foundry-dataset.jsonl

foundry-register:  ## print the evaluator payloads without calling Foundry
	$(PY) foundry/register_evaluators.py --dry-run

foundry:  ## convert to the Foundry judged-evaluator schema (no judge calls)
	$(PY) foundry/submit_to_foundry.py out/eval_runs.jsonl --dry-run --sample 0

manifest-skeleton:  ## skeleton manifest from the traces (no schemas)
# Writes to artifacts/, NOT tool_manifests/ — the real manifest already lives
# there and the converter loads every file in the directory.
	$(PY) tools/extract_tool_manifest.py --from-trace $(FULL_TRIAGE) \
	    --toolbox ConnectwiseMCP --version 5 \
	    -o artifacts/connectwisemcp-skeleton.json

clean:
	rm -rf out out-ops out-triage artifacts skills cassettes .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
