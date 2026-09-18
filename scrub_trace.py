#!/usr/bin/env python3
"""
scrub_trace.py — redact customer data from a trace export so a frozen set can
be committed.

`protectGenAISensitiveData` routes tool content into AppGenAIContent and
restricts it to Privileged Monitoring Data Reader. Committing a raw export to
git replaces that control with "has repo access", permanently, in history.

Two steps, on purpose. Scrubbing a corpus for commit is a review, not an
automation — an automatic sweep both over-redacts (it will happily learn
"Priority 4" and "AI Triage Complete", which the checks score) and
under-redacts (a contact name inside an audit note is not a structured field).

    1. propose                  python3 scrub_trace.py TRACE --learn redact.json
    2. review redact.json by hand — delete anything that is vocabulary
    3. apply                    python3 scrub_trace.py TRACE --redact-file redact.json -o OUT --verify

`--verify` scores the input and the output and fails if a single check verdict
differs. A scrub that changes an eval result is a corrupted fixture, not a
redaction. The residual check then proves every declared literal is gone.

Identity is preserved by pseudonym, not destroyed: one real value always maps
to one token, so a cascade of retries against a single target still reads as a
single target. Tokens are salted; keep the salt out of the repo.
"""

from __future__ import annotations
import argparse, csv, hashlib, json, os, re, sys
import datetime as _dt

# 16 hex chars of entropy is the floor; `secrets.token_hex(16)` gives 32.
MIN_SALT_LEN = 16

# Structured ConnectWise fields that hold customer data. Used only to PROPOSE
# candidates during --learn; nothing is redacted without a reviewed list.
SENSITIVE_KEYS = {
    "contact", "contactname", "contactemailaddress", "contactemail",
    "contactphone", "firstname", "lastname", "nickname", "fullname",
    "name", "email", "emailaddress", "phone", "phonenumber", "mobilephone",
    "company", "companyname", "site", "sitename",
    "addressline1", "addressline2", "city", "state", "zip", "country",
    "summary", "description", "initialdescription", "text", "notes", "note",
    "auditnote", "title", "query",
}

# Never proposed: the vocabulary the checks and trajectories are made of.
NEVER_PROPOSE = {
    "reference_type", "entity", "field", "op", "path", "fields", "page",
    "page_size", "ticket_number", "id", "skill_name", "script_name",
    "count", "count_hint", "operations", "conditions", "type", "subtype",
    "item", "board", "status", "priority", "urgency", "impact",
}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# 10+ digits, and not an ISO date — `2026-07-06` is not a phone number.
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]?){9,}\d(?!\d)")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
# Foundry tool identifiers: `<server>___<tool>`, or a bare cw_* / snake_case
# tool name. Never customer data.
TOOL_NAME_RE = re.compile(r"^[\w.-]*___[\w.-]+$|^(cw_|load_skill|run_skill|tool_)")

# Person and company names live in prose, not in structured fields — this
# corpus has exactly two `name` values and 67 e-mail addresses, but contact
# and company names appear throughout audit notes and write plans. A
# capitalised multi-word phrase is the usual shape; the reviewer decides.
CAPPHRASE_RE = re.compile(
    r"\b[A-Z][a-z'`-]+(?:\.?\s+[A-Z][A-Za-z'`.-]+){1,3}\b")

# Capitalised phrases that are ConnectWise or product vocabulary, never a
# person. Keeps the review list short enough to actually read.
PHRASE_STOPWORDS = {
    "ai", "triage", "complete", "connectwise", "psa", "service", "ticket",
    "board", "status", "priority", "type", "subtype", "item", "impact",
    "urgency", "company", "contact", "site", "agreement", "configuration",
    "request", "issue", "process", "problem", "management", "incident",
    "new", "closed", "resolved", "open", "internal", "external", "automation",
    "test", "summary", "description", "details", "requested", "outcome",
    "reported", "note", "notes", "laptop", "email", "phone", "user",
    "microsoft", "windows", "office", "azure", "foundry", "the", "this",
    "response", "result", "error", "warning", "unknown", "none", "not",
}

