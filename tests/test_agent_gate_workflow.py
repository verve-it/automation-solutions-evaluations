"""The gate workflow, checked for the things that make it a gate.

A workflow cannot be run here, so this asserts the properties that decide
whether it gates anything at all. Each one has a way of being quietly wrong:
a step that reports instead of failing, a cassette that should have been
skipped, a summary that never reaches the builder.
"""
import os

import pytest

from conftest import REPO

yaml = pytest.importorskip("yaml")
GATE = os.path.join(REPO, ".github", "workflows", "agent-gate.yml")


@pytest.fixture(scope="module")
def workflow():
    with open(GATE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def steps(workflow):
    return workflow["jobs"]["replay"]["steps"]


def _run_text(steps):
    return "\n".join(s.get("run", "") for s in steps)


def test_it_is_callable_from_the_repository_that_deploys(workflow):
    """The agents merge elsewhere. A gate they cannot call gates nothing."""
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "workflow_call" in triggers
    assert "environment" in triggers["workflow_call"]["inputs"]
    assert triggers["workflow_call"]["secrets"]["REPLAY_TOKEN"]["required"]


def test_both_environments_are_reachable(workflow):
    """staging and prod, and the job binds to one so OIDC resolves."""
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert set(triggers["workflow_dispatch"]["inputs"]["environment"]
               ["options"]) == {"staging", "prod"}
    assert workflow["jobs"]["replay"]["environment"]["name"] == \
        "${{ inputs.environment }}"


def test_the_scores_reach_the_builder(steps):
    """A red build has to show which check failed, not just that one did."""
    assert "$GITHUB_STEP_SUMMARY" in _run_text(steps)
    assert "--summary" in _run_text(steps)


def test_the_detail_lands_in_foundry(steps):
    """Foundry -> Evaluation is where a run gets opened and read."""
    text = _run_text(steps)
    assert "run_cloud_eval.py" in text
    assert "to_foundry_dataset.py" in text

    foundry = [s for s in steps if "run_cloud_eval.py" in s.get("run", "")][0]
    # A failed gate is exactly when someone wants to open the run.
    assert "always()" in foundry.get("if", "")


def test_the_replay_server_is_verified_before_an_agent_is_pointed_at_it(steps):
    """Otherwise every call diverges and the gate blames the agent."""
    names = [s.get("name", "") for s in steps]
    verify = next(i for i, s in enumerate(steps)
                  if "verify.py" in s.get("run", ""))
    replay = next(i for i, s in enumerate(steps)
                  if "run_replay.py" in s.get("run", ""))
    assert verify < replay, names


def test_orchestrations_are_skipped_not_merely_refused(steps):
    """An orchestration's children run their production versions, which are
    still pointed at the real ConnectWise toolbox -- writes included.

    run_replay.py refuses one, but a gate that relies on a refusal to avoid
    live writes is one flag away from doing them. This skips them itself.
    """
    text = _run_text(steps)
    assert "agents" in text and "!= \"1\"" in text
    assert "skip" in text


def test_replaying_nothing_fails_the_build(steps):
    """A gate that silently gates nothing is worse than no gate."""
    text = _run_text(steps)
    assert 'ran" = "0"' in text
    assert "nothing was gated" in text


def test_a_missing_setting_fails_early_and_says_which(steps):
    text = _run_text(steps)
    for name in ("AZURE_AI_PROJECT_ENDPOINT", "REPLAY_SERVER_URL",
                 "REPLAY_TOKEN"):
        assert name in text


def test_the_replay_step_fails_the_job_on_a_nonzero_exit(steps):
    """`| tee` swallows the exit code without pipefail, so the gate would
    pass while the replay failed."""
    replay = [s for s in steps if "run_replay.py" in s.get("run", "")][0]
    assert "pipefail" in replay["run"]
