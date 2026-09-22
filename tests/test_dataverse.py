"""The Dataverse loader, without Dataverse.

Every HTTP call goes through an injected getter, so the discovery logic, the
join-key reasoning and the error messages are all testable offline. None of
this needs credentials.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dataverse"))
import fetch_outcomes as fo                              # noqa: E402


SERVICE_DOC = {"value": [{"name": n} for n in (
    "accounts", "contacts",
    "verve_aiorchestrations", "verve_airuns",
    "verve_aidecisions", "verve_aireviews")]}


def getter(rows_by_set, service=SERVICE_DOC):
    """Fake _get: serves the service document and one page per entity set."""
    def _g(url, bearer):
        path = url.split("/api/data/v9.2/", 1)[1].split("?", 1)[0]
        if path == "":
            return service
        return {"value": rows_by_set.get(path, [])}
    return _g


# --- discovery --------------------------------------------------------------

def test_probe_finds_entity_sets_behind_an_unknown_publisher_prefix():
    """GUESSES cannot know the customisation prefix. Substring matching is
    what makes one probe enough instead of a guessing loop."""
    found, _ = fo.probe("https://x.crm.dynamics.com", "t",
                        getter=getter({}))
    assert found["orchestration"] == "verve_aiorchestrations"
    assert found["run"] == "verve_airuns"
    assert found["decision"] == "verve_aidecisions"
    assert found["review"] == "verve_aireviews"


def test_probe_reports_a_missing_entity_rather_than_inventing_one():
    thin = {"value": [{"name": "accounts"}]}
    found, _ = fo.probe("https://x", "t", getter=getter({}, service=thin))
    assert found["orchestration"] is None


# --- the join key -----------------------------------------------------------

def test_an_operation_id_is_recognised_by_shape():
    assert fo.looks_like_operation_id("bed408b416e8bb61d56f800212b90459")
    assert not fo.looks_like_operation_id("805392")
    assert not fo.looks_like_operation_id("")
    assert not fo.looks_like_operation_id("bed408b416e8bb61d56f800212b9045")


def test_probe_surfaces_a_correlation_attribute_as_a_join_candidate():
    rows = {"verve_aiorchestrations": [{
        "verve_aiorchestrationid": "11111111-2222-3333-4444-555555555555",
        "verve_operationid": "bed408b416e8bb61d56f800212b90459",
        "verve_ticketnumber": "805392",
    }]}
    _, report = fo.probe("https://x", "t", getter=getter(rows))
    cands = report["join_candidates"]["verve_aiorchestrations"]
    assert "verve_operationid" in cands
    assert "verve_ticketnumber" not in cands, \
        "ticket number is not a join key -- two orchestrations share 805392"


def test_a_guid_shaped_value_is_not_mistaken_for_an_operation_id():
    """A Dataverse primary key is a hyphenated guid, 36 chars. The App
    Insights operation_Id is 32 hex with no hyphens."""
    rows = {"verve_aiorchestrations": [{
        "verve_aiorchestrationid": "11111111-2222-3333-4444-555555555555",
        "verve_ticketnumber": "805392",
    }]}
    _, report = fo.probe("https://x", "t", getter=getter(rows))
    assert report["join_candidates"]["verve_aiorchestrations"] == []


# --- normalisation ----------------------------------------------------------

def test_a_review_row_normalises_to_the_flat_outcome_shape():
    schema = json.load(open(os.path.join(fo.REPO_ROOT, "dataverse",
                                         "schema.json"), encoding="utf-8"))
    f = schema["fields"]
    raw = {f["review_id"]: "r1", f["decision_id"]: "d1",
           f["orchestration_key"]: "bed408b416e8bb61d56f800212b90459",
           f["field"]: "company", f["ai_value"]: "Acme",
           f["human_value"]: "Acme Corp", f["disposition"]: "modified",
           f["reason_code"]: "Human Corrected Classification",
           f["reviewed_utc"]: "2026-09-04T09:00:00Z"}
    out = fo.normalise(raw, schema)
    assert out["disposition"] == "modified"
    assert out["orchestration_key"] == "bed408b416e8bb61d56f800212b90459"
    assert out["ai_value"] != out["human_value"]


def test_a_missing_attribute_normalises_to_none_rather_than_raising():
    """Dataverse omits null attributes from OData responses entirely."""
    schema = json.load(open(os.path.join(fo.REPO_ROOT, "dataverse",
                                         "schema.json"), encoding="utf-8"))
    out = fo.normalise({}, schema)
    assert set(out) == {"review_id", "decision_id", "orchestration_key",
                        "field", "ai_value", "human_value", "disposition",
                        "reason_code", "reviewed_utc"}
    assert all(v is None for v in out.values())


# --- guard rails ------------------------------------------------------------

def test_the_schema_template_is_marked_as_unprobed():
    """Scoring against guessed logical names would produce empty results that
    look like 'no reviews yet' rather than a misconfiguration."""
    with open(os.path.join(fo.REPO_ROOT, "dataverse", "schema.json"),
              encoding="utf-8") as fh:
        schema = json.load(fh)
    assert schema["_probed"] is None, \
        "once probed, set _probed to the date and delete this assertion"
    assert "PLACEHOLDER" in schema["_README"]


def test_missing_credentials_name_all_of_them_at_once(monkeypatch, capsys):
    for var in ("DATAVERSE_URL", "DATAVERSE_CLIENT_ID",
                "DATAVERSE_CLIENT_SECRET", "DATAVERSE_TENANT_ID"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit) as exc:
        fo.main([])
    msg = str(exc.value)
    for flag in ("--url", "--client-id", "--client-secret", "--tenant-id"):
        assert flag in msg, msg


def test_load_schema_explains_why_it_cannot_be_guessed(tmp_path):
    with pytest.raises(SystemExit) as exc:
        fo.load_schema(str(tmp_path / "nope.json"))
    assert "--probe" in str(exc.value)
    assert "publisher prefix" in str(exc.value)


# --- the environment URL ----------------------------------------------------

@pytest.mark.parametrize("given,expected", [
    ("http://org70929f62.crm.dynamics.com",
     "https://org70929f62.crm.dynamics.com"),
    ("https://x.crm.dynamics.com/", "https://x.crm.dynamics.com"),
    ("org.crm.dynamics.com", "https://org.crm.dynamics.com"),
    ("https://x.crm4.dynamics.com", "https://x.crm4.dynamics.com"),
    ("  https://x.crm.dynamics.com  ", "https://x.crm.dynamics.com"),
])
def test_the_environment_url_is_normalised(given, expected):
    """The scope must be the registered resource identifier: https, no
    trailing slash. An http:// URL yields a scope matching no resource and
    Entra rejects it with a 400 from the TOKEN endpoint -- before Dataverse
    is contacted at all, so it reads as a credential problem rather than a
    typo in the host."""
    assert fo.normalise_url(given) == expected


def test_the_scope_is_built_from_the_normalised_url():
    seen = {}

    def transport(endpoint, body, headers):
        seen["body"] = body.decode()
        return json.dumps({"access_token": "t"})

    fo.token("http://org.crm.dynamics.com/", "c", "s", "tid",
             transport=transport)
    assert "https%3A%2F%2Forg.crm.dynamics.com%2F.default" in seen["body"]
    assert "http%3A%2F%2F" not in seen["body"]


def test_a_token_error_surfaces_entras_description_not_a_traceback(monkeypatch):
    """A bare `HTTP Error 400: Bad Request` traceback is unactionable. Entra
    puts the actual cause in error_description, and _post must show it.

    Exercises _post itself rather than an injected transport, because the
    guard lives there -- injecting a transport bypasses exactly the code
    under test.
    """
    import io
    import urllib.error
    import urllib.request

    def raise_400(req, *a, **kw):
        raise urllib.error.HTTPError(
            "https://login.microsoftonline.com/t/oauth2/v2.0/token",
            400, "Bad Request", {},
            io.BytesIO(json.dumps({
                "error": "invalid_scope",
                "error_description": "AADSTS500011: The resource principal "
                                     "named http://org.crm.dynamics.com was "
                                     "not found in the tenant",
            }).encode()))

    monkeypatch.setattr(urllib.request, "urlopen", raise_400)
    with pytest.raises(SystemExit) as exc:
        fo._post("https://login.microsoftonline.com/t/oauth2/v2.0/token",
                 b"", {})
    msg = str(exc.value)
    assert "AADSTS500011" in msg, msg
    assert "resource principal" in msg, msg
    assert "token endpoint" in msg, msg


def test_a_non_json_token_error_still_shows_its_body(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    def raise_500(req, *a, **kw):
        raise urllib.error.HTTPError("https://x", 500, "Server Error", {},
                                     io.BytesIO(b"<html>upstream died</html>"))

    monkeypatch.setattr(urllib.request, "urlopen", raise_500)
    with pytest.raises(SystemExit) as exc:
        fo._post("https://x", b"", {})
    assert "upstream died" in str(exc.value)