PAYLOAD_ATTRS = ("gen_ai.tool.call.arguments", "gen_ai.tool.call.result",
                 "gen_ai.input.messages", "gen_ai.output.messages",
                 "gen_ai.system_instructions")
CONTENT_COLUMNS = ("c_input", "c_output", "c_system", "c_tool_args",
                   "c_tool_result", "c_tool_defs")

MIN_LEN = 4          # shorter literals are ordinary English
MAX_LEN = 120        # longer ones are prose, not an identity


def protected_vocabulary():
    """Literals a scrub must never touch, whatever a reviewed list says.

    Redacting one of these does not look like a mistake anywhere: the file is
    written, the residual check passes, and the corpus quietly stops matching
    `expected.json`. Seen live — "Full Triage" was swept, every hand-off
    turned into `intent=NAME?_e22a669c`, and trajectory coverage dropped from
    7/7 to 3/7 with no error.

    Agent names are here for the same reason: they are the A2A run boundaries
    in AGENT_NAMES, and a redacted one collapses a child run into its caller.
    """
    from trace_to_eval import AGENT_NAMES, SUPPORTED_INTENTS
    return {v.lower() for v in
            set(SUPPORTED_INTENTS) | set(AGENT_NAMES) | {
                # ConnectWise vocabulary the checks and trajectories score.
                "Full Triage", "Write Request", "Information Request",
                "AI Triage Complete", "Priority 1", "Priority 2",
                "Priority 3", "Priority 4", "Incident", "Problem",
                "Service Request", "New", "Closed", "Resolved",
            }}


def salt_fingerprint(salt):
    """A non-reversible id for a salt, safe to commit.

    Two traces scrubbed with the same salt give the same person the same
    token; with different salts they do not, and nothing in the files says
    so. This makes that visible without storing the secret: it is a hash of
    a fixed constant under the salt, so it identifies the salt without
    helping anyone recover it or any token.
    """
    return hashlib.sha256(b"scrub-salt-fingerprint-v1|"
                          + salt.encode()).hexdigest()[:12]


class Pseudonymiser:
    def __init__(self, salt):
        self.salt = salt.encode()
        self.fingerprint = salt_fingerprint(salt)
        self.seen = {}

    def token(self, value, kind="VALUE"):
        key = str(value).lower()
        if key not in self.seen:
            digest = hashlib.sha256(self.salt + key.encode()).hexdigest()[:8]
            self.seen[key] = f"{kind.upper()}_{digest}"
        return self.seen[key]


def _kind_for(key):
    k = (key or "").lower()
    for needle, kind in (("email", "EMAIL"), ("phone", "PHONE"),
                         ("company", "COMPANY"), ("site", "PLACE"),
                         ("address", "PLACE"), ("contact", "PERSON"),
                         ("name", "PERSON")):
        if needle in k:
            return kind
    return "VALUE"


# ------------------------------------------------------------------ loading

def load_rows(path):
    with open(path, encoding="utf-8-sig") as fh:
        head = fh.read(1)
        fh.seek(0)
        if head in "[{":
            rows = json.load(fh)
            if isinstance(rows, dict):
                rows = rows.get("value") or rows.get("rows") or [rows]
            return rows
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def payloads_of(row):
    dims = (row.get("customDimensions") or row.get("CustomDimensions")
            or row.get("Properties"))
    if isinstance(dims, str):
        try:
            dims = json.loads(dims)
        except json.JSONDecodeError:
            dims = None
    out = []
    if isinstance(dims, dict):
        out += [(a, dims.get(a)) for a in PAYLOAD_ATTRS]
    out += [(c, row.get(c)) for c in CONTENT_COLUMNS]
    return [(a, v) for a, v in out if v]


# -------------------------------------------------------------------- learn

def skill_corpus(rows):
    """Every load_skill body, concatenated.

    The traces are mostly our own skill files — they arrive as load_skill
    results and are pasted into system instructions and prompts. Their
    headings look exactly like names to any capitalisation heuristic
    ("Classification Rules", "Priority Matrix"). Anything that appears in a
    skill file is documentation we wrote, so it is not customer data.
    """
    parts = []
    for row in rows:
        for attr, raw in payloads_of(row):
            if "tool.call.result" not in attr and attr != "c_tool_result":
                continue
            if isinstance(raw, str) and raw.lstrip().startswith("---\nname:"):
                parts.append(raw)
    return "\n".join(parts).lower()


