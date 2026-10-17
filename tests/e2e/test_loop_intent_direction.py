# -*- coding: utf-8 -*-
"""Gate G8, direction 2 -- a person asks, and the system proves it landed.

    intent -> approve -> apply -> snapshot N+1 -> verify -> VERIFIED

Run against the real seed from a freshly built baseline, so nothing here
depends on state left behind by another test. The thing being demonstrated
is not that apply works -- P7 showed that -- but that the system goes back
and *checks*, and that the check is capable of saying no.

The last two tests are the ones that make the rest worth reading: apply
part of the change, and the loop must report DRIFT rather than VERIFIED.
A system that always reports success is a system that checks nothing.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from core.change_intelligence.action_plan import Action, ActionPlan
from core.change_intelligence.apply_gate import APPROVE, apply_changeset, decide
from core.change_intelligence.changeset import ChangeSet
from core.change_intelligence.lineage import VerifiedLineage
from core.change_intelligence.orchestrator import (
    AWAITING_HUMAN,
    Orchestrator,
    REPORTED,
)
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.shadow_change import baseline_fingerprint
from core.change_intelligence.states import AlignmentState, ChangeSetState
from core.change_intelligence.structured_diff import (
    StructuredEdit,
    read_at,
    validate_workflow,
)
from core.change_intelligence.verification import UNREADABLE
from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
WORKFLOW = "workflow/approval_routes.yaml"
PATH = "approval_routes[3].condition.value"
SESSION = "SESSION-G8-INTENT"


def _parse(artifact, raw):
    """The adapter layer, injected.

    A spreadsheet is parsed into `{"Sheet!Cell": value}` so that the same
    locator grammar the structured edit uses resolves against it. The
    verifier knows nothing about either format -- that is the point of
    passing this in.
    """
    if artifact.endswith(".xlsx"):
        cells = {}
        for record in read_xlsx(raw, artifact, "S"):
            payload = record.payload
            cells[f"{payload['sheet']}!{payload['cell']}"] = payload["value"]
        return cells
    return yaml.safe_load(raw)


def _serialise(artifact, document):
    return yaml.safe_dump(document, sort_keys=False,
                          allow_unicode=True).encode("utf-8")


@pytest.fixture
def world():
    """A fresh baseline every time. No leftover state, by construction."""
    artifacts = build_baseline(SEED)["artifacts"]
    baseline = InMemoryBaseline.from_artifacts(artifacts)
    overlay = InMemoryOverlay(baseline, SESSION)
    lineage = VerifiedLineage(session_id=SESSION,
                              baseline_id=baseline.baseline_id)
    return artifacts, baseline, overlay, lineage


@pytest.fixture
def current(world):
    _, _, overlay, _ = world
    return read_at(yaml.safe_load(overlay.read(WORKFLOW)), PATH)


def _edit(current, proposed, artifact=WORKFLOW, path=PATH):
    return StructuredEdit(
        artifact=artifact, path=path, expected_current=current,
        proposed=proposed, unit="JPY",
        reason="an approved intent lowered the approval threshold",
        evidence=f"{artifact}:{path}")


def _approved_changeset(overlay, edits):
    by_artifact = {}
    for edit in edits:
        by_artifact.setdefault(edit.artifact, []).append(edit)
    actions = tuple(
        Action(kind="WORKFLOW", target=artifact,
               description=f"set the value in {artifact}",
               necessity="required", performer="system",
               impact_evidence=(f"{artifact}:{PATH}",), edits=tuple(group),
               reason="carries the value the system reads")
        for artifact, group in sorted(by_artifact.items()))

    changeset = ChangeSet(
        trigger="INT-001", base_snapshot=overlay.head.snapshot_id,
        intent=_intent(), affected_nodes=("n",),
        affected_artifacts=tuple(sorted(by_artifact)),
        evidence=(f"{WORKFLOW}:{PATH}",), simulation_result=_Sim(),
        actions=ActionPlan(target="approval_threshold", actions=actions),
        risk="MEDIUM", session_id=SESSION)
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="ops.lead",
                                    timestamp="2026-09-20T09:00:00Z")
    approved, _ = decide(reviewed, actor="cfo",
                         timestamp="2026-09-20T09:30:00Z",
                         decision=APPROVE, reason="impact reviewed")
    return approved


def _intent():
    class I:
        intent_id, version = "INT-001", 1
        target = "approval_threshold"

    return I()


class _Sim:
    def new_inconsistencies(self):
        return []

    def inherited_inconsistencies(self):
        return []


def _apply(changeset, overlay):
    return apply_changeset(
        changeset, overlay=overlay,
        documents={WORKFLOW: yaml.safe_load(overlay.read(WORKFLOW))},
        actor="ops.lead", timestamp="2026-09-20T10:00:00Z",
        serialise=_serialise, validate=validate_workflow)


# --- the whole direction, end to end -----------------------------------------


def test_the_loop_closes_from_intent_to_verified(world, current):
    _, _, overlay, lineage = world
    before = overlay.head

    changeset = _approved_changeset(overlay, (_edit(current, current // 2),))
    applied, _ = _apply(changeset, overlay)
    after = overlay.head

    orchestrator = Orchestrator(session_id=SESSION, lineage=lineage)
    turn = orchestrator.run_verification(
        changeset=applied, snapshot_before=before, snapshot_after=after,
        parse=_parse, read_at=read_at, actor="qa.lead",
        timestamp="2026-09-20T10:05:00Z")

    assert turn.result.alignment is AlignmentState.ALIGNED
    assert turn.status == REPORTED
    assert not turn.needs_a_person


def test_apply_makes_a_new_snapshot_whose_parent_is_the_old_one(world, current):
    _, _, overlay, _ = world
    before = overlay.head
    changeset = _approved_changeset(overlay, (_edit(current, current // 2),))
    _apply(changeset, overlay)
    after = overlay.head

    assert after.snapshot_id != before.snapshot_id
    assert after.parent_snapshot_id == before.snapshot_id


def test_the_old_snapshot_is_still_readable_after_apply(world, current):
    _, _, overlay, _ = world
    before = overlay.head
    changeset = _approved_changeset(overlay, (_edit(current, current // 2),))
    _apply(changeset, overlay)

    assert read_at(yaml.safe_load(before.read(WORKFLOW)), PATH) == current


def test_the_verified_state_advances_by_exactly_one(world, current):
    _, _, overlay, lineage = world
    before = overlay.head
    changeset = _approved_changeset(overlay, (_edit(current, current // 2),))
    applied, _ = _apply(changeset, overlay)

    assert lineage.version == 0, "apply alone must not advance the version"
    orchestrator = Orchestrator(session_id=SESSION, lineage=lineage)
    orchestrator.run_verification(
        changeset=applied, snapshot_before=before, snapshot_after=overlay.head,
        parse=_parse, read_at=read_at, actor="qa", timestamp="t")
    assert lineage.version == 1


def test_verifying_twice_does_not_advance_twice(world, current):
    _, _, overlay, lineage = world
    before = overlay.head
    changeset = _approved_changeset(overlay, (_edit(current, current // 2),))
    applied, _ = _apply(changeset, overlay)

    orchestrator = Orchestrator(session_id=SESSION, lineage=lineage)
    for _ in range(3):
        orchestrator.run_verification(
            changeset=applied, snapshot_before=before,
            snapshot_after=overlay.head, parse=_parse, read_at=read_at,
            actor="qa", timestamp="t")
    assert lineage.version == 1


def test_the_lineage_entry_points_at_the_snapshot_that_was_verified(
        world, current):
    _, _, overlay, lineage = world
    before = overlay.head
    changeset = _approved_changeset(overlay, (_edit(current, current // 2),))
    applied, _ = _apply(changeset, overlay)
    after = overlay.head

    Orchestrator(session_id=SESSION, lineage=lineage).run_verification(
        changeset=applied, snapshot_before=before, snapshot_after=after,
        parse=_parse, read_at=read_at, actor="qa", timestamp="t")

    entry = lineage.at(1)
    assert entry.snapshot_id == after.snapshot_id
    assert entry.parent_snapshot_id == before.snapshot_id
    assert entry.changeset_id == applied.changeset_id


def test_the_baseline_never_moves_through_the_whole_loop(world, current):
    artifacts, baseline, overlay, lineage = world
    fingerprint = baseline_fingerprint(artifacts=artifacts)

    changeset = _approved_changeset(overlay, (_edit(current, current // 2),))
    before = overlay.head
    applied, _ = _apply(changeset, overlay)
    Orchestrator(session_id=SESSION, lineage=lineage).run_verification(
        changeset=applied, snapshot_before=before,
        snapshot_after=overlay.head, parse=_parse, read_at=read_at,
        actor="qa", timestamp="t")

    after = baseline_fingerprint(
        artifacts={name: baseline.read(name) for name in baseline.names()})
    assert after == fingerprint


def test_reset_still_works_after_the_whole_loop(world, current):
    _, baseline, overlay, lineage = world
    changeset = _approved_changeset(overlay, (_edit(current, current // 2),))
    _apply(changeset, overlay)
    overlay.clear()
    assert overlay.read(WORKFLOW) == baseline.read(WORKFLOW)


# --- the half-applied change: the test that makes the rest mean something ----


def test_applying_part_of_the_change_reports_drift_not_verified(world, current):
    """Change the workflow, leave the spreadsheet. DRIFT, not ALIGNED.

    The distinguishing test of this phase. A reconciliation that marks
    success is not a reconciliation, and the only way to tell the two apart
    is to give it a case where the honest answer is no.
    """
    _, _, overlay, lineage = world
    before = overlay.head

    # A quarter, not a half: the seed's spreadsheet already states half,
    # and an edit whose target value is already present would verify as
    # APPLIED -- correctly, and uselessly for this test.
    target = current // 4
    spreadsheet_edit = StructuredEdit(
        artifact="data/approval_matrix.xlsx", path="Thresholds!C2",
        expected_current=current // 2, proposed=target,
        reason="the same approved intent", evidence="matrix:Thresholds!C2")
    changeset = _approved_changeset(
        overlay, (_edit(current, target), spreadsheet_edit))

    # Apply only the workflow half, by hand -- this is what a partial apply
    # looks like from the verifier's side.
    document = yaml.safe_load(overlay.read(WORKFLOW))
    document["approval_routes"][3]["condition"]["value"] = target
    after = overlay.write(WORKFLOW, _serialise(WORKFLOW, document))

    turn = Orchestrator(session_id=SESSION, lineage=lineage).run_verification(
        changeset=changeset, snapshot_before=before, snapshot_after=after,
        parse=_parse, read_at=read_at, actor="qa", timestamp="t")

    assert turn.result.alignment is AlignmentState.DRIFT
    assert not turn.result.passed
    assert lineage.version == 0, "a drifted state must not advance the version"


def _half_applied(world, current):
    """Approve two artifacts, write only one. Returns the verification turn."""
    _, _, overlay, lineage = world
    before = overlay.head
    target = current // 4
    changeset = _approved_changeset(overlay, (
        _edit(current, target),
        StructuredEdit(artifact="data/approval_matrix.xlsx",
                       path="Thresholds!C2", expected_current=current // 2,
                       proposed=target, reason="the same approved intent",
                       evidence="matrix:Thresholds!C2")))
    document = yaml.safe_load(overlay.read(WORKFLOW))
    document["approval_routes"][3]["condition"]["value"] = target
    after = overlay.write(WORKFLOW, _serialise(WORKFLOW, document))

    turn = Orchestrator(session_id=SESSION, lineage=lineage).run_verification(
        changeset=changeset, snapshot_before=before, snapshot_after=after,
        parse=_parse, read_at=read_at, actor="qa", timestamp="t")
    return turn, after, lineage


def test_the_half_applied_change_names_the_artifact_that_was_missed(
        world, current):
    turn, _, _ = _half_applied(world, current)
    missed = {m.artifact for m in turn.result.missing}
    assert missed == {"data/approval_matrix.xlsx"}


def test_the_failed_verification_is_recorded_with_its_reason(world, current):
    _, after, lineage = _half_applied(world, current)
    failures = lineage.failures_for(after.snapshot_id)
    assert failures and "did not land" in failures[0].reason


def test_the_drift_gives_a_person_something_to_decide(world, current):
    turn, _, _ = _half_applied(world, current)
    assert turn.status == AWAITING_HUMAN
    assert turn.next_actions
    assert all(len(action.options) >= 2 for action in turn.next_actions)


def test_an_artifact_with_no_reader_is_unreadable_not_missing(world, current):
    """"The old value is still there" and "nobody can tell" differ.

    Only the first tells a person what to do, and folding them together
    would have the report name a value nothing observed.
    """
    _, _, overlay, lineage = world
    before = overlay.head
    changeset = _approved_changeset(overlay, (
        StructuredEdit(artifact="data/approval_matrix.xlsx",
                       path="Thresholds!C2", expected_current=current // 2,
                       proposed=current // 4, reason="r", evidence="e"),))
    after = overlay.write("unrelated.txt", b"x")

    turn = Orchestrator(session_id=SESSION, lineage=lineage).run_verification(
        changeset=changeset, snapshot_before=before, snapshot_after=after,
        parse=lambda a, raw: yaml.safe_load(raw), read_at=read_at,
        actor="qa", timestamp="t")

    outcomes = {o.outcome for o in turn.result.edits}
    assert outcomes == {UNREADABLE}
    assert turn.result.alignment is AlignmentState.UNKNOWN
    assert not turn.result.passed
    assert lineage.version == 0


# --- the event that closes the loop ------------------------------------------


def test_verification_emits_the_event_that_closes_the_loop(world, current):
    _, _, overlay, lineage = world
    before = overlay.head
    changeset = _approved_changeset(overlay, (_edit(current, current // 2),))
    applied, _ = _apply(changeset, overlay)

    turn = Orchestrator(session_id=SESSION, lineage=lineage).run_verification(
        changeset=applied, snapshot_before=before,
        snapshot_after=overlay.head, parse=_parse, read_at=read_at,
        actor="qa", timestamp="2026-09-20T10:05:00Z")

    assert turn.emitted
    event = turn.emitted[0]
    assert event.event_type == "VerificationCompleted"
    assert event.is_loop_triggering
    assert event.payload["alignment"] == "ALIGNED"


def test_the_whole_direction_is_deterministic(current):
    """Two independent runs from a fresh baseline agree on everything."""
    def run():
        artifacts = build_baseline(SEED)["artifacts"]
        baseline = InMemoryBaseline.from_artifacts(artifacts)
        overlay = InMemoryOverlay(baseline, SESSION)
        lineage = VerifiedLineage(session_id=SESSION,
                                  baseline_id=baseline.baseline_id)
        before = overlay.head
        value = read_at(yaml.safe_load(overlay.read(WORKFLOW)), PATH)
        changeset = _approved_changeset(overlay, (_edit(value, value // 2),))
        applied, _ = _apply(changeset, overlay)
        turn = Orchestrator(session_id=SESSION, lineage=lineage).run_verification(
            changeset=applied, snapshot_before=before,
            snapshot_after=overlay.head, parse=_parse, read_at=read_at,
            actor="qa", timestamp="t")
        return (applied.changeset_id, overlay.head.snapshot_id,
                turn.result.alignment, lineage.version)

    assert run() == run()
