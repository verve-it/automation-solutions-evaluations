# Shortcuts for the two frozen sets and the pieces around them.
# `python` not `python3` on Windows.
PY ?= python3
FULL_TRIAGE := traces/2026-09-03-full-triage.csv
OPS_WORST   := traces/2026-09-15-ops-worst-case.csv
FT_BASELINE := baselines/full-triage-2026-09-16.json
OW_BASELINE := baselines/ops-worst-case-2026-09-16.json

.PHONY: help test evals evals-ops baselines manifest-skeleton clean

help:
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  %-18s %s\n", $$1, $$2}'

test:  ## unit tests + frozen-set replay (no Azure, no network)
	$(PY) -m pytest tests/ -q

evals:  ## score the known-good set against its baseline
	$(PY) trace_to_eval.py $(FULL_TRIAGE) -o out --tool-defs tool_manifests/
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

manifest-skeleton:  ## seed tool_manifests/ from the traces (no schemas)
	$(PY) extract_tool_manifest.py --from-trace $(FULL_TRIAGE) \
	    --toolbox ConnectwiseMCP --version 5 \
	    -o tool_manifests/connectwisemcp-v5.json

clean:
	rm -rf out out-ops artifacts .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