def propose(rows):
    """Candidate literals for review. Deliberately generous — a human deletes
    the vocabulary, which is safer than a heuristic keeping it."""
    found = {}
    skills = skill_corpus(rows)

    def note(value, kind):
        v = (value or "").strip()
        if not (MIN_LEN <= len(v) <= MAX_LEN):
            return
        if kind == "PHONE" and (ISO_DATE_RE.match(v)
                                or sum(c.isdigit() for c in v) < 10):
            return
        if TOOL_NAME_RE.search(v):
            return
        # In a skill file -> our documentation, not a person or a customer.
        if kind == "NAME?" and v.lower() in skills:
            return
        found.setdefault(v, kind)

    def scan_text(text, parent_key=None, names=True):
        """Every scalar heuristic, over one string.

        Shared by walk()'s str leaves and the non-JSON fallback below. They
        used to differ: the fallback ran EMAIL only, so a name or phone in a
        payload that failed json.loads was never proposed and so never
        reviewed, never redacted, and never reported as residual.
        """
        key = (parent_key or "").lower()
        if key in SENSITIVE_KEYS and key not in NEVER_PROPOSE:
            note(text, _kind_for(key))
        for m in EMAIL_RE.finditer(text):
            note(m.group(0), "EMAIL")
        for m in PHONE_RE.finditer(text):
            note(m.group(0), "PHONE")
        for m in (CAPPHRASE_RE.finditer(text) if names else ()):
            phrase = m.group(0)
            words = [w.strip(".'`-").lower() for w in phrase.split()]
            # Every word vocabulary -> not a name. Any word not in the
            # stoplist -> propose it and let the reviewer decide.
            if any(w and w not in PHRASE_STOPWORDS for w in words):
                note(phrase, "NAME?")

    def walk(obj, parent_key=None, in_schema=False, names=True):
        if isinstance(obj, dict):
            # A tool definition or a call_tool envelope is schema, end to end.
            schema = in_schema or (
                "name" in obj and ("arguments" in obj or "parameters" in obj
                                   or "inputSchema" in obj
                                   or obj.get("type") == "function"))
            for k, v in obj.items():
                walk(v, k, schema and k != "arguments", names)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, parent_key, in_schema, names)
        elif isinstance(obj, str):
            if in_schema:
                return
            scan_text(obj, parent_key, names)

    for row in rows:
        for attr, raw in payloads_of(row):
            # Skill files and tool schemas are our own documentation; their
            # capitalised headings are not people.
            names = not ("system_instructions" in attr or "c_system" == attr
                         or "tool.definitions" in attr or "c_tool_defs" == attr)
            try:
                walk(json.loads(raw), names=names)
            except (json.JSONDecodeError, TypeError):
                # Not JSON — a prose tool result, a truncated blob, a plain
                # message. 40% of the payloads in the committed traces land
                # here, tool results and input messages among them, which is
                # exactly where customer data is. Scan the raw text with the
                # same heuristics rather than only for e-mails.
                scan_text(raw, names=names)
    return found


# -------------------------------------------------------------------- apply

def bounded(literal):
    r"""`re.escape` plus word boundaries where they apply.

    Without them a short literal eats longer words containing it: "Process"
    in the list turns "Processing the request" into "PERSON_36dc5581ing the
    request". Boundaries are only added where the literal starts or ends with
    a word character — an e-mail address ends in a letter but a phone number
    may end in punctuation, and `\b` next to a non-word character would never
    match.
    """
    escaped = re.escape(literal)
    prefix = r"\b" if literal[:1].isalnum() or literal[:1] == "_" else ""
    suffix = r"\b" if literal[-1:].isalnum() or literal[-1:] == "_" else ""
    return f"{prefix}{escaped}{suffix}"


