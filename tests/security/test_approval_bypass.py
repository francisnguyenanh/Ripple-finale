# -*- coding: utf-8 -*-
"""Gate G7's central claim: nothing is applied that nobody approved.

Every test here tries to get a change applied without a valid signature,
and every one must be refused by a RAISE. A refusal that returns False, or
logs a warning, or applies-but-marks-it, is a boundary that has already been
crossed by the time anybody reads the log.

The companion file `tests/mutation/test_approval_mutation.py` answers the
question this file cannot: is the check real, or is it an `if` statement
nobody has ever seen fail?
"""

from __future__ import annotations

import pytest

from core.change_intelligence.action_plan import Action, ActionPlan
from core.change_intelligence.apply_gate import (
    APPROVAL_FIELDS,
    APPROVE,
    ApprovalRecord,
    ApprovalRequired,
    IncompleteApproval,
    NotVerified,
    REJECT,
    REQUEST_CHANGES,
    StaleApproval,
    apply_changeset,
    decide,
    mark_verified,
)
from core.change_intelligence.changeset import ChangeSet
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.states import ChangeSetState
from core.change_intelligence.structured_diff import StaleDiff, StructuredEdit

ARTIFACT = "workflow/routes.yaml"


def _document():
    return {"approval_routes": [
        {"id": "R", "condition": {"field": "amount", "op": ">=",
                                  "value": 1000}}]}


def _edit(current=1000, proposed=500):
    return StructuredEdit(
        artifact=ARTIFACT, path="approval_routes[0].condition.value",
        expected_current=current, proposed=proposed,
        reason="an approved intent lowered this",
        evidence=f"{ARTIFACT}:approval_routes[0]")


def _plan(edits=None):
    return ActionPlan(target="rule", actions=(Action(
        kind="WORKFLOW", target=ARTIFACT, description="set the value",
        necessity="required", performer="system",
        impact_evidence=(f"{ARTIFACT}:approval_routes[0]",),
        edits=tuple(edits if edits is not None else (_edit(),)),
        reason="carries the value the system reads"),))


class _Sim:
    cases: list = []
    total = 0
    changed: list = []

    def new_inconsistencies(self):
        return []

    def inherited_inconsistencies(self):
        return []


def _overlay(session="SESSION-SEC"):
    import json

    baseline = InMemoryBaseline.from_artifacts(
        {ARTIFACT: json.dumps(_document()).encode("utf-8")})
    return InMemoryOverlay(baseline, session)


@pytest.fixture
def world():
    overlay = _overlay()
    changeset = ChangeSet(
        trigger="INTENT-1", base_snapshot=overlay.head.snapshot_id,
        intent=_intent(), affected_nodes=(_node(),),
        affected_artifacts=(ARTIFACT,), evidence=("quote",),
        simulation_result=_Sim(), actions=_plan(), risk=_risk(),
        session_id="SESSION-SEC", created_by="analyst",
        created_at="2026-09-20T09:00:00Z")
    return overlay, changeset


def _intent():
    class I:
        intent_id = "INT-1"
        version = 1
        target = "rule"
        desired_value = 500
        reason = "audit finding"
        requested_by = "ops.lead"
        requested_at = "2026-09-19T00:00:00Z"

    return I()


def _node():
    from core.change_intelligence.impact import ImpactNode

    return ImpactNode(identifier=ARTIFACT, kind="workflow_config",
                      tier="DIRECT", reason="carries the value",
                      evidence=f"{ARTIFACT}:approval_routes[0]")


def _risk():
    from core.change_intelligence.risk import assess

    return assess()


def _apply(changeset, overlay, **kwargs):
    return apply_changeset(
        changeset, overlay=overlay, documents={ARTIFACT: _document()},
        actor="ops.lead", timestamp="2026-09-20T10:00:00Z", **kwargs)


# --- the bypass attempts -----------------------------------------------------


def test_applying_a_proposed_changeset_raises(world):
    """The simplest bypass: call the executor and skip the review."""
    overlay, changeset = world
    assert changeset.state is ChangeSetState.PROPOSED
    with pytest.raises(ApprovalRequired, match="no approval"):
        _apply(changeset, overlay)


