"""Find a planted value in text, in the forms it takes on the way out.

A recorded value rarely leaves verbatim: json.dumps escapes it, a URL quotes
it, a model base64-encodes a hand-off around it, and a reason string clips
it. Checking only the literal would pass a leak in any of those forms.
"""
import base64
import json
import urllib.parse


def forms(canary):
    out = {canary, json.dumps(canary)[1:-1], urllib.parse.quote(canary),
           canary[:8]}
    # base64 of the canary at each alignment, minus the edge characters that
    # depend on the bytes around it: what a run of encoded text containing
    # it must contain.
    for k in range(3):
        enc = base64.b64encode(b"x" * k + canary.encode()).decode()
        lead = (k * 4 + 2) // 3 + 1
        out.add(enc[lead:lead + 8])
    return sorted(f for f in out if len(f) >= 8)


def found(texts, canary):
    """[(form, where)] for every form of `canary` in `texts`, a dict of
    channel name -> text."""
    return [(f, where) for where, text in texts.items()
            for f in forms(canary) if f in (text or "")]
