#!/usr/bin/env python3
"""
fetch_outcomes.py — pull human review outcomes out of Dataverse.

Every check in run_evals.py is process quality: did the agent behave tidily.
None of them say whether it was RIGHT. A run can pass all eight having
proposed entirely the wrong company. §8 of docs/HANDOFF.md calls this the
single highest-value gap, and the ground truth for closing it already exists
in production:

    AI Orchestration   one workflow run against one entity (one ticket)
      └─ AI Run        one agent execution
           └─ AI Decision   one proposed field change
                └─ AI Review   human disposition + reason code (1:1, optional)

AI Review is the label. Its reason codes — Human Corrected Classification,
Source Data Incorrect, Business Rule Exception — are a failure taxonomy the
reviewers have been using for months. Operationalise that rather than
inventing new metrics.

Two modes:

  --probe    Discover. Lists the entity sets, dumps one sample row of each,
             and reports which attributes could serve as the join key. Writes
             what it learns to the schema file. Run this FIRST: the logical
             names below are informed guesses, not facts, and one probe
             replaces a week of them.

  (default)  Read the schema file, pull reviews since a watermark, and write
             one normalised JSONL row per decision.

    python3 dataverse/fetch_outcomes.py --probe
    python3 dataverse/fetch_outcomes.py --since 2026-09-01 -o artifacts/outcomes.jsonl

Credentials: DATAVERSE_URL, DATAVERSE_CLIENT_ID, DATAVERSE_CLIENT_SECRET,
DATAVERSE_TENANT_ID. See docs/CREDENTIALS.md — note that an app registration
alone grants nothing, the application user inside the environment is what
actually authorises.

The join problem, stated before anyone writes a join
----------------------------------------------------
A trace row is keyed by `orchestration_id`, the App Insights operation_Id.
A Dataverse AI Orchestration row is keyed by its own guid. Nothing obliges
them to know about each other.

Ticket id will NOT do it. In the committed frozen set, both orchestrations ran
against ticket 805392 — the same ticket triaged twice. Joining on ticket alone
maps one trace to two orchestrations and silently picks whichever came back
first, which is the kind of bug that produces confident wrong numbers rather
than an error.

So --probe reports, specifically, whether the orchestration entity carries a
correlation attribute holding the operation_Id. If it does, that is the join.
If it does not, the fix belongs in the agents repo — write it — and until then
outcome evaluation cannot be keyed reliably and this script says so rather
than guessing.
"""

from __future__ import annotations

# This script lives in a subdirectory but imports from the repo root, so put
# the root on sys.path before those imports.
import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import argparse, json, urllib.error, urllib.parse, urllib.request
import datetime as _dt

SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.json")
API = "/api/data/v9.2"


# ----------------------------------------------------------------- transport

def normalise_url(url):
    """Environment URL as Entra expects it in a scope.

    The scope must be the registered resource identifier, which is https and
    has no trailing slash. An http:// URL produces a scope that matches no
    resource and Entra rejects the token request with a 400 -- from the token
    endpoint, before Dataverse is ever contacted, which makes it look like a
    credential problem rather than a typo.
    """
    u = (url or "").strip().rstrip("/")
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    elif u and not u.startswith("https://"):
        u = "https://" + u
    return u


