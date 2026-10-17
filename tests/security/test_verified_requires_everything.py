# -*- coding: utf-8 -*-
"""VERIFIED is the easiest thing in the product to fake.

It is one enum value, and everything upstream of it wants to be green. So
five separate conditions each independently prevent it, and each has a test
that supplies only that condition and requires a refusal. A guard that only
works when several things are wrong at once is a guard that passes exactly
when somebody is cutting a corner.

The mutation at the end answers the question the rest cannot: is the guard
load-bearing, or does the suite pass for reasons unrelated to it?
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap

import pytest
import yaml

from core.change_intelligence.apply_gate import mark_verified
from core.change_intelligence.lineage import VerifiedLineage
from core.change_intelligence.orchestrator import Orchestrator
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.states import (
    CHANGESET_TRANSITIONS,
    AlignmentState,
    ChangeSetState,
)
from core.change_intelligence.structured_diff import read_at
from core.change_intelligence.verification import (
    BLOCKERS,
    VerificationResult,
    blockers_for,
)

from tests.contract.test_verification import (  # noqa: F401
    NEW,
    OLD,
    PATH,
    WORKFLOW,
    _artifacts,
    _changeset,
    _doc,
    _parse,
    _write,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
SEED_SUITE = ROOT / "reference_system" / "tests" / "test_approval_rules.py"


@pytest.fixture
def overlay():
    return InMemoryOverlay(
        InMemoryBaseline.from_artifacts(_artifacts()), "SESSION-P8-SEC")


@pytest.fixture
def lineage():
    return VerifiedLineage(session_id="SESSION-P8-SEC", baseline_id="B")


def _run(overlay, lineage, changeset, before, after, **kwargs):
    return Orchestrator(session_id="SESSION-P8-SEC",
                        lineage=lineage).run_verification(
        changeset=changeset, snapshot_before=before, snapshot_after=after,
        parse=_parse, read_at=read_at, actor="qa", timestamp="t", **kwargs)


# --- the five conditions, one at a time --------------------------------------


def test_there_are_five_and_the_list_is_in_one_place():
    assert BLOCKERS == ("tests_failing", "evidence_stale",
                        "intent_conflict_open", "artifact_stale",
                        "approval_evidence_missing")


@pytest.mark.parametrize("condition,kwargs", [
    ("tests_failing", {"tests_failing": ("test_old_001",)}),
    ("evidence_stale", {"stale_evidence": ("workflow/routes.yaml:L37",)}),
    ("intent_conflict_open", {"open_conflicts": ("approval_threshold",)}),
])
def test_each_condition_alone_prevents_verified(condition, kwargs, overlay,
                                                lineage):
    """Only one thing wrong. Still not VERIFIED."""
    before = overlay.head
    changeset = _changeset(overlay)
    after = _write(overlay, WORKFLOW, _doc(NEW))

    turn = _run(overlay, lineage, changeset, before, after, **kwargs)
    assert not turn.result.passed, f"{condition} did not prevent a pass"
    assert condition in turn.result.blocked_by
    assert lineage.version == 0


def test_a_stale_required_artifact_prevents_verified(overlay, lineage):
    """The fourth: derived from the edits rather than supplied."""
    before = overlay.head
    changeset = _changeset(overlay)
    after = overlay.write("unrelated.txt", b"x")

    turn = _run(overlay, lineage, changeset, before, after)
    assert not turn.result.passed
    assert "artifact_stale" in turn.result.blocked_by


def test_a_missing_approval_record_prevents_verified(overlay, lineage):
    """The fifth. Applied through a legitimate path, approval lost after.

    Verification re-checks rather than trusting the recorded state, because
    "this object says it was approved" is a declared fact.
    """
    before = overlay.head
    changeset = _changeset(overlay, approved=False)
    after = _write(overlay, WORKFLOW, _doc(NEW))

    turn = _run(overlay, lineage, changeset, before, after)
    assert not turn.result.passed
    assert "approval_evidence_missing" in turn.result.blocked_by


def test_a_corrupted_approval_record_prevents_verified(overlay, lineage):
    before = overlay.head
    changeset = _changeset(overlay)
    changeset.approval = None
    after = _write(overlay, WORKFLOW, _doc(NEW))

    turn = _run(overlay, lineage, changeset, before, after)
    assert "approval_evidence_missing" in turn.result.blocked_by


def test_all_five_together_are_all_reported(overlay, lineage):
    """A guard that stops at the first problem hides the other four."""
    found = blockers_for(
        tests_failing=("t",), stale_evidence=("e",), open_conflicts=("c",),
        stale_artifacts=("a",), changeset=None)
    assert {name for name, _ in found} == set(BLOCKERS) - {
        "approval_evidence_missing"}


def test_every_blocker_carries_a_reason(overlay):
    found = blockers_for(tests_failing=("t",), stale_evidence=("e",),
                         open_conflicts=("c",), stale_artifacts=("a",))
    assert all(detail for _, detail in found)


# --- the three seeded outdated tests -----------------------------------------


def test_the_seed_still_carries_three_outdated_tests():
    """The answer key for this guard. If they vanish, the guard is untested."""
    source = SEED_SUITE.read_text(encoding="utf-8")
    assert source.count("@pytest.mark.seed_outdated") == 3


def test_those_tests_going_red_blocks_verification(overlay, lineage):
    """After the rule changes they assert the old behaviour and fail.

    If the system still reported VERIFIED with them red, the regression
    guard would be doing nothing at all.
    """
    before = overlay.head
    changeset = _changeset(overlay)
    after = _write(overlay, WORKFLOW, _doc(NEW))

    turn = _run(overlay, lineage, changeset, before, after,
                tests_failing=("test_old_001_requests_under_a_million_are_"
                               "always_automatic",
                               "test_old_002_a_seven_hundred_fifty_thousand_"
                               "request_needs_no_approval",
                               "test_old_003_managers_see_nothing_below_one_"
                               "million"))
    assert not turn.result.passed
    assert turn.result.alignment is AlignmentState.DRIFT


def test_the_blocked_result_names_the_failing_tests(overlay, lineage):
    found = blockers_for(tests_failing=("test_old_001", "test_old_002"))
    assert "test_old_001" in found[0][1]


# --- the state machine cannot be jumped --------------------------------------


def test_a_failed_verification_cannot_reach_verified(overlay):
    """There is no edge from APPLYING or REVIEW to VERIFIED."""
    for state in (ChangeSetState.PROPOSED, ChangeSetState.REVIEW,
                  ChangeSetState.APPROVED, ChangeSetState.APPLYING):
        assert ChangeSetState.VERIFIED not in CHANGESET_TRANSITIONS[state]


def test_the_only_way_into_verified_is_from_applied():
    sources = [state for state, targets in CHANGESET_TRANSITIONS.items()
               if ChangeSetState.VERIFIED in targets]
    assert sources == [ChangeSetState.APPLIED]


def test_marking_verified_still_requires_a_report(overlay):
    from core.change_intelligence.apply_gate import NotVerified
    from core.change_intelligence.changeset import ChangeSet

    applied = ChangeSet(trigger="T", base_snapshot="S",
                        state=ChangeSetState.APPLIED)
    with pytest.raises(NotVerified):
        mark_verified(applied, actor="qa", timestamp="t", report=None)


def test_a_drifted_result_is_not_a_report_that_satisfies_i14(overlay, lineage):
    """A failing verification IS a report -- and it must not mark VERIFIED.

    The state transition is the caller's, and the caller has `passed` to
    read. This asserts the value it would read is False.
    """
    before = overlay.head
    changeset = _changeset(overlay)
    after = overlay.write("unrelated.txt", b"x")
    turn = _run(overlay, lineage, changeset, before, after)
    assert turn.result is not None
    assert not turn.result.passed


def test_aligned_cannot_be_constructed_with_a_blocker():
    with pytest.raises(Exception):
        VerificationResult(
            changeset_id="C", snapshot_before="A", snapshot_after="B",
            alignment=AlignmentState.ALIGNED,
            blocked_by=["approval_evidence_missing"])


# --- the guard is structural, not incidental ---------------------------------


def test_the_orchestrator_advances_the_version_only_on_a_pass():
    source = inspect.getsource(Orchestrator.run_verification)
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "record_verified"):
            enclosing = [n for n in ast.walk(tree)
                         if isinstance(n, ast.If)
                         and n.lineno <= node.lineno
                         and getattr(n, "end_lineno", 0) >= node.lineno]
            assert enclosing, "record_verified is called unconditionally"
            assert any("passed" in ast.unparse(n.test) for n in enclosing), (
                "the version advances without checking result.passed")
            return
    pytest.fail("the orchestrator no longer records a verified lineage entry")


def test_the_orchestrator_holds_no_verdict_of_its_own():
    """It coordinates. Two implementations of one rule disagree eventually."""
    from core.change_intelligence import orchestrator as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and "ALIGNED" in ast.unparse(node):
            pytest.fail(
                f"the orchestrator decides alignment at line {node.lineno}; "
                f"the verdict belongs to verification.py")
    for banned in ("AlignmentState.", "HIGH_AT", "MEDIUM_AT", "DIRECT"):
        assert banned not in source, (
            f"the orchestrator names {banned}; that is domain logic, and a "
            f"second implementation of a business rule gives a verdict "
            f"nobody can explain")