def build_sweeper(redactions, pseudo):
    """One case-insensitive pattern over every reviewed literal, longest
    first so a longer name is replaced before a substring of it."""
    if not redactions:
        return None
    ordered = sorted(redactions, key=len, reverse=True)
    pattern = re.compile("|".join(bounded(v) for v in ordered), re.I)
    tokens = {v.lower(): pseudo.token(v, redactions[v]) for v in ordered}
    return pattern, tokens


def still_present(literal, haystack):
    """Residual check, with the same boundaries the sweep used — otherwise
    "Process" reads as residual inside "Processing", which the sweep
    deliberately left alone."""
    return re.search(bounded(literal), haystack, re.I) is not None


def sweep(text, pseudo, sweeper):
    if not isinstance(text, str):
        return text
    out = EMAIL_RE.sub(lambda m: pseudo.token(m.group(0), "EMAIL"), text)
    out = PHONE_RE.sub(lambda m: pseudo.token(m.group(0), "PHONE"), out)
    if sweeper:
        pattern, tokens = sweeper
        out = pattern.sub(
            lambda m: tokens.get(m.group(0).lower(), m.group(0)), out)
    return out


def sweep_obj(obj, pseudo, sweeper):
    if isinstance(obj, dict):
        return {k: sweep_obj(v, pseudo, sweeper) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sweep_obj(v, pseudo, sweeper) for v in obj]
    return sweep(obj, pseudo, sweeper)


def sweep_payload(raw, pseudo, sweeper):
    if not raw:
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return sweep(raw, pseudo, sweeper)
    return json.dumps(sweep_obj(parsed, pseudo, sweeper), ensure_ascii=False)


def scrub_row(row, pseudo, sweeper):
    out = dict(row)
    dims_key = next((k for k in ("customDimensions", "CustomDimensions",
                                 "Properties") if k in out), None)
    dims = out.get(dims_key) if dims_key else None
    if isinstance(dims, str):
        try:
            dims = json.loads(dims)
        except json.JSONDecodeError:
            dims = None
    if isinstance(dims, dict):
        dims = {k: (sweep_payload(v, pseudo, sweeper) if k in PAYLOAD_ATTRS
                    else sweep(v, pseudo, sweeper))
                for k, v in dims.items()}
        out[dims_key] = json.dumps(dims, ensure_ascii=False)
    for column in CONTENT_COLUMNS:
        if out.get(column):
            out[column] = sweep_payload(out[column], pseudo, sweeper)
    return out


# ------------------------------------------------------------------- verify