def test_applying_a_changeset_under_review_raises(world):
    overlay, changeset = world
    in_review = changeset.with_state(
        ChangeSetState.REVIEW, actor="a", timestamp="t")
    with pytest.raises(ApprovalRequired):
        _apply(in_review, overlay)


def test_applying_a_rejected_changeset_raises(world):
    overlay, changeset = world
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="a",
                                    timestamp="t")
    rejected, _ = decide(reviewed, actor="cfo", timestamp="t",
                         decision=REJECT, reason="not now")
    with pytest.raises(ApprovalRequired, match="was not approved"):
        _apply(rejected, overlay)


def test_request_changes_is_not_an_approval(world):
    overlay, changeset = world
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="a",
                                    timestamp="t")
    asked, _ = decide(reviewed, actor="cfo", timestamp="t",
                      decision=REQUEST_CHANGES, reason="split it up")
    with pytest.raises(ApprovalRequired):
        _apply(asked, overlay)


def test_an_approval_record_forged_onto_a_proposed_changeset_raises(world):
    """State and record must agree. Disagreement is resolved by refusing."""
    overlay, changeset = world
    changeset.approval = ApprovalRecord(
        actor="attacker", timestamp="t", changeset_id=changeset.changeset_id,
        proposal_version=changeset.proposal_version, decision=APPROVE,
        reason="looks fine")
    with pytest.raises(ApprovalRequired, match="the record and the state disagree"):
        _apply(changeset, overlay)


def test_the_refusal_raises_rather_than_returning_a_value(world):
    """A False nobody checks is not a gate."""
    overlay, changeset = world
    try:
        _apply(changeset, overlay)
    except ApprovalRequired:
        return
    pytest.fail("apply returned instead of raising; a caller may ignore a "
                "return value and cannot ignore an exception")


# --- the happy path, so the refusals mean something --------------------------


def _approved(world):
    overlay, changeset = world
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="analyst",
                                    timestamp="t")
    approved, record = decide(reviewed, actor="cfo",
                              timestamp="2026-09-20T09:30:00Z",
                              decision=APPROVE, reason="evidence is clear")
    return overlay, approved, record


def test_an_approved_changeset_applies(world):
    overlay, approved, _ = _approved(world)
    applied, result = _apply(approved, overlay)
    assert applied.state is ChangeSetState.APPLIED
    assert result.written == (ARTIFACT,)


def test_applying_writes_the_proposed_value(world):
    import yaml

    overlay, approved, _ = _approved(world)
    _apply(approved, overlay)
    # Serialised as YAML because the artifact is a .yaml: the bytes written
    # are in the artifact's own format, not the engine's convenience format.
    after = yaml.safe_load(overlay.read(ARTIFACT))
    assert after["approval_routes"][0]["condition"]["value"] == 500


# --- stale approval ----------------------------------------------------------


def test_approving_v1_does_not_authorise_v2(world):
    """Duyet v1 -> sua thanh v2 -> apply -> tu choi."""
    overlay, approved, record = _approved(world)
    v1 = approved.proposal_version

    approved.actions = _plan([_edit(proposed=250)])
    assert approved.proposal_version != v1, (
        "editing the proposal did not change its version; then 'approved' "
        "is a flag on an object that can still be rewritten")

    with pytest.raises(StaleApproval, match="is not the change that was read"):
        _apply(approved, overlay)


def test_the_stale_message_names_both_versions(world):
    overlay, approved, _ = _approved(world)
    v1 = approved.proposal_version
    approved.actions = _plan([_edit(proposed=250)])
    with pytest.raises(StaleApproval) as caught:
        _apply(approved, overlay)
    assert v1 in str(caught.value)
    assert approved.proposal_version in str(caught.value)


def test_moving_state_does_not_change_the_proposal_version(world):
    """Approval must survive the transition it causes."""
    overlay, changeset = world
    before = changeset.proposal_version
    moved = changeset.with_state(ChangeSetState.REVIEW, actor="a",
                                 timestamp="t")
    assert moved.proposal_version == before


