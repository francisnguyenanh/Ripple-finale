# -*- coding: utf-8 -*-
"""A proposed edit as a path and a value, never as text.

    RIGHT   {path: "approval_routes[0].condition.value",
             from: <old>, to: <new>, unit: "JPY"}

    WRONG   "- value: <old>\\n+ value: <new>"

The difference is not presentation. A text diff cannot be checked: nothing
can ask it whether the document is still valid afterwards, whether a route
became unreachable, or whether the value it claims to replace is the one
actually there right now. A structured edit answers all three, which is also
what lets P8 confirm the change was applied as approved rather than merely
that a file changed.

Three things every edit carries, each with a test:

**`expected_current`.** The edit states what it believes is there. If the
snapshot moved between building the diff and applying it, the edit is STALE
and is refused -- not applied blind, and not silently reconciled. A proposal
built against a world that no longer exists is a different proposal.

**A post-condition.** Applying produces a new document, and the document is
re-validated: schema still valid, no route made unreachable. An edit that
produces an invalid workflow is rejected before a person is asked to approve
it, because asking somebody to approve something that cannot work wastes the
one resource the product is trying to protect.

**A spreadsheet edit is a CELL edit.** `Sheet1!C4` with an old and a new
value, not a binary blob replacement, for the same reason.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field


class StaleDiff(Exception):
    """The world moved under the diff. Refuse rather than guess."""


class InvalidAfterEdit(Exception):
    """Applying would produce a document that no longer works."""


class NoSuchLocation(Exception):
    """The edit names a location the document does not have."""


#: What an edit can be made to. Each has a locator grammar of its own.
EDIT_KINDS = ("workflow", "config", "spreadsheet")

_INDEX = re.compile(r"^(?P<name>[^\[\]]+)\[(?P<index>\d+)\]$")
_CELL = re.compile(r"^(?P<sheet>[^!]+)!(?P<column>[A-Z]+)(?P<row>\d+)$")


@dataclass(frozen=True)
class StructuredEdit:
    """One value, in one named place, with what it is replacing.

    Frozen: an edit that could be rewritten after a person read it is not
    the thing they approved.
    """

    artifact: str
    path: str
    expected_current: object
    proposed: object
    kind: str = "workflow"
    unit: str = ""
    reason: str = ""
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.kind not in EDIT_KINDS:
            raise ValueError(f"{self.kind!r} is not one of {EDIT_KINDS}")
        if not self.path:
            raise ValueError("an edit with no path is a text diff wearing a hat")
        if not self.reason:
            raise ValueError(
                f"{self.artifact}:{self.path} proposes a change with no "
                f"reason; a reviewer cannot approve what nobody justified")

    @property
    def is_noop(self) -> bool:
        return self.expected_current == self.proposed

    def rendered(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        return (f"{self.artifact}  {self.path}: "
                f"{self.expected_current}{unit} -> {self.proposed}{unit}")


@dataclass
class DiffResult:
    """A set of edits against one artifact, and what applying them produced."""

    artifact: str
    edits: tuple[StructuredEdit, ...] = ()
    after: object = None
    notes: list = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.edits


# --- walking a document ------------------------------------------------------


def _steps(path: str) -> list:
    """`a.b[0].c` -> ["a", "b", 0, "c"]. Deterministic, no eval."""
    steps: list = []
    for part in path.split("."):
        match = _INDEX.match(part)
        if match:
            steps.append(match.group("name"))
            steps.append(int(match.group("index")))
        else:
            steps.append(part)
    return steps


def read_at(document, path: str):
    """The value the document currently holds at `path`.

    Raises rather than returning None for a missing path: "the value is
    None" and "there is no such place" are different facts, and a diff that
    conflates them will happily propose an edit to a location that does not
    exist.
    """
    node = document
    walked: list = []
    for step in _steps(path):
        walked.append(step)
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError) as exc:
            here = ".".join(str(s) for s in walked)
            raise NoSuchLocation(
                f"{path} does not exist in this document; it stops at "
                f"{here!r}") from exc
    return node


def _write_at(document, path: str, value) -> None:
    steps = _steps(path)
    node = document
    for step in steps[:-1]:
        node = node[step]
    node[steps[-1]] = value


# --- applying ----------------------------------------------------------------


def apply_edits(document, edits, *, validate=None) -> DiffResult:
    """Produce a NEW document with the edits applied, or refuse.

    The original is deep-copied first. A diff routine that mutated its input
    would make "show me what this would look like" indistinguishable from
    "do it", which is the whole distinction the product rests on.
    """
    edits = tuple(edits)
    if not edits:
        return DiffResult(artifact="", edits=(), after=document)

    artifact = edits[0].artifact
    after = copy.deepcopy(document)
    notes: list = []

    for edit in edits:
        actual = read_at(after, edit.path)
        if actual != edit.expected_current:
            raise StaleDiff(
                f"{edit.artifact}:{edit.path} holds {actual!r}, but this "
                f"diff was built when it held {edit.expected_current!r}; "
                f"the snapshot moved and the proposal now describes a "
                f"different change")
        if edit.is_noop:
            notes.append(f"{edit.path}: already {edit.proposed}, nothing to do")
            continue
        _write_at(after, edit.path, edit.proposed)

    if validate is not None:
        problems = list(validate(after))
        if problems:
            raise InvalidAfterEdit(
                f"{artifact} would no longer be valid after this change: "
                + "; ".join(problems))

    return DiffResult(artifact=artifact, edits=edits, after=after, notes=notes)


def dry_run(document, edits, *, validate=None) -> tuple:
    """Apply into a copy and report, without raising.

    The review screen needs to show a stale or invalid diff as a finding,
    not fail to render. `apply_edits` raises because the apply path must;
    this wraps it because the *reading* path must not.
    """
    try:
        return apply_edits(document, edits, validate=validate), ""
    except (StaleDiff, InvalidAfterEdit, NoSuchLocation) as exc:
        return None, str(exc)


# --- the workflow post-condition ---------------------------------------------


def _condition(route) -> tuple:
    """`(field, operator, value)`, accepting either spelling of the key.

    The seed writes `op`; the schema documentation writes `operator`. A
    validator that knew only one of them would quietly check nothing on
    real files, which is the worst way for a guard to fail.
    """
    condition = route.get("condition", {}) or {}
    operator = condition.get("op", condition.get("operator", ""))
    return condition.get("field", ""), operator, condition.get("value")


def unreachable_routes(workflow) -> list:
    """Routes no request can reach, given that the FIRST match decides.

    The specific failure this catches: moving one threshold so that an
    earlier route swallows everything a later route used to receive. The
    later route is still in the file, still reviewed, still believed to
    work -- and is now dead. A text diff shows two changed lines and says
    nothing about it.

    Only routes on the same field and the same direction are compared. Two
    routes testing different things never shadow each other, and reporting
    them as if they did would make this list something a reader skims past.
    """
    dead = []
    #: field -> the most permissive bound an earlier route already claims.
    claimed: dict = {}

    for index, route in enumerate(workflow.get("approval_routes", [])):
        field_name, operator, value = _condition(route)
        if not isinstance(value, (int, float)):
            continue

        seen = claimed.get((field_name, operator))
        if seen is not None:
            earlier_index, earlier_value = seen
            shadowed = (value >= earlier_value if operator in (">", ">=")
                        else value <= earlier_value)
            if shadowed:
                dead.append(
                    f"approval_routes[{index}] "
                    f"({route.get('id', 'unnamed')}) can never be reached: "
                    f"approval_routes[{earlier_index}] already takes every "
                    f"request with {field_name} {operator} {earlier_value}")
                continue

        if seen is None or (
                value < seen[1] if operator in (">", ">=") else value > seen[1]):
            claimed[(field_name, operator)] = (index, value)

    return dead


def workflow_schema_problems(workflow) -> list:
    """The minimum a workflow must still have to be a workflow."""
    problems = []
    routes = workflow.get("approval_routes")
    if not isinstance(routes, list) or not routes:
        return ["there are no approval routes left"]
    for index, route in enumerate(routes):
        if "condition" not in route:
            problems.append(f"approval_routes[{index}] has no condition")
            continue
        _, operator, value = _condition(route)
        if not operator:
            problems.append(
                f"approval_routes[{index}].condition has no operator")
        if value is None:
            problems.append(f"approval_routes[{index}].condition has no value")
    return problems


def validate_workflow(workflow) -> list:
    """Both post-conditions, in the order a reader would want them."""
    return workflow_schema_problems(workflow) + unreachable_routes(workflow)


# --- spreadsheets ------------------------------------------------------------


def cell_edit(artifact: str, cell: str, *, expected_current, proposed,
              reason: str, unit: str = "", evidence: str = "") -> StructuredEdit:
    """`Sheet1!C4`, checked. A spreadsheet diff is a cell diff.

    Validating the locator here rather than at apply time means a malformed
    reference is caught while the proposal is being built, when there is
    still somebody around who knows what was meant.
    """
    if not _CELL.match(cell):
        raise NoSuchLocation(
            f"{cell!r} is not a cell reference; expected Sheet!Column Row, "
            f"because a spreadsheet change is made to a cell and a diff that "
            f"replaces the file cannot be reviewed")
    return StructuredEdit(
        artifact=artifact, path=cell, expected_current=expected_current,
        proposed=proposed, kind="spreadsheet", unit=unit, reason=reason,
        evidence=evidence)


def render(result: DiffResult) -> str:
    """Deterministic. The same edits always render the same way."""
    if result.is_empty:
        return "no structured edit proposed"
    out = [f"STRUCTURED DIFF   {result.artifact}"]
    for edit in sorted(result.edits, key=lambda e: e.path):
        out.append(f"  {edit.rendered()}")
        out.append(f"      why: {edit.reason}")
        if edit.evidence:
            out.append(f"      evidence: {edit.evidence}")
    for note in result.notes:
        out.append(f"  note: {note}")
    return "\n".join(out)


__all__ = [
    "DiffResult",
    "EDIT_KINDS",
    "InvalidAfterEdit",
    "StaleDiff",
    "StructuredEdit",
    "NoSuchLocation",
    "apply_edits",
    "cell_edit",
    "dry_run",
    "read_at",
    "render",
    "unreachable_routes",
    "validate_workflow",
    "workflow_schema_problems",
]