def verdicts(path):
    """Score a trace: {(orchestration, agent, check): passed}."""
    import trace_to_eval, run_evals
    spans = trace_to_eval.load_spans(path)
    runs, _, _ = trace_to_eval.convert(spans)
    expected = {}
    if os.path.exists("expected.json"):
        expected = {k: v for k, v in
                    json.load(open("expected.json", encoding="utf-8")).items()
                    if not k.startswith("_")}
    rows = run_evals.score(runs, {"max_empty_rate": 0.25, "expected": expected})
    return {(r["orchestration_id"], r["run_agent"], name): c["passed"]
            for r in rows for name, c in r["checks"].items()}


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trace")
    ap.add_argument("--learn", metavar="FILE",
                    help="propose candidate literals for review and exit")
    ap.add_argument("--redact-file", metavar="FILE",
                    help="reviewed {literal: KIND} map to sweep")
    ap.add_argument("-o", "--out")
    ap.add_argument("--salt", default=os.environ.get("SCRUB_SALT"))
    ap.add_argument("--verify", action="store_true",
                    help="fail if any check verdict differs after scrubbing")
    args = ap.parse_args()

    rows = load_rows(args.trace)

    if args.learn:
        candidates = propose(rows)
        # Drop protected vocabulary here rather than refusing at apply time.
        # Proposing a term that can only be rejected wastes review attention,
        # and review attention is the scarce resource that decides whether a
        # real name gets caught.
        protected = protected_vocabulary()
        dropped = sorted(k for k in candidates
                         if k.strip().lower() in protected)
        for k in dropped:
            del candidates[k]
        with open(args.learn, "w", encoding="utf-8") as fh:
            json.dump(dict(sorted(candidates.items())), fh, indent=1,
                      ensure_ascii=False)
        print(f"{len(candidates)} candidate(s) -> {args.learn}")
        if dropped:
            print(f"{len(dropped)} protected term(s) withheld (the checks "
                  f"score these): {', '.join(repr(k) for k in dropped[:8])}"
                  + (" ..." if len(dropped) > 8 else ""))
        print("\nREVIEW THIS FILE BEFORE APPLYING IT. Delete every entry that "
              "is ConnectWise vocabulary rather than customer data — a value "
              "the checks score, such as a status, a priority or a board "
              "name. Sweeping one of those rewrites the trajectory.")
        return 0

    if not (args.redact_file and args.out):
        sys.exit("--redact-file and -o are required to apply a scrub "
                 "(or use --learn to propose one)")
    if not args.salt:
        sys.exit("--salt or $SCRUB_SALT is required; without one the tokens "
                 "are a plain hash and trivially reversible")
    if len(args.salt) < MIN_SALT_LEN:
        sys.exit(f"the salt is {len(args.salt)} characters; use at least "
                 f"{MIN_SALT_LEN}. A short salt is brute-forceable against a "
                 "known name list, which is exactly the attack the "
                 "pseudonyms exist to stop.")

    with open(args.redact_file, encoding="utf-8") as fh:
        redactions = json.load(fh)
    redactions = {k: v for k, v in redactions.items() if not k.startswith("_")}

    # Refuse before writing anything. This failure is silent otherwise.
    protected = protected_vocabulary()
    illegal = sorted(k for k in redactions if k.strip().lower() in protected)
    if illegal:
        print("REFUSING: the redaction list contains vocabulary the checks "
              "score. Sweeping any of these rewrites intents, trajectories or "
              "run boundaries, and nothing downstream reports an error:\n")
        for k in illegal:
            print(f"  {k!r}")
        print("\nRemove them from the list and re-run. They are not customer "
              "data — they are the orchestrator's intent enum, the agent "
              "names, or ConnectWise status vocabulary.")
        return 1

    pseudo = Pseudonymiser(args.salt)
    sweeper = build_sweeper(redactions, pseudo)
    scrubbed = [scrub_row(r, pseudo, sweeper) for r in rows]

    parent = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(parent, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(scrubbed, fh, ensure_ascii=False)
    print(f"{len(rows)} span(s), {len(redactions)} literal(s) -> {args.out}")

    # Sidecar, not a key in the file: the output is an array of spans and
    # load_rows would hand an extra element to the converter.
    sidecar = args.out + ".scrub.json"
    with open(sidecar, "w", encoding="utf-8") as fh:
        json.dump({"salt_fingerprint": pseudo.fingerprint,
                   "literals": len(redactions),
                   "tokens_issued": len(pseudo.seen),
                   "source": os.path.basename(args.trace),
                   "scrubbed_utc": _dt.datetime.now(_dt.timezone.utc)
                                      .replace(microsecond=0).isoformat()},
                  fh, indent=1)
        fh.write("\n")
    print(f"salt fingerprint {pseudo.fingerprint} -> {sidecar}")

    # A scrubber is never provably complete; prove at least that everything
    # declared is gone.
    written = open(args.out, encoding="utf-8").read()
    residual = [v for v in redactions if still_present(v, written)]
    if residual:
        print(f"\nRESIDUAL: {len(residual)} declared literal(s) still present")
        for v in residual[:10]:
            print(f"  {v[:70]!r}")
        return 1
    print("RESIDUAL: none of the declared literals remain")

    if args.verify:
        before, after = verdicts(args.trace), verdicts(args.out)
        if before.keys() != after.keys():
            sys.exit("VERIFY FAILED: the set of runs or checks changed")
        drift = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
        if drift:
            print(f"\nVERIFY FAILED: {len(drift)} check verdict(s) changed")
            for (op, agent, check), (b, a) in list(drift.items())[:10]:
                print(f"  {agent} [{op[:12]}] {check}: {b} -> {a}")
            return 1
        print(f"VERIFY OK: {len(before)} check verdict(s) identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
