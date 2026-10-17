# -*- coding: utf-8 -*-
"""One door. Several apply paths means several places the check can be missing.

The failure this prevents is gradual, not dramatic: a feature needs to write
one file, writing through the gate is inconvenient for that one case, a small
helper appears, and six months later the helper is how half the product
writes. Nobody decided that. It happened because the first exception was
reasonable.

So the count is asserted, by walking the syntax tree of the package rather
than grepping it. A grep for `overlay.write` misses `w = overlay.write` and
finds the string inside this docstring.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE = (pathlib.Path(__file__).resolve().parents[2]
           / "core" / "change_intelligence")

#: The single apply path. If this list ever has two entries, the property
#: this file exists to protect is gone.
PERMITTED_WRITERS = {("apply_gate.py", "apply_changeset")}

#: Session-overlay mutators. Calling any of these writes state.
_WRITE_METHODS = {"write", "clear"}


def _enclosing_function(tree, node):
    best = None
    for candidate in ast.walk(tree):
        if not isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (candidate.lineno <= node.lineno
                and getattr(candidate, "end_lineno", node.lineno) >= node.lineno):
            if best is None or candidate.lineno > best.lineno:
                best = candidate
    return best.name if best else "<module>"


def _writers() -> set:
    """Every function in the package that calls an overlay mutator."""
    found = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in _WRITE_METHODS:
                continue
            # `overlay.write(...)` and `self._overlay.write(...)` count;
            # `manifest.clear()` on a plain dict does not.
            receiver = ast.unparse(func.value)
            if "overlay" not in receiver.lower():
                continue
            found.add((path.name, _enclosing_function(tree, node)))
    return found


def test_exactly_one_function_writes_through_an_overlay():
    writers = _writers()
    unexpected = writers - PERMITTED_WRITERS
    assert not unexpected, (
        f"these also write: {sorted(unexpected)}. Every write must go "
        f"through apply_changeset, which is where the approval check lives. "
        f"A second path is a second place for the check to be absent from.")


def test_the_permitted_list_is_not_empty_and_has_one_entry():
    """The guard on the guard.

    The cheapest way to make the test above pass is to add the new writer
    to PERMITTED_WRITERS. This makes that visible.
    """
    assert len(PERMITTED_WRITERS) == 1


def test_the_one_writer_calls_the_approval_check():
    source = (PACKAGE / "apply_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "apply_changeset":
            body = ast.unparse(node)
            assert "check_approved(changeset)" in body
            assert "check_version(changeset)" in body
            assert "check_snapshot(" in body
            return
    pytest.fail("apply_changeset is gone")


def test_the_checks_run_before_anything_is_written():
    """Order matters: a write that happens before the check is not gated."""
    source = (PACKAGE / "apply_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "apply_changeset")

    check_lines, write_lines = [], []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        rendered = ast.unparse(node.func)
        if rendered.startswith("check_"):
            check_lines.append(node.lineno)
        # apply_changeset writes through `overlay.write_all`
        # (one call, one new snapshot for every staged artifact), not a
        # per-artifact `overlay.write` loop. Both are the single write path
        # -- `write` is `write_all` with one entry (P7_FROZEN SS7) -- so both
        # names are watched for here.
        if (rendered.endswith(".write") or rendered.endswith(".write_all")) \
                and "overlay" in rendered.lower():
            write_lines.append(node.lineno)

    assert check_lines and write_lines
    assert max(check_lines) < min(write_lines), (
        "a write is reachable before the last authorisation check")


def test_no_module_writes_to_the_baseline():
    """Apply goes to the session overlay. The baseline is immutable (I17)."""
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write"
                    and "baseline" in ast.unparse(node.func.value).lower()):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"{offenders} write to the baseline; apply writes to the session "
        f"overlay so that the baseline hash never moves and reset keeps "
        f"working (I17, I18)")


def test_the_apply_path_offers_no_force_or_skip_argument():
    """A parameter that turns the gate off is the gate being off."""
    source = (PACKAGE / "apply_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "apply_changeset")
    names = {a.arg for a in function.args.args + function.args.kwonlyargs}
    for banned in ("force", "skip_approval", "unsafe", "bypass", "dry",
                   "ignore_approval"):
        assert banned not in names, (
            f"apply_changeset takes {banned!r}; a caller who can pass it "
            f"does not need to bypass the gate, because the gate has a "
            f"door in it")
