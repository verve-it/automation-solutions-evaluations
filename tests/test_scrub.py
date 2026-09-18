"""Scrubbing a trace for commit.

Two failure modes matter and both are silent: leaving customer data in
(the whole point), and redacting ConnectWise vocabulary the checks score,
which rewrites a trajectory while every file still looks fine.
"""
import json
import os

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
    out = s.sweep("contact tech@example.com or 555-0100-999 today", pseudo, None)
    assert "tech@example.com" not in out and "EMAIL_" in out
    assert "555-0100-999" not in out and "PHONE_" in out


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
                   json.dumps({"text": "write to tech@example.com"})})]
    assert s.propose(rows).get("tech@example.com") == "EMAIL"


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


# --- protected vocabulary ---------------------------------------------------

def test_intent_names_are_refused():
    """Seen live: 'Full Triage' was swept, every hand-off became
    `intent=NAME?_e22a669c`, and trajectory coverage dropped 7/7 -> 3/7 with
    no error anywhere."""
    protected = s.protected_vocabulary()
    for intent in ("Full Triage", "Write Request", "Information Request"):
        assert intent.lower() in protected


def test_agent_names_are_refused():
    """A redacted agent name stops matching AGENT_NAMES, so the child run
    collapses into its caller's trajectory."""
    protected = s.protected_vocabulary()
    for agent in ("triage-orchestrator", "connectwise-operations-agent"):
        assert agent in protected


def test_a_real_identity_is_not_protected():
    assert "eli seale" not in s.protected_vocabulary()


def test_the_check_is_case_insensitive():
    assert "full triage" in s.protected_vocabulary()


# --- word boundaries --------------------------------------------------------

def test_a_short_literal_does_not_eat_longer_words(pseudo):
    """Seen live: 'Process' in the list turned 'Processing the request' into
    'PERSON_36dc5581ing the request' across the whole corpus."""
    sweeper = s.build_sweeper({"Process": "PERSON"}, pseudo)
    out = s.sweep("Processing the request, processed already", pseudo, sweeper)
    assert out == "Processing the request, processed already"


def test_the_literal_itself_is_still_swept(pseudo):
    sweeper = s.build_sweeper({"Process": "PERSON"}, pseudo)
    assert "PERSON_" in s.sweep("the Process owner", pseudo, sweeper)


def test_a_multiword_name_is_swept_inside_prose(pseudo):
    sweeper = s.build_sweeper({"Eli Seale": "PERSON"}, pseudo)
    out = s.sweep("## Request\nEli Seale's laptop", pseudo, sweeper)
    assert "Eli Seale" not in out and "PERSON_" in out


def test_emails_keep_working_with_boundaries(pseudo):
    sweeper = s.build_sweeper({"tech@example.com": "EMAIL"}, pseudo)
    out = s.sweep("write to tech@example.com now", pseudo, sweeper)
    assert "tech@example.com" not in out


def test_a_literal_ending_in_punctuation_still_matches(pseudo):
    """`\\b` next to a non-word character never matches, so it must not be
    added there."""
    sweeper = s.build_sweeper({"(209) 244-7120": "PHONE"}, pseudo)
    out = s.sweep("call (209) 244-7120 today", pseudo, sweeper)
    assert "244-7120" not in out


def test_residual_check_uses_the_same_boundaries():
    """Otherwise 'Process' reads as residual inside 'Processing', which the
    sweep deliberately left alone, and the scrub reports a failure it did
    not have."""
    assert s.still_present("Process", "the Process owner") is True
    assert s.still_present("Process", "Processing only") is False


# --- the non-JSON fallback --------------------------------------------------

def _nonjson(text, attr="gen_ai.tool.call.result"):
    """A payload that fails json.loads — a prose tool result, say."""
    return {"customDimensions": json.dumps({attr: text})}


def test_a_non_json_payload_is_scanned_for_names_and_phones():
    """The fallback used to run EMAIL only.

    40% of the payloads in the committed traces fail json.loads -- tool
    results and input messages among them, which is exactly where customer
    data is. A name or phone there was never proposed, so never reviewed,
    never redacted, and never reported as residual by --verify. The scrub
    passed and the data shipped.
    """
    found = s.propose([_nonjson(
        "Contacted Jeff Gilbert at 555-0100-999 "
        "(jeff.gilbert@example.com) about the laptop. Not valid JSON {")])
    assert "jeff.gilbert@example.com" in found
    # CAPPHRASE takes the whole capitalised run, sentence-initial word
    # included -- the reviewer sees the name either way.
    assert any("Jeff Gilbert" in v for v in found), sorted(found)
    assert any(k == "PHONE" for k in found.values()), sorted(found.items())


def test_the_fallback_still_respects_the_skill_filter():
    """Our own documentation is not a person, JSON or not."""
    found = s.propose([_nonjson("## Escalation Policy\nAsk the Triage Team.",
                                attr="gen_ai.system_instructions")])
    assert not [v for v, k in found.items() if k == "NAME?"], sorted(found)


