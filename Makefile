# Shortcuts for the two frozen sets and the pieces around them.
# `python` not `python3` on Windows.
PY ?= python3
FULL_TRIAGE := traces/2026-09-03-full-triage.csv
OPS_WORST   := traces/2026-09-15-ops-worst-case.csv
FT_BASELINE := baselines/full-triage-2026-09-16.json
OW_BASELINE := baselines/ops-worst-case-2026-09-16.json

.PHONY: help test evals evals-ops baselines manifest-skeleton foundry foundry-dataset foundry-register cassettes replay clean

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

baselines:  ## re-freeze both baselines from the committed traces
	$(PY) trace_to_eval.py $(FULL_TRIAGE) -o out
	-$(PY) run_evals.py out/eval_runs.jsonl --expected expected.json \
	    --json $(FT_BASELINE)
	$(PY) trace_to_eval.py $(OPS_WORST) -o out-ops
	-$(PY) run_evals.py out-ops/eval_runs.jsonl --expected expected.json \
	    --json $(OW_BASELINE)

cassettes:  ## build replay cassettes from the committed traces
	$(PY) make_cassette.py $(FULL_TRIAGE) -o cassettes
	$(PY) make_cassette.py $(OPS_WORST) -o cassettes

replay:  ## serve a cassette as an MCP toolbox (no ConnectWise, no writes)
	@test -n "$(CASSETTE)" || { echo "usage: make replay CASSETTE=cassettes/<file>.json"; exit 2; }
	$(PY) replay_server.py $(CASSETTE) --tool-defs tool_manifests/ \
	    --journal artifacts/replay-journal.json

foundry-dataset:  ## build the Foundry evaluation dataset from the frozen set
	$(PY) to_foundry_dataset.py $(FULL_TRIAGE) --expected expected.json \
	    --tool-defs tool_manifests/ -o artifacts/foundry-dataset.jsonl

foundry-register:  ## print the evaluator payloads without calling Foundry
	$(PY) register_evaluators.py --dry-run

foundry:  ## convert to the Foundry judged-evaluator schema (no judge calls)
	$(PY) submit_to_foundry.py out/eval_runs.jsonl --dry-run --sample 0

manifest-skeleton:  ## seed tool_manifests/ from the traces (no schemas)
	$(PY) extract_tool_manifest.py --from-trace $(FULL_TRIAGE) \
	    --toolbox ConnectwiseMCP --version 5 \
	    -o tool_manifests/connectwisemcp-v5.json

clean:
	rm -rf out out-ops artifacts skills cassettes .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
