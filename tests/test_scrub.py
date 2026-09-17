"""Scrubbing a trace for commit.

Two failure modes matter and both are silent: leaving customer data in
(the whole point), and redacting ConnectWise vocabulary the checks score,
which rewrites a trajectory while every file still looks fine.
"""
import json

import pytest

import scrub_trace as s


@pytest.fixture
def pseudo():
    return s.Pseudonymiser("a-salt")


def row(**payload):
    return {"customDimensions": json.dumps(payload)}


# --- pseudonyms -------------------------------------------------------------

def test_one_value_always_yields_one_token(pseudo):
    """A cascade of retries against one target must still read as one
    target after scrubbing."""
    a = pseudo.token("A.S. Economou Development", "COMPANY")
    b = pseudo.token("A.S. Economou Development", "COMPANY")
    assert a == b and a.startswith("COMPANY_")


def test_different_salts_give_different_tokens():
    v = "A.S. Economou Development"
    assert s.Pseudonymiser("x").token(v) != s.Pseudonymiser("y").token(v)


def test_tokens_are_case_insensitive_so_prose_and_fields_agree(pseudo):
    assert pseudo.token("Eli Seale") == pseudo.token("eli seale")


# --- the sweep --------------------------------------------------------------

def test_a_name_in_free_prose_is_swept(pseudo):
    """Most customer data is not in a structured field. Key-based redaction
    alone left 663 of 675 occurrences of one contact name in place."""
    sweeper = s.build_sweeper({"Eli Seale": "PERSON"}, pseudo)
    out = s.sweep("## Request\nEli Seale's laptop will not turn on.",
                  pseudo, sweeper)
    assert "Eli Seale" not in out
    assert "PERSON_" in out


def test_longest_literal_wins(pseudo):
    """Otherwise 'Economou' fires first and strands 'A.S. ... Development'."""
    sweeper = s.build_sweeper(
        {"A.S. Economou Development": "COMPANY", "Economou": "PERSON"}, pseudo)
    out = s.sweep("resolve A.S. Economou Development", pseudo, sweeper)
    assert "Economou" not in out
    assert out.count("_") >= 1


def test_emails_and_phones_go_without_being_declared(pseudo):
    out = s.sweep("contact eli@verveit.com or 209-244-7120 today", pseudo, None)
    assert "eli@verveit.com" not in out and "EMAIL_" in out
    assert "209-244-7120" not in out and "PHONE_" in out


def test_sweeping_preserves_json_structure(pseudo):
    sweeper = s.build_sweeper({"Mettle": "COMPANY"}, pseudo)
    raw = json.dumps({"reference_type": "company", "query": "Mettle"})
    out = json.loads(s.sweep_payload(raw, pseudo, sweeper))
    assert out["reference_type"] == "company"      # vocabulary untouched
    assert out["query"].startswith("COMPANY_")


# --- what must NOT be proposed ---------------------------------------------

def test_tool_names_are_never_proposed():
    """Redacting the call_tool envelope's `name` rewrites the trajectory."""
    rows = [row(**{"gen_ai.tool.call.arguments": json.dumps(
        {"name": "ConnectWise-PSA-ForAgents___cw_resolve",
         "arguments": {"query": "Some Company Name"}})})]
    proposed = s.propose(rows)
    assert not any("cw_resolve" in p for p in proposed)


def test_tool_definitions_are_never_proposed():
    rows = [row(**{"gen_ai.tool.call.result": json.dumps(
        [{"type": "function", "name": "cw_resolve",
          "description": "Resolve A Fuzzy Name", "parameters": {}}])})]
    assert s.propose(rows) == {}


def test_iso_dates_are_not_phone_numbers():
    rows = [row(**{"gen_ai.tool.call.result":
                   json.dumps({"summary": "due 2026-07-06"})})]
    assert not any(v == "PHONE" for v in s.propose(rows).values())


def test_skill_file_headings_are_not_names():
    """The corpus is mostly our own skill files; their headings look exactly
    like names to any capitalisation heuristic."""
    skill = "---\nname: classification\n---\n# Priority Matrix\n## Status Rules"
    rows = [
        row(**{"gen_ai.tool.call.result": skill}),
        row(**{"gen_ai.tool.call.result":
               json.dumps({"summary": "Priority Matrix applies"})}),
    ]
    assert "Priority Matrix" not in s.propose(rows)


def test_a_real_name_survives_the_skill_filter():
    skill = "---\nname: classification\n---\n# Priority Matrix"
    rows = [
        row(**{"gen_ai.tool.call.result": skill}),
        row(**{"gen_ai.tool.call.result":
               json.dumps({"summary": "Eli Seale reported this"})}),
    ]
    assert "Eli Seale" in s.propose(rows)


def test_emails_are_proposed():
    rows = [row(**{"gen_ai.tool.call.result":
                   json.dumps({"text": "write to eli@verveit.com"})})]
    assert s.propose(rows).get("eli@verveit.com") == "EMAIL"


# --- applying ---------------------------------------------------------------

def test_comment_keys_are_not_treated_as_literals(pseudo):
    sweeper = s.build_sweeper({"Mettle": "COMPANY"}, pseudo)
    assert s.sweep("_comment stays", pseudo, sweeper) == "_comment stays"


def test_scrub_row_rewrites_the_property_bag(pseudo):
    sweeper = s.build_sweeper({"Mettle": "COMPANY"}, pseudo)
    out = s.scrub_row(row(**{"gen_ai.tool.call.result":
                             json.dumps({"company": "Mettle"})}),
                      pseudo, sweeper)
    assert "Mettle" not in out["customDimensions"]


def test_scrub_row_rewrites_appgenaicontent_columns(pseudo):
    sweeper = s.build_sweeper({"Mettle": "COMPANY"}, pseudo)
    out = s.scrub_row({"c_tool_result": json.dumps({"company": "Mettle"})},
                      pseudo, sweeper)
    assert "Mettle" not in out["c_tool_result"]