def test_json_and_non_json_payloads_propose_the_same_literals():
    """The two paths ran different heuristics, which is how this happened.

    Same text, once as a JSON string leaf and once as raw prose, must yield
    the same candidates.
    """
    text = "Call Connie Revay on 209-478-8864 or connie@example.com"
    as_json = s.propose([_nonjson(json.dumps([text]))])   # parses
    as_prose = s.propose([_nonjson(text + " {")])         # does not
    assert as_json, "the JSON path proposed nothing -- test is wrong"
    assert set(as_json) == set(as_prose), (sorted(as_json), sorted(as_prose))


def test_learn_withholds_protected_vocabulary(tmp_path, monkeypatch, capsys):
    """Proposing a term that can only be rejected at apply time wastes the
    review attention that decides whether a real name gets caught.

    Uses a sensitive key rather than the capitalised-phrase heuristic: the
    stopword list already drops most vocabulary, so the terms that reach the
    proposal are the ones arriving by some other route. Those are the ones
    worth withholding.
    """
    term = sorted(s.protected_vocabulary())[0]
    key = sorted(s.SENSITIVE_KEYS - s.NEVER_PROPOSE)[0]
    payload = {"gen_ai.tool.call.result":
               json.dumps({key: term.title(), "note": "Contact Grant Johnson"})}
    trace = tmp_path / "t.json"
    trace.write_text(json.dumps([{"customDimensions": json.dumps(payload)}]))

    assert term.title() in s.propose(json.loads(trace.read_text())), \
        "the term is not proposed at all -- the test proves nothing"

    out = tmp_path / "candidates.json"
    monkeypatch.setattr("sys.argv",
                        ["scrub_trace.py", str(trace), "--learn", str(out)])
    assert s.main() == 0
    proposed = list(json.loads(out.read_text()))
    assert term not in {k.strip().lower() for k in proposed}, proposed
    assert "withheld" in capsys.readouterr().out
    # and the real name beside it is still proposed
    assert any("Grant Johnson" in k for k in proposed), proposed


def test_a_candidate_list_can_never_be_committed(tmp_path):
    """A --learn output is a catalogue of exactly the customer data being
    removed. .gitignore covered redact*/reviewed*, but not the name
    traces/README.md actually tells you to use."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("candidates.json", "scrub-candidates-full-triage.json",
                 "ops-candidates.json", "reviewed.json", "redact.json"):
        out = subprocess.run(["git", "check-ignore", "-q", name],
                             cwd=repo, capture_output=True)
        assert out.returncode == 0, f"{name} is NOT gitignored"


# --- salt discipline --------------------------------------------------------

def test_the_fingerprint_identifies_the_salt_without_revealing_it():
    a, b = s.salt_fingerprint("a" * 32), s.salt_fingerprint("a" * 32)
    c = s.salt_fingerprint("b" * 32)
    assert a == b and a != c
    assert len(a) == 12 and all(ch in "0123456789abcdef" for ch in a)
    assert "a" * 32 not in a


def test_the_fingerprint_does_not_leak_a_token():
    """It must not be derivable from, or usable to derive, any pseudonym."""
    salt = "s" * 32
    p = s.Pseudonymiser(salt)
    tok = p.token("Jeff Gilbert", "PERSON")
    assert s.salt_fingerprint(salt) not in tok
    assert tok.split("_")[1] not in s.salt_fingerprint(salt)


def test_two_traces_scrubbed_with_one_salt_agree_on_a_person():
    """The property the fingerprint exists to make checkable."""
    a = s.Pseudonymiser("x" * 32).token("Jeff Gilbert", "PERSON")
    b = s.Pseudonymiser("x" * 32).token("Jeff Gilbert", "PERSON")
    assert a == b
    assert s.Pseudonymiser("y" * 32).token("Jeff Gilbert", "PERSON") != a


def test_a_short_salt_is_refused(tmp_path, monkeypatch, capsys):
    """A short salt is brute-forceable against a known name list, which is
    the attack the pseudonyms exist to stop."""
    trace = tmp_path / "t.json"
    trace.write_text(json.dumps([_nonjson("hello")]))
    redact = tmp_path / "r.json"
    redact.write_text(json.dumps({"hello": "VALUE"}))
    out = tmp_path / "o.json"
    monkeypatch.setattr("sys.argv", ["scrub_trace.py", str(trace),
                                     "--redact-file", str(redact),
                                     "--salt", "short", "-o", str(out)])
    with pytest.raises(SystemExit) as exc:
        s.main()
    assert "at least" in str(exc.value)
    assert not out.exists(), "a refused scrub must not write anything"


def test_a_scrub_writes_a_sidecar_naming_its_salt(tmp_path, monkeypatch):
    trace = tmp_path / "t.json"
    trace.write_text(json.dumps([_nonjson("contact Jeff Gilbert")]))
    redact = tmp_path / "r.json"
    redact.write_text(json.dumps({"Jeff Gilbert": "PERSON"}))
    out = tmp_path / "o.json"
    salt = "z" * 32
    monkeypatch.setattr("sys.argv", ["scrub_trace.py", str(trace),
                                     "--redact-file", str(redact),
                                     "--salt", salt, "-o", str(out)])
    assert s.main() == 0
    side = json.loads((tmp_path / "o.json.scrub.json").read_text())
    assert side["salt_fingerprint"] == s.salt_fingerprint(salt)
    assert side["literals"] == 1
    assert salt not in json.dumps(side), "the sidecar must never carry the salt"