def token(url, client_id, client_secret, tenant_id, transport=None):
    """Client-credentials token for the Dataverse data plane.

    The scope is the environment URL, not a Graph scope. A Graph token against
    Dataverse returns 401 with nothing useful in the body, which is a long way
    to travel for a typo.
    """
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": f"{normalise_url(url)}/.default",
    }).encode()
    endpoint = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    raw = (transport or _post)(endpoint, body,
                               {"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(raw)["access_token"]


def _post(endpoint, body, headers):
    req = urllib.request.Request(endpoint, data=body, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # Entra says exactly what is wrong in the body. Letting the raw
        # HTTPError propagate throws that away and leaves a bare
        # "HTTP Error 400: Bad Request" traceback, which is unactionable.
        detail = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
            detail = parsed.get("error_description") or detail
        except json.JSONDecodeError:
            pass
        raise SystemExit(f"HTTP {exc.code} from the token endpoint\n\n"
                         f"{detail[:900]}")


def _get(endpoint, bearer):
    req = urllib.request.Request(endpoint, headers={
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json",
        "OData-Version": "4.0",
        "OData-MaxVersion": "4.0",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        if exc.code == 401:
            raise SystemExit(
                f"401 from {endpoint}\n{detail}\n\n"
                "401 here almost always means the app registration exists but "
                "was never added as an APPLICATION USER inside the Dataverse "
                "environment. Creating the registration grants nothing on its "
                "own. See docs/CREDENTIALS.md.")
        if exc.code == 403:
            raise SystemExit(
                f"403 from {endpoint}\n{detail}\n\n"
                "The application user exists but its security role does not "
                "cover this table. Read access on the four AI tables is "
                "enough; System Administrator is not required.")
        raise SystemExit(f"HTTP {exc.code} from {endpoint}\n{detail}")


def query(base, bearer, entity_set, select=None, filter=None, top=None,
          expand=None, getter=None):
    params = {}
    if select:
        params["$select"] = ",".join(select)
    if filter:
        params["$filter"] = filter
    if expand:
        params["$expand"] = expand
    if top:
        params["$top"] = str(top)
    url = f"{base.rstrip('/')}{API}/{entity_set}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return (getter or _get)(url, bearer).get("value", [])


# --------------------------------------------------------------------- probe

# Informed guesses, not facts. Dataverse prefixes custom entities with the
# publisher's customisation prefix, which this repo has no way to know.
# --probe replaces every one of these with what is actually there.
GUESSES = {
    "orchestration": ["ai_aiorchestrations", "aiorchestrations",
                      "verve_aiorchestrations"],
    "run":           ["ai_airuns", "airuns", "verve_airuns"],
    "decision":      ["ai_aidecisions", "aidecisions", "verve_aidecisions"],
    "review":        ["ai_aireviews", "aireviews", "verve_aireviews"],
}

# Attributes that could carry the App Insights operation_Id. A 32-character
# hex string is the signature.
JOIN_HINTS = ("operation", "correlation", "trace", "activity", "conversation",
              "runid", "run_id", "externalid")


def entity_sets(base, bearer, getter=None):
    """Every entity set in the environment, from the service document."""
    doc = (getter or _get)(f"{base.rstrip('/')}{API}/", bearer)
    return sorted(e.get("name", "") for e in doc.get("value", []))


def looks_like_operation_id(value):
    v = str(value or "")
    return len(v) == 32 and all(c in "0123456789abcdefABCDEF" for c in v)


def probe(base, bearer, getter=None):
    """Discover the real entity sets and report join candidates."""
    available = entity_sets(base, bearer, getter=getter)
    found, report = {}, {"entity_sets_matched": {}, "join_candidates": {},
                         "samples": {}}

    for role, candidates in GUESSES.items():
        hit = next((c for c in candidates if c in available), None)
        if hit is None:
            # Fall back to substring matching, which survives an unknown prefix.
            needle = role.replace("orchestration", "orchestr")[:8]
            hit = next((e for e in available if needle in e.lower()), None)
        found[role] = hit
        report["entity_sets_matched"][role] = hit

    for role, entity_set in found.items():
        if not entity_set:
            continue
        rows = query(base, bearer, entity_set, top=1, getter=getter)
        if not rows:
            report["samples"][entity_set] = "no rows"
            continue
        row = rows[0]
        report["samples"][entity_set] = sorted(
            k for k in row if not k.startswith("@"))
        if role == "orchestration":
            report["join_candidates"][entity_set] = sorted(
                k for k, v in row.items()
                if not k.startswith("@")
                and (looks_like_operation_id(v)
                     or any(h in k.lower() for h in JOIN_HINTS)))
    return found, report


# --------------------------------------------------------------------- fetch

def load_schema(path=SCHEMA):
    if not os.path.exists(path):
        raise SystemExit(
            f"{path} does not exist. Run --probe first: the entity logical "
            "names depend on the publisher prefix of your environment and "
            "cannot be guessed from here.")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def normalise(review, schema):
    """One review row -> the flat shape the outcome checks will read."""
    f = schema["fields"]
    return {
        "review_id": review.get(f["review_id"]),
        "decision_id": review.get(f["decision_id"]),
        "orchestration_key": review.get(f["orchestration_key"]),
        "field": review.get(f["field"]),
        "ai_value": review.get(f["ai_value"]),
        "human_value": review.get(f["human_value"]),
        "disposition": review.get(f["disposition"]),
        "reason_code": review.get(f["reason_code"]),
        "reviewed_utc": review.get(f["reviewed_utc"]),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true",
                    help="discover entity sets and join keys, then exit")
    ap.add_argument("--since", help="ISO date; only reviews at or after it")
    ap.add_argument("-o", "--out", default="artifacts/outcomes.jsonl")
    ap.add_argument("--schema", default=SCHEMA)
    ap.add_argument("--url", default=os.environ.get("DATAVERSE_URL"))
    ap.add_argument("--client-id", default=os.environ.get("DATAVERSE_CLIENT_ID"))
    ap.add_argument("--client-secret",
                    default=os.environ.get("DATAVERSE_CLIENT_SECRET"))
    ap.add_argument("--tenant-id", default=os.environ.get("DATAVERSE_TENANT_ID"))
    args = ap.parse_args(argv)

    missing = [n for n, v in (("--url", args.url),
                              ("--client-id", args.client_id),
                              ("--client-secret", args.client_secret),
                              ("--tenant-id", args.tenant_id)) if not v]
    if missing:
        sys.exit("missing: " + ", ".join(missing) +
                 "\nSet DATAVERSE_URL / _CLIENT_ID / _CLIENT_SECRET / "
                 "_TENANT_ID, or pass the flags. See docs/CREDENTIALS.md.")

    if args.url != normalise_url(args.url):
        print(f"note: using {normalise_url(args.url)} "
              f"(was {args.url!r})", file=sys.stderr)
    args.url = normalise_url(args.url)

    bearer = token(args.url, args.client_id, args.client_secret, args.tenant_id)

    if args.probe:
        found, report = probe(args.url, bearer)
        print(json.dumps(report, indent=1))
        print()
        missing_sets = [r for r, v in found.items() if not v]
        if missing_sets:
            print("NOT FOUND: " + ", ".join(missing_sets) +
                  "\nThe guesses in GUESSES are wrong for this environment. "
                  "Pick the right names out of the entity set list above and "
                  "write them into the schema file by hand.")
        orch = found.get("orchestration")
        candidates = report["join_candidates"].get(orch) or []
        if candidates:
            print(f"\nJOIN CANDIDATES on {orch}: {', '.join(candidates)}")
            print("Pick the one holding the App Insights operation_Id and set "
                  "it as fields.orchestration_key in the schema file.")
        else:
            print(f"\nNO JOIN CANDIDATE on {orch or '<not found>'}.")
            print("Nothing on the orchestration row looks like an App "
                  "Insights operation_Id. Ticket id will not substitute -- in "
                  "the committed frozen set two orchestrations share ticket "
                  "805392, so that join is one-to-many and picks a winner "
                  "silently. The fix belongs in the agents repo: write the "
                  "operation_Id onto the orchestration record. Until then "
                  "outcome evaluation cannot be keyed reliably.")
        return 0

    schema = load_schema(args.schema)
    f = schema["fields"]
    flt = None
    if args.since:
        flt = f"{f['reviewed_utc']} ge {args.since}T00:00:00Z"
    rows = query(args.url, bearer, schema["entity_sets"]["review"], filter=flt)
    out = [normalise(r, schema) for r in rows]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".",
                exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for row in out:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    keyed = sum(1 for r in out if r["orchestration_key"])
    print(f"{len(out)} review(s) -> {args.out}")
    print(f"  with a join key : {keyed}/{len(out)}")
    if out and not keyed:
        print("  NONE carry an orchestration key, so none can be joined to a "
              "trace. See the note under --probe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
