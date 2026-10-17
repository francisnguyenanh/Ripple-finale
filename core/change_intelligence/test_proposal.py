# -*- coding: utf-8 -*-
"""Which tests this change turns red, named one by one.

The generation half is out of scope: nothing here writes a new
test. What is emphatically NOT out of scope is the detection half, and the
two get confused because they sound like one feature. They are not. Cutting
the ability to WRITE a test must never quietly cut the ability to NOTICE
that one has gone stale -- only the second is what the product claims.

Granularity is the whole value. "tests/test_approval_rules.py needs
updating" sends somebody to read four hundred lines. "these three functions
assert a value inside the affected range, here are their line numbers"
sends them to three places, and it is checkable: the range is computed from
the change, not from a list somebody maintained.

    affected range = [min(current, proposed), max(current, proposed))

A test asserting an outcome for an amount in that range is asserting the
OLD behaviour, whatever its name says. This is how a suite rots: the
assertion was true when it was written, and nothing about it looks like a
liability until the policy moves.

Found by walking the syntax tree. A regular expression over the text would
match the number in a comment explaining why the test exists, which is the
opposite of a stale assertion.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class StaleTest:
    """One test function whose expectation this change invalidates."""

    artifact: str
    function: str
    line: int
    values: tuple
    quote: str

    @property
    def evidence(self) -> str:
        return f"{self.artifact}:{self.line}"

    def rendered(self) -> str:
        values = ", ".join(f"{v:,}" for v in self.values)
        return (f"{self.artifact}:{self.line} {self.function} asserts an "
                f"outcome for {values}")


def _literals(node) -> list:
    return [child.value for child in ast.walk(node)
            if isinstance(child, ast.Constant)
            and isinstance(child.value, (int, float))
            and not isinstance(child.value, bool)]


def _first_assert_line(node, source_lines) -> tuple:
    """The line a reader should open, and the line itself.

    The function's own `def` line says nothing; the assertion is where the
    stale expectation actually lives, and quoting it means the finding can
    be checked without opening the file (I4).
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            line = child.lineno
            return line, source_lines[line - 1].strip()
    return node.lineno, source_lines[node.lineno - 1].strip()


def stale_tests(source: str, *, artifact: str, current_value,
                proposed_value) -> tuple:
    """Test functions asserting an outcome this change moves.

    Returns them sorted by line, so two runs over the same file produce the
    same list and a reviewer's second look matches their first.
    """
    if not isinstance(current_value, (int, float)) or \
            not isinstance(proposed_value, (int, float)):
        return ()

    low, high = sorted((current_value, proposed_value))
    if low == high:
        return ()

    lines = source.splitlines()
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        # Half-open: a test asserting the boundary value itself is asserting
        # the NEW rule's boundary, and flagging it would send somebody to
        # read a test that is about to become correct.
        affected = tuple(sorted({v for v in _literals(node) if low <= v < high}))
        if not affected:
            continue
        line, quote = _first_assert_line(node, lines)
        found.append(StaleTest(artifact=artifact, function=node.name,
                               line=line, values=affected, quote=quote))

    return tuple(sorted(found, key=lambda t: t.line))


def describe(stale, *, current_value, proposed_value) -> str:
    """One sentence a reviewer reads before deciding."""
    if not stale:
        return "no test asserts an outcome inside the affected range"
    return (f"{len(stale)} test(s) assert outcomes for amounts between "
            f"{min(current_value, proposed_value):,} and "
            f"{max(current_value, proposed_value):,}; they encode the value "
            f"as it is today and will fail once it changes")


def render(stale, *, current_value, proposed_value) -> str:
    out = [describe(stale, current_value=current_value,
                    proposed_value=proposed_value)]
    for test in stale:
        out.append(f"  {test.rendered()}")
        out.append(f"      {test.quote}")
    return "\n".join(out)


__all__ = ["StaleTest", "describe", "render", "stale_tests"]
