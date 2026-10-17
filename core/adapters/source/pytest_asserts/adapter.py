# -*- coding: utf-8 -*-
"""`tests` adapter -- what the suite asserts, as CORROBORATION.

The evidence view has a `test` column, and it matters:
a green test asserting behaviour nobody wrote down is strong evidence the
behaviour is INTENDED rather than accidental. That distinction is the whole
input to the later stages, and until this module existed nothing in the product read a
test file at all -- the column would have rendered empty on a repository
that contains the assertion in plain sight.

What this adapter is NOT
------------------------
It does not produce candidates, and `to_candidates` is deliberately left
without a branch for `test_assertion`. A test is evidence ABOUT intent, not
a statement of policy, and `discovery.CORROBORATING_FAMILIES` says so. If a
test could document a rule, then UN-003 -- behaviour asserted by a green
test that no document describes -- would stop being a finding at the moment
this adapter was added, which is the opposite of the point.

So the records land in the Ingestion and are read by the evidence view,
and by nothing that decides anything.

Two things are extracted and both are quoted verbatim (I4):

    literal      an assert whose text contains a number written out. The
                 value was true when it was typed and nothing rechecks it.
    derived      an assert that READS the value instead of restating it.
                 It survives a policy change, and saying which kind a test
                 is matters more than counting them.
"""

from __future__ import annotations

import ast
from typing import Iterator

from core.adapters.base import IRRecord, Provenance

#: Accessors a test reads a value THROUGH rather than restating. A test
#: calling one of these asserts a relationship; a test with a bare
#: literal asserts a snapshot of the world on the day it was written.
#:
#: ATTRIBUTE ACCESS ONLY, and `config` is deliberately absent. The first
#: version matched the bare name too, which labelled
#: `route_request(request_for(1_000_000), config)` as reading its value
#: from configuration -- the exact opposite of what that line does.
#: Passing the config object somewhere is not reading a value out of it,
#: and a check that cannot tell those apart inverts the one distinction
#: this adapter exists to draw. The evidence view caught it on the first
#: render, which is the argument for rendering early.
_VALUE_ACCESSORS = ("threshold", "limit", "ceiling", "max_amount")


def _numbers_in(node: ast.AST) -> list[int | float]:
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, (int, float)) \
                and not isinstance(child.value, bool):
            found.append(child.value)
    return found


def _reads_config(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Attribute)
               and child.attr in _VALUE_ACCESSORS
               for child in ast.walk(node))


def _markers(fn: ast.FunctionDef) -> tuple[str, ...]:
    """Decorator names, so a suite can say of itself that a test is known
    stale. Reported, never acted on -- a marker is a claim by whoever typed
    it, and this module has no way to check it."""
    names = []
    for decorator in fn.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        parts = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        if parts:
            names.append(".".join(reversed(parts)))
    return tuple(sorted(names))


def extract_bytes(raw: bytes, path: str, snapshot_id: str) -> Iterator[IRRecord]:
    """Every assertion in every test function, with the line it is on.

    The quote is the SOURCE LINE, sliced out of the file being read. Three
    times in this project a quote has turned out to be a sentence ABOUT a
    line rather than the line -- each time it passed review and failed
    verification, because a description cannot be found in the artifact it
    describes. Reading it back out of `lines` is the only version that
    cannot drift.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return

    try:
        tree = ast.parse(text)
    except SyntaxError:
        # A test file that does not parse is a fact worth having, but it is
        # not this module's to report: it yields nothing and `skipped`
        # already records files nothing read.
        return

    lines = text.splitlines()

    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test_"):
            continue
        markers = _markers(fn)

        for node in ast.walk(fn):
            if not isinstance(node, ast.Assert):
                continue
            start = node.lineno
            end = getattr(node, "end_lineno", start) or start
            quote = "\n".join(lines[start - 1:end]).strip()
            if not quote:
                continue

            numbers = _numbers_in(node.test)
            derived = _reads_config(node.test)

            yield IRRecord(
                kind="test_assertion",
                id=f"test::{path}::{fn.name}::L{start}",
                payload={
                    "test_name": fn.name,
                    "values": numbers,
                    # A test can be both: `config.threshold("BR-001") - 1`
                    # reads the value AND writes a number. Reporting one
                    # would make the more interesting case invisible.
                    "restates_a_literal": bool(numbers),
                    "reads_the_value": derived,
                    "markers": markers,
                },
                provenance=(Provenance(
                    file=path,
                    line_start=start,
                    line_end=end,
                    method="ast",
                    quote=quote,
                    snapshot_id=snapshot_id),))


class PytestAssertAdapter:
    name = "pytest_asserts"
    languages = ["python"]

    def parse_bytes(self, raw: bytes, path: str, snapshot_id: str) -> list[IRRecord]:
        return list(extract_bytes(raw, path, snapshot_id))


ADAPTER = PytestAssertAdapter()