# --- stale snapshot ----------------------------------------------------------


def test_a_snapshot_that_moved_after_approval_blocks_the_apply(world):
    overlay, approved, _ = _approved(world)
    overlay.write("unrelated.txt", b"somebody else changed something")
    with pytest.raises(StaleDiff, match="a world that has moved"):
        _apply(approved, overlay)


def test_an_artifact_named_by_an_approved_edit_but_not_supplied_raises(world):
    """Applying PART of an approved change is worse than applying none."""
    overlay, approved, _ = _approved(world)
    with pytest.raises(StaleDiff, match="applying part of an approved change"):
        apply_changeset(approved, overlay=overlay, documents={},
                        actor="ops.lead", timestamp="t")


# --- the six fields ----------------------------------------------------------


@pytest.mark.parametrize("field", APPROVAL_FIELDS)
def test_an_approval_missing_any_one_field_is_refused(field):
    complete = dict(actor="cfo", timestamp="t", changeset_id="CHG-1",
                    proposal_version="v", decision=APPROVE, reason="why")
    complete[field] = ""
    with pytest.raises(IncompleteApproval, match=field):
        ApprovalRecord(**complete)


def test_there_are_exactly_six_approval_fields():
    assert len(APPROVAL_FIELDS) == 6


def test_a_decision_outside_the_three_is_refused():
    with pytest.raises(IncompleteApproval):
        ApprovalRecord(actor="a", timestamp="t", changeset_id="c",
                       proposal_version="v", decision="maybe", reason="r")


# --- I14: apply must be followed by verification -----------------------------


def test_applied_is_not_a_terminal_state(world):
    from core.change_intelligence.states import TERMINAL_STATES

    assert ChangeSetState.APPLIED not in TERMINAL_STATES


def test_marking_verified_without_a_report_raises(world):
    overlay, approved, _ = _approved(world)
    applied, _ = _apply(approved, overlay)
    with pytest.raises(NotVerified, match="nothing checked it"):
        mark_verified(applied, actor="qa", timestamp="t", report=None)


def test_apply_emits_the_event_that_triggers_verification(world):
    overlay, approved, _ = _approved(world)
    _, result = _apply(approved, overlay)
    assert result.requires_verification
    assert "ChangeApplied" in result.events


def test_verification_completes_the_chain(world):
    overlay, approved, _ = _approved(world)
    applied, _ = _apply(approved, overlay)
    verified = mark_verified(applied, actor="qa", timestamp="t",
                             report={"checked": ARTIFACT})
    assert verified.state is ChangeSetState.VERIFIED
    assert verified.verification


# --- human actions are not silently claimed ----------------------------------


def test_an_action_a_person_performs_is_not_written_by_apply(world):
    overlay, changeset = world
    changeset.actions = ActionPlan(target="rule", actions=(
        _plan().actions[0],
        Action(kind="CODE", target="app/handler.py",
               description="read the value from configuration",
               necessity="required", performer="human",
               impact_evidence=("app/handler.py:12",),
               reason="hardcodes the value"),))
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="a",
                                    timestamp="t")
    approved, _ = decide(reviewed, actor="cfo", timestamp="t",
                         decision=APPROVE, reason="ok")
    _, result = _apply(approved, overlay)

    assert result.written == (ARTIFACT,)
    assert len(result.human_actions) == 1
    assert result.notes and "carried out by a person" in result.notes[0], (
        "the apply result must say what it did NOT do; silence there reads "
        "as 'all of it was done'")


# --- two reviewers at once ---------------------------------------------------


def test_two_approvals_of_the_same_proposal_do_not_double_apply(world):
    """Concurrent approval: one write, because apply is idempotent on value.

    The second apply finds the value already at the proposed figure and is
    refused as stale, which is the correct outcome: the world it was
    computed against is not the world any more.
    """
    overlay, approved, _ = _approved(world)
    _apply(approved, overlay)
    with pytest.raises(StaleDiff):
        _apply(approved, overlay)
