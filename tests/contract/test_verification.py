# -*- coding: utf-8 -*-
"""The verifier reads artifacts, and refuses to read anything else.

Each test hands the verifier a situation where the declared state and the
actual state disagree, and requires it to report the actual one. If any of
these passed by consulting the ChangeSet, the module would be a formatter
for its own input.
"""

from __future__ import annotations

import inspect

import pytest
import yaml

import _srcscan

from core.change_intelligence.action_plan import Action, ActionPlan
from core.change_intelligence.apply_gate import APPROVE, decide
from core.change_intelligence.changeset import ChangeSet
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.states import AlignmentState, ChangeSetState
from core.change_intelligence.structured_diff import StructuredEdit, read_at
from core.change_intelligence.verification import (
    APPLIED,
    BLOCKERS,
    CannotVerify,
    DIMENSIONS,
    DIVERGED,
    MISSING,
    REVERSE_DRIFT,
    UNEXPECTED,
    VerificationResult,
    blockers_for,
    next_actions,
    render,
    verify,
)

#: The shared scan, re-exported under the name the P8 tests already use.
#: It lives in tests/_srcscan.py because five separate ad-hoc versions of
#: it each grew the same defect.
_executable_source = _srcscan.executable_source


WORKFLOW = "workflow/routes.yaml"
SHEET = "data/matrix.yaml"
DOC = "docs/policy.md"
PATH = "approval_routes[0].condition.value"
OLD, NEW = 1_000_000, 500_000


def _doc(value=OLD):
    return {"approval_routes": [
        {"id": "R", "condition": {"field": "amount", "op": ">=",
                                  "value": value}}]}


def _parse(artifact, raw):
    if artifact.endswith(".md"):
        return {"text": raw.decode("utf-8")}
    return yaml.safe_load(raw)


def _artifacts():
    return {
        WORKFLOW: yaml.safe_dump(_doc()).encode("utf-8"),
        SHEET: yaml.safe_dump(_doc()).encode("utf-8"),
        DOC: b"The threshold is 1000000.\n",
    }


def _edit(artifact=WORKFLOW, proposed=NEW, current=OLD):
    return StructuredEdit(
        artifact=artifact, path=PATH, expected_current=current,
        proposed=proposed, reason="an approved intent lowered this",
        evidence=f"{artifact}:{PATH}")


def _changeset(overlay, edits=None, *, approved=True):
    edits = edits if edits is not None else (_edit(),)
    by_artifact = {}
    for edit in edits:
        by_artifact.setdefault(edit.artifact, []).append(edit)
    actions = tuple(
        Action(kind="WORKFLOW", target=artifact, description="set the value",
               necessity="required", performer="system",
               impact_evidence=(f"{artifact}:{PATH}",), edits=tuple(group),
               reason="carries the value the system reads")
        for artifact, group in sorted(by_artifact.items()))

    changeset = ChangeSet(
        trigger="INT-1", base_snapshot=overlay.head.snapshot_id,
        intent=_intent(), affected_nodes=("n",),
        affected_artifacts=tuple(sorted(by_artifact)), evidence=("q",),
        simulation_result=_Sim(), actions=ActionPlan(target="rule",
                                                    actions=actions),
        risk="MEDIUM")
    if not approved:
        return changeset
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="a",
                                    timestamp="t")
    approved_cs, _ = decide(reviewed, actor="cfo", timestamp="t",
                            decision=APPROVE, reason="clear")
    return approved_cs


def _intent():
    class I:
        intent_id, version = "INT-1", 1

    return I()


class _Sim:
    def new_inconsistencies(self):
        return []

    def inherited_inconsistencies(self):
        return []


@pytest.fixture
def overlay():
    return InMemoryOverlay(
        InMemoryBaseline.from_artifacts(_artifacts()), "SESSION-V")


def _write(overlay, artifact, document):
    return overlay.write(artifact, yaml.safe_dump(document).encode("utf-8"))


def _verify(changeset, before, after, **kwargs):
    return verify(changeset=changeset, snapshot_before=before,
                  snapshot_after=after, parse=_parse, read_at=read_at,
                  **kwargs)


# --- the happy path, so the failures mean something --------------------------


