"""Shared helpers for the registered Foundry evaluators.

This module is INLINED into every evaluator's `code_text` at registration
time, because a code-based evaluator runs in a sandbox with no network and no
way to import from this repo. Keep it small, stdlib-only, and side-effect
free. `register_evaluators.py` strips the module docstring and the imports
below and pastes the rest above each `grade()`.

The evaluators read `item["tool_outcomes"]`, a column `to_foundry_dataset.py`
writes alongside the standard `messages`: one entry per tool call with the
result text and span status, which is what every deterministic check needs and
what `messages` alone cannot carry (a failed span returning nothing is
indistinguishable from a successful empty one).
"""

import json

TRUNC_BOUNDARY = 8192
CASCADE_MIN = 4
TOOL_PREFIX_SEP = "___"

ERROR_PATTERNS = [
    ("invalid_entity",           "not found in registry"),
    ("invalid_reference_type",   "Unknown reference type"),
    ("invalid_projection_field", "not found on service"),
    ("missing_script",           "Error: Script"),
    ("circuit_open",             "circuit open"),
    ("validation_error",         "validation_error"),
    ("rate_limited",             "rate_limited"),
    ("quota_exceeded",           "quota_exceeded"),
]

AVOIDABLE = ("missing_script", "invalid_reference_type", "invalid_entity",
             "invalid_projection_field", "empty_failed")

EMPTY_MARKERS = ('"count":0', '"count_hint":0', '"matches":[]', '"data":[]',
                 '"count": 0', '"count_hint": 0', '"matches": []', '"data": []')


def base_tool_name(name):
    return name.rsplit(TOOL_PREFIX_SEP, 1)[-1] if name else name


def classify_error(result, span_ok=True):
    if not result:
        return None if span_ok else "empty_failed"
    head = result[:600]
    for kind, sig in ERROR_PATTERNS:
        if sig in head:
            return kind
    if head.startswith("Error:") or '"error"' in head:
        return "other_error"
    return None


def is_empty_result(result):
    if not result:
        return False
    return any(m in result[:600] for m in EMPTY_MARKERS)


RESULT_HEAD = 600          # every check reads at most this much


def outcomes(item):
    """Normalised tool calls: [{tool, arguments, result, success, ...}].

    A result arrives as `result_head` (the first RESULT_HEAD characters) plus
    `result_len` (the original length), because that is all any check needs —
    classification and emptiness read the head, truncation reads the length —
    and carrying the whole body made one dataset row 1.1 MB and the run 500.
    `result` is still accepted, for a dataset built before the split.
    """
    raw = item.get("tool_outcomes") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    out = []
    for o in raw:
        head = o.get("result_head")
        if head is None:
            head = o.get("result") or ""
        length = o.get("result_len")
        if length is None:
            length = len(head)
        kind = classify_error(head, o.get("success", True))
        out.append({
            "tool": o.get("tool", ""),
            "arguments": o.get("arguments") or {},
            "result": head,
            "result_len": length,
            "success": o.get("success", True),
            "error_kind": kind,
            "errored": kind is not None,
            "empty": is_empty_result(head),
            "truncated": length == TRUNC_BOUNDARY,
        })
    return out


def ratio_score(bad, total):
    """1.0 when clean, falling linearly with the bad fraction."""
    if not total:
        return 1.0
    return max(0.0, 1.0 - (bad / float(total)))
