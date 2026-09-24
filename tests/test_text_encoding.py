"""Every text file this repo opens names its encoding.

Without one, Python uses the locale's: UTF-8 on Linux, cp1252 on most
Windows machines. trace_to_eval.py writes UTF-8, so a test reading its
output with a bare read_text() passed in CI and failed on Windows as soon
as a trace held a character outside cp1252 -- the re-scrubbed triage trace
carries a literal "”", whose UTF-8 bytes end in 0x9d, which cp1252
cannot decode. Half this repo's users are on Windows, so the locale default
is never the right one.
"""
import ast
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT_METHODS = {"read_text", "write_text"}
NOT_FILES = {"os", "urllib", "webbrowser"}   # os.open takes flags, not a mode


def _mode(call):
    if len(call.args) > 1:
        return call.args[1]
    return next((k.value for k in call.keywords if k.arg == "mode"), None)


def unencoded_calls(source):
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if any(k.arg == "encoding" for k in node.keywords):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
        if name in TEXT_METHODS:
            yield node.lineno, name
        elif name == "open":
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id in NOT_FILES:
                continue
            mode = _mode(node)
            if isinstance(mode, ast.Constant) and "b" in str(mode.value):
                continue
            yield node.lineno, name


def python_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in {"node_modules", "purge.git", "out"}]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def test_the_scan_sees_what_it_looks_for():
    found = [n for _, n in unencoded_calls(
        "open(p)\nopen(p, 'rb')\nopen(p, encoding='utf-8')\n"
        "x.read_text()\nx.read_text(encoding='utf-8')\nos.open(p, 0)\n")]
    assert found == ["open", "read_text"]


def test_every_text_open_names_its_encoding():
    bad = []
    for path in python_files():
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        rel = os.path.relpath(path, REPO)
        bad += [f"{rel}:{line}: {name}()" for line, name in
                unencoded_calls(source)]
    assert not bad, (
        f"{len(bad)} text-mode open/read_text/write_text without encoding= "
        "(the locale default is cp1252 on Windows):\n  " + "\n  ".join(bad[:20]))
