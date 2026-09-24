"""The continuous-evaluation rule, without Foundry.

The payload is built as a plain dict so it is inspectable and testable
offline; create_or_update accepts a MutableMapping as well as the model.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "foundry"))
import continuous_eval as ce                              # noqa: E402


def test_the_rule_id_is_stable_so_reruns_update(tmp_path):
    """An id derived from the agent means re-running updates the rule rather
    than accumulating a new one per invocation."""
    assert ce.rule_id("triage-orchestrator") == "triage-orchestrator-continuous"
    assert ce.rule_id("triage-orchestrator") == ce.rule_id("triage-orchestrator")


def test_the_payload_filters_to_one_agent():
    p = ce.rule_payload("triage-analysis-agent", "eval_1", 25.0, 500)
    assert p["filter"]["agentName"] == "triage-analysis-agent"
    assert p["eventType"] == "ResponseCompleted"
    assert p["action"]["type"] == "ContinuousEvaluation"
    assert p["action"]["evalId"] == "eval_1"


def test_the_payload_carries_both_sampling_controls():
    p = ce.rule_payload("a", "e", 40.0, 250)
    assert p["action"]["samplingRate"] == 40.0
    assert p["action"]["maxHourlyRuns"] == 250


def test_a_rule_can_be_built_disabled():
    assert ce.rule_payload("a", "e", 10.0, 100, enabled=False)["enabled"] is False


# --- the percent-versus-fraction trap ---------------------------------------

@pytest.mark.parametrize("bad", [0, -5, 101, 1000])
def test_sampling_outside_the_range_is_refused(bad):
    with pytest.raises(SystemExit):
        ce.validate_sampling(bad)


@pytest.mark.parametrize("ambiguous", [0.25, 0.5, 0.99])
def test_a_fraction_shaped_value_is_refused_rather_than_guessed(ambiguous):
    """The docs say samplingPercent 0-100; the SDK field is sampling_rate.
    A bare 0.25 could mean 25% or 0.25%, and guessing wrong samples 100x off
    in silence. Refuse and make the caller say which."""
    with pytest.raises(SystemExit) as exc:
        ce.validate_sampling(ambiguous)
    assert "PERCENT" in str(exc.value)


@pytest.mark.parametrize("good", [1, 10, 25, 100])
def test_a_sane_percent_is_accepted(good):
    assert ce.validate_sampling(good) == float(good)


def test_the_default_samples_higher_than_judged_guidance():
    """Continuous-evaluation guidance suggests 5-10% because the usual
    evaluators are LLM judges and every sample is an inference call. Ours are
    code-based, so sampling costs compute rather than tokens."""
    assert ce.DEFAULT_SAMPLING_PERCENT >= 25


def test_the_dry_run_creates_nothing(capsys):
    assert ce.main(["--agent", "a", "--eval-id", "e", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "nothing was created" in out
    assert json.loads(out.split("\n\n")[0])["rule"]["action"]["evalId"] == "e"


def test_dry_run_still_validates_sampling():
    with pytest.raises(SystemExit):
        ce.main(["--agent", "a", "--eval-id", "e", "--dry-run",
                 "--sampling-percent", "0.5"])
