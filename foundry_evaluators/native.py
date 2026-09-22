"""Run the same checks through azure-ai-evaluation's local `evaluate()`.

The eight checks exist once, in `checks.py`, as `grade_*(sample, item)`.
Foundry runs them in its evaluator catalog. This adapts the same functions to
the shape `azure-ai-evaluation` wants, so the local gate runs the native
harness over the native evaluator objects rather than a second implementation
of the same logic.

`evaluate()` runs entirely offline: `azure_ai_project` is optional and the
promptflow-service dependency is gone. Verified with every AZURE_* variable
deleted — no project, no credentials, no network.

Two things it does that are easy to trip over
---------------------------------------------
**It introspects the signature to decide which dataset columns an evaluator
needs.** A `**kwargs` parameter is read as a required input literally named
`kw`, and the run fails with "missing required inputs: ['kw']". So the
adapter builds an explicit keyword-only signature from the dataset's own
columns rather than accepting anything.

**Extra keys in the returned dict survive.** A string comes back per row and
is left alone; a number is also aggregated into `metrics`. That is what lets
the reason strings — "4 avoidable call(s): empty_failedx2, missing_scriptx1"
— travel with the score instead of being flattened to a float.
"""

from __future__ import annotations

import inspect

import checks


def dataset_columns(rows):
    """Every column present across the rows, which is what evaluate() maps."""
    seen = []
    for row in rows:
        for k in row:
            if k not in seen:
                seen.append(k)
    return seen


def as_evaluator(name, grade_fn, columns):
    """Wrap a `grade_*(sample, item)` as an evaluate()-compatible callable.

    The signature is built explicitly from `columns` because evaluate()
    reads it to work out required inputs. Returning the score under `name`
    keeps the metric key identical to the registered evaluator's.
    """
    def _call(**row):
        score = grade_fn({}, row)
        return {name: score}

    _call.__name__ = name
    _call.__signature__ = inspect.Signature([
        inspect.Parameter(c, inspect.Parameter.KEYWORD_ONLY, default=None)
        for c in columns
    ])
    return _call


def evaluator_set(rows, only=None):
    """{name: callable} for evaluate(), over the columns these rows carry."""
    columns = dataset_columns(rows)
    out = {}
    for name, spec in checks.EVALUATORS.items():
        if only and name not in only:
            continue
        out[name] = as_evaluator(name, spec[0], columns)
    return out


def thresholds(only=None):
    """{name: (threshold, gating)} — the gate, not the score."""
    return {n: (s[4], s[5]) for n, s in checks.EVALUATORS.items()
            if not only or n in only}


def verdicts(result):
    """evaluate() output -> [{evaluator: passed}] per row, applying thresholds.

    A score is a number; a verdict is a number against a threshold. Foundry
    stores the score and applies the threshold at display time; the gate
    needs the verdict, so it is computed here from the same constants the
    registered evaluators carry.
    """
    limits = thresholds()
    out = []
    for row in result.get("rows", []):
        row_verdicts = {}
        for key, value in row.items():
            if not key.startswith("outputs."):
                continue
            metric = key.split(".")[-1]
            if metric in limits and isinstance(value, (int, float)):
                row_verdicts[metric] = value >= limits[metric][0]
        out.append(row_verdicts)
    return out