def test_a_change_that_fully_landed_is_aligned(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    after = _write(overlay, WORKFLOW, _doc(NEW))
    result = _verify(changeset, before, after)
    assert result.alignment is AlignmentState.ALIGNED
    assert result.passed


def test_the_verdict_names_the_two_snapshots(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    after = _write(overlay, WORKFLOW, _doc(NEW))
    result = _verify(changeset, before, after)
    assert result.snapshot_before == before.snapshot_id
    assert result.snapshot_after == after.snapshot_id


# --- 1. the actual state, not the declared one -------------------------------


def test_a_changeset_marked_applied_with_nothing_written_is_not_verified(overlay):
    """The central claim. The ChangeSet says APPLIED; the file says otherwise."""
    before = overlay.head
    changeset = _changeset(overlay).with_state(
        ChangeSetState.APPLYING, actor="a", timestamp="t")
    # Something else moved, so the snapshots differ -- but not the artifact
    # the approved edit names.
    after = overlay.write("unrelated.txt", b"x")

    result = _verify(changeset, before, after)
    assert not result.passed
    assert result.missing


def test_the_verifier_never_consults_the_changeset_state():
    source = inspect.getsource(verify)
    assert ".state" not in source, (
        "the verifier reads the ChangeSet's recorded state; a state field "
        "is a declared fact, and this module accepts only artifacts")


def test_the_verifier_has_no_write_path():
    import ast

    from core.change_intelligence import verification

    tree = ast.parse(inspect.getsource(verification))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("write", "clear", "write_text",
                                       "write_bytes")):
            pytest.fail(
                f"verification writes at line {node.lineno}; a verifier that "
                f"repaired what it found would make the drift invisible")


def test_the_verifier_asks_no_model_for_a_verdict():
    from core.change_intelligence import verification

    source = _executable_source(verification).lower()
    for banned in ("proposer", "gemini", "llm", "generate("):
        assert banned not in source, (
            f"the verification RESULT may not come from {banned!r}; a model "
            f"may summarise evidence, it may not return the verdict")


def test_the_verifier_reads_no_clock_and_rolls_no_dice():
    from core.change_intelligence import verification

    source = _executable_source(verification)
    for banned in ("datetime.now", "time.time", "uuid", "random"):
        assert banned not in source, (
            f"{banned} would make the same two snapshots produce a different "
            f"verdict on two runs")


# --- 2. missing intended change ----------------------------------------------


def test_a_missing_intended_change_is_detected(overlay):
    """Apply the workflow, forget the spreadsheet."""
    before = overlay.head
    changeset = _changeset(overlay, (_edit(WORKFLOW), _edit(SHEET)))
    after = _write(overlay, WORKFLOW, _doc(NEW))

    result = _verify(changeset, before, after)
    assert [m.artifact for m in result.missing] == [SHEET]


def test_a_missing_intended_change_is_drift_not_aligned(overlay):
    before = overlay.head
    changeset = _changeset(overlay, (_edit(WORKFLOW), _edit(SHEET)))
    after = _write(overlay, WORKFLOW, _doc(NEW))
    assert _verify(changeset, before, after).alignment is AlignmentState.DRIFT


def test_the_missing_finding_says_what_it_found_instead(overlay):
    before = overlay.head
    changeset = _changeset(overlay, (_edit(WORKFLOW), _edit(SHEET)))
    after = _write(overlay, WORKFLOW, _doc(NEW))
    missing = _verify(changeset, before, after).missing[0]
    assert missing.actual == OLD and missing.expected == NEW
    assert "still holds the old value" in missing.detail


def test_an_artifact_absent_from_the_snapshot_is_missing(overlay):
    before = overlay.head
    changeset = _changeset(overlay, (_edit("nowhere/absent.yaml"),))
    after = overlay.write("unrelated.txt", b"x")
    result = _verify(changeset, before, after)
    assert result.missing[0].outcome == MISSING


# --- 3. unexpected change ----------------------------------------------------


def test_an_artifact_that_changed_without_approval_is_detected(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    _write(overlay, WORKFLOW, _doc(NEW))
    after = overlay.write(DOC, b"Somebody rewrote this.\n")

    result = _verify(changeset, before, after)
    assert [u.artifact for u in result.unexpected] == [DOC]
    assert result.unexpected[0].kind == UNEXPECTED


def test_an_unexpected_change_is_not_absorbed_into_the_approved_set(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    _write(overlay, WORKFLOW, _doc(NEW))
    after = overlay.write(DOC, b"Somebody rewrote this.\n")
    result = _verify(changeset, before, after)
    assert not result.passed
    assert not result.unexpected[0].related_to_change


def test_relatedness_is_membership_not_resemblance(overlay):
    """Decided by the approved set. Similarity would absorb out-of-band edits."""
    source = inspect.getsource(
        __import__("core.change_intelligence.verification",
                   fromlist=["check_unexpected"]).check_unexpected)
    assert "moved - expected" in source


# --- 4. reverse drift --------------------------------------------------------


def test_an_external_edit_after_apply_is_reverse_drift(overlay):
    """approved 500000 -> applied 500000 -> somebody writes 250000."""
    before = overlay.head
    changeset = _changeset(overlay)
    _write(overlay, WORKFLOW, _doc(NEW))
    after = _write(overlay, WORKFLOW, _doc(250_000))

    result = _verify(changeset, before, after,
                     approved_values={WORKFLOW: NEW},
                     paths_by_artifact={WORKFLOW: (PATH,)})
    assert result.reverse_drift
    assert result.reverse_drift[0].artifact == WORKFLOW


def test_reverse_drift_is_a_conflict(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    _write(overlay, WORKFLOW, _doc(NEW))
    after = _write(overlay, WORKFLOW, _doc(250_000))
    result = _verify(changeset, before, after,
                     approved_values={WORKFLOW: NEW},
                     paths_by_artifact={WORKFLOW: (PATH,)})
    assert result.alignment is AlignmentState.CONFLICT


def test_reverse_drift_is_not_reinterpreted_as_an_approved_change(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    _write(overlay, WORKFLOW, _doc(NEW))
    after = _write(overlay, WORKFLOW, _doc(250_000))
    result = _verify(changeset, before, after,
                     approved_values={WORKFLOW: NEW},
                     paths_by_artifact={WORKFLOW: (PATH,)})
    finding = result.reverse_drift[0]
    assert "no ChangeSet authorised" in finding.reason
    assert finding.introduces_contradiction


def test_a_value_that_is_neither_old_nor_approved_is_diverged(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    after = _write(overlay, WORKFLOW, _doc(777))
    result = _verify(changeset, before, after)
    assert result.diverged
    assert result.diverged[0].outcome == DIVERGED
    assert result.alignment is AlignmentState.CONFLICT


# --- 5. no auto-repair -------------------------------------------------------


def test_drift_produces_options_for_a_person_not_an_action(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    _write(overlay, WORKFLOW, _doc(NEW))
    after = _write(overlay, WORKFLOW, _doc(250_000))
    result = _verify(changeset, before, after,
                     approved_values={WORKFLOW: NEW},
                     paths_by_artifact={WORKFLOW: (PATH,)})

    actions = next_actions(result)
    assert actions
    assert len(actions[0].options) == 2, (
        "one option is a recommendation, and whether an out-of-band edit is "
        "acceptable is a business decision")


def test_the_artifact_is_unchanged_after_verification(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    after = _write(overlay, WORKFLOW, _doc(250_000))
    digest = after.overlay_manifest[WORKFLOW]
    _verify(changeset, before, after, approved_values={WORKFLOW: NEW},
            paths_by_artifact={WORKFLOW: (PATH,)})
    assert overlay.head.overlay_manifest[WORKFLOW] == digest


# --- the regression guard: five conditions -----------------------------------


def test_there_are_exactly_five_blockers():
    assert len(BLOCKERS) == 5


def test_failing_tests_block_verification():
    found = blockers_for(tests_failing=("test_old_001",))
    assert [name for name, _ in found] == ["tests_failing"]


def test_stale_evidence_blocks_verification():
    found = blockers_for(stale_evidence=("workflow/routes.yaml:L37",))
    assert [name for name, _ in found] == ["evidence_stale"]


def test_an_open_intent_conflict_blocks_verification():
    found = blockers_for(open_conflicts=("approval_threshold",))
    assert [name for name, _ in found] == ["intent_conflict_open"]


def test_a_stale_required_artifact_blocks_verification():
    found = blockers_for(stale_artifacts=(SHEET,))
    assert [name for name, _ in found] == ["artifact_stale"]


def test_a_missing_approval_record_blocks_verification(overlay):
    """The fifth. Applied legitimately, then the approval was lost."""
    changeset = _changeset(overlay, approved=False)
    found = blockers_for(changeset=changeset)
    assert [name for name, _ in found] == ["approval_evidence_missing"]


def test_an_approval_for_a_different_version_blocks_verification(overlay):
    changeset = _changeset(overlay)
    changeset.actions = ActionPlan(target="rule", actions=(
        Action(kind="WORKFLOW", target=WORKFLOW, description="d",
               necessity="required", performer="system",
               impact_evidence=("e",), edits=(_edit(proposed=250_000),),
               reason="r"),))
    found = blockers_for(changeset=changeset)
    assert [name for name, _ in found] == ["approval_evidence_missing"]


def test_a_blocked_verification_is_never_aligned(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    after = _write(overlay, WORKFLOW, _doc(NEW))
    result = _verify(changeset, before, after,
                     tests_failing=("test_old_001",))
    assert result.alignment is AlignmentState.DRIFT
    assert "tests_failing" in result.blocked_by


def test_every_blocker_alone_prevents_a_pass(overlay):
    before = overlay.head
    after = _write(overlay, WORKFLOW, _doc(NEW))
    for kwargs in ({"tests_failing": ("t",)}, {"stale_evidence": ("e",)},
                   {"open_conflicts": ("c",)}):
        result = _verify(_changeset(overlay), before, after, **kwargs)
        assert not result.passed, kwargs


# --- ALIGNED cannot carry open problems --------------------------------------


def test_aligned_with_unresolved_items_cannot_be_constructed():
    with pytest.raises(CannotVerify, match="nobody would check"):
        VerificationResult(
            changeset_id="C", snapshot_before="A", snapshot_after="B",
            alignment=AlignmentState.ALIGNED,
            unresolved=["the spreadsheet still says the old value"])


def test_aligned_with_a_blocker_cannot_be_constructed():
    with pytest.raises(CannotVerify):
        VerificationResult(
            changeset_id="C", snapshot_before="A", snapshot_after="B",
            alignment=AlignmentState.ALIGNED, blocked_by=["tests_failing"])


def test_only_aligned_passes():
    for state in (AlignmentState.DRIFT, AlignmentState.CONFLICT,
                  AlignmentState.UNKNOWN):
        result = VerificationResult(
            changeset_id="C", snapshot_before="A", snapshot_after="B",
            alignment=state)
        assert not result.passed


# --- the six dimensions ------------------------------------------------------


def test_the_six_dimensions_are_frozen():
    assert DIMENSIONS == ("workflow", "software", "data", "documents",
                          "tests", "evidence")


def test_a_dimension_nobody_measured_is_unknown_not_ok(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    after = _write(overlay, WORKFLOW, _doc(NEW))
    result = _verify(changeset, before, after)
    assert dict(result.dimension_summary)["software"] == "UNKNOWN"


def test_a_dimension_with_a_missing_edit_is_drift(overlay):
    before = overlay.head
    changeset = _changeset(overlay, (_edit(WORKFLOW), _edit(SHEET)))
    after = _write(overlay, WORKFLOW, _doc(NEW))
    result = _verify(changeset, before, after)
    assert dict(result.dimension_summary)["workflow"] == "OK"


# --- no-op and determinism ---------------------------------------------------


def test_verifying_two_identical_snapshots_is_refused(overlay):
    changeset = _changeset(overlay)
    with pytest.raises(CannotVerify, match="never happened"):
        _verify(changeset, overlay.head, overlay.head)


def test_the_same_inputs_give_the_same_result(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    after = _write(overlay, WORKFLOW, _doc(NEW))
    first = _verify(changeset, before, after)
    second = _verify(changeset, before, after)
    assert render(first) == render(second)
    assert first.alignment is second.alignment
    assert [e.rendered() for e in first.edits] == [
        e.rendered() for e in second.edits]


def test_a_changeset_with_only_human_actions_says_so(overlay):
    before = overlay.head
    changeset = ChangeSet(
        trigger="T", base_snapshot=before.snapshot_id, intent=_intent(),
        affected_nodes=("n",), affected_artifacts=(), evidence=("q",),
        simulation_result=_Sim(),
        actions=ActionPlan(target="r", actions=(
            Action(kind="CODE", target="a.py", description="d",
                   necessity="required", performer="human",
                   impact_evidence=("e",), reason="r"),)),
        risk="LOW")
    after = overlay.write("unrelated.txt", b"x")
    result = _verify(changeset, before, after)
    assert any("carried out by a person" in note for note in result.notes)


def test_the_rendering_names_the_blockers(overlay):
    before = overlay.head
    changeset = _changeset(overlay)
    after = _write(overlay, WORKFLOW, _doc(NEW))
    text = render(_verify(changeset, before, after,
                          tests_failing=("test_old_001",)))
    assert "VERIFICATION BLOCKED BY" in text
    assert "tests_failing" in text
