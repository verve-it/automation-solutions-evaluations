"""Nothing under traces/ may carry unredacted customer data.

This is the check that was missing. `scrub_trace.py` exists, its tests pass,
and its two-step workflow is documented -- and every trace in the repo was
committed without it ever being run. Nothing noticed, because nothing looked.

An e-mail address is the marker: high precision, and the scrubber has always
caught them when it ran. A file with real addresses in it has not been
scrubbed, whatever else is true of it.
"""
import os
import re
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
TOKEN_RE = re.compile(r"\b(?:PERSON|COMPANY|EMAIL|PHONE|PLACE|VALUE)_[0-9a-f]{8}\b")

# Domains that cannot identify anyone: RFC 2606 reserved names, and the
# pseudonymiser's own output. Everything else is treated as real.
SAFE_DOMAINS = ("example.com", "example.org", "example.net", "invalid",
                "localhost", "test")


def tracked_traces():
    out = subprocess.run(["git", "ls-files", "traces/"], cwd=REPO,
                         capture_output=True, text=True, check=True)
    return [p for p in out.stdout.split()
            if not p.endswith((".md", ".meta.json"))]


def real_emails(path, cap=20):
    found = set()
    with open(os.path.join(REPO, path), encoding="utf-8",
              errors="replace") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), ""):
            for m in EMAIL_RE.finditer(chunk):
                addr = m.group(0)
                if not addr.lower().endswith(SAFE_DOMAINS):
                    found.add(addr)
                    if len(found) >= cap:
                        return found
    return found


def test_there_are_traces_to_check():
    """A guard that silently checks nothing is worse than no guard."""
    assert tracked_traces()


@pytest.mark.parametrize("path", tracked_traces())
def test_a_committed_trace_carries_no_real_email_addresses(path):
    found = real_emails(path)
    assert not found, (
        f"\n{path} contains {len(found)}+ real e-mail addresses, so it was "
        f"never scrubbed.\n"
        f"  examples: {sorted(found)[:5]}\n\n"
        f"  python3 scrub_trace.py {path} --learn candidates.json\n"
        f"  # review candidates.json by hand, then:\n"
        f"  python3 scrub_trace.py {path} --redact-file candidates.json \\\n"
        f"      --verify -o {path}\n\n"
        f"Re-freeze the baselines afterwards and confirm no verdict moved. "
        f"Rewriting the file does not remove it from git history.")


@pytest.mark.parametrize("path", tracked_traces())
def test_a_committed_trace_shows_evidence_of_being_scrubbed(path):
    """Absence of e-mails is necessary, not sufficient.

    A scrubbed trace carries pseudonym tokens where the data used to be. Zero
    tokens means the sweeper never ran over this file -- which is exactly
    what was true of all four committed traces.
    """
    with open(os.path.join(REPO, path), encoding="utf-8",
              errors="replace") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), ""):
            if TOKEN_RE.search(chunk):
                return
    pytest.fail(f"{path} contains no pseudonym tokens — scrub_trace.py has "
                f"never been run over it. See traces/README.md.")
