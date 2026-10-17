# -*- coding: utf-8 -*-
"""N1 -- round-trip tests for the DTO/codec that lets session.work hold
real domain objects instead of hand-built dicts.

`json.dumps` cannot round-trip a `ChangeSet` (enums, nested frozen
dataclasses, tuples). `app/api/codec.py` can. Every test here builds a real
object with the real constructor, encodes it, decodes it, and asserts
equality -- not "no exception", equality, because a codec that silently
drops a field passes "no exception" every time.
"""

from __future__ import annotations

import pytest

from app.api.codec import decode_record, encode_record
from core.change_intelligence.action_plan import Action, ActionPlan
from core.change_intelligence.apply_gate import APPROVE, ApprovalRecord, decide
from core.change_intelligence.changeset import ChangeSet
from core.change_intelligence.impact import ImpactNode, ImpactResult
from core.change_intelligence.risk import assess
from core.change_intelligence.shadow_change import ShadowChange
from core.change_intelligence.states import ChangeSetState
from core.change_intelligence.structured_diff import StructuredEdit
from core.change_intelligence.verification import verify
from core.change_intelligence.states import AlignmentState


def test_a_shadow_change_round_trips():
    shadow = ShadowChange(
        shadow_id="SESSION-1:shadow:abc", base_snapshot="snap-1",
        target="approval_threshold", current_value=1_000_000,
        proposed_value=500_000, session_id="SESSION-1",
        created_by="analyst", created_at="2026-09-20T09:00:00Z")
    record = encode_record("shadow_change", shadow)
    assert record["schema_version"] == 1
    assert record["kind"] == "shadow_change"

    back = decode_record(record, expected_kind="shadow_change")
    assert back == shadow
    assert back.fingerprint() == shadow.fingerprint()


def test_an_impact_result_round_trips():
    result = ImpactResult(
        target="approval_threshold",
        nodes=(ImpactNode(identifier="workflow/routes.yaml",
                          kind="workflow_config", tier="DIRECT",
                          reason="carries the value",
                          evidence="workflow/routes.yaml:approval_routes[0]"),),
        notes=["a note"])
    record = encode_record("impact", result)
    back = decode_record(record, expected_kind="impact")
    assert back == result
    assert back.artifacts == result.artifacts
    assert isinstance(back.nodes, tuple)


def test_a_risk_assessment_round_trips():
    assessment = assess()
    record = encode_record("risk", assessment)
    back = decode_record(record, expected_kind="risk")
    assert back == assessment
    assert back.level == assessment.level


def test_a_changeset_round_trips_including_its_state_enum():
    changeset = ChangeSet(
        trigger="INTENT-1", base_snapshot="snap-1",
        affected_artifacts=("workflow/routes.yaml",), evidence=("quote",),
        session_id="SESSION-1", created_by="analyst",
        created_at="2026-09-20T09:00:00Z")
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="analyst",
                                    timestamp="t")

    record = encode_record("changeset", reviewed)
    back = decode_record(record, expected_kind="changeset")

    assert back.changeset_id == reviewed.changeset_id
    assert back.proposal_version == reviewed.proposal_version
    assert back.state is ChangeSetState.REVIEW
    assert isinstance(back.state, ChangeSetState)
    assert back.history == reviewed.history


def test_a_changeset_with_an_approval_round_trips():
    changeset = ChangeSet(
        trigger="INTENT-1", base_snapshot="snap-1",
        session_id="SESSION-1", created_by="analyst", created_at="t")
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="a", timestamp="t")
    approved, approval = decide(reviewed, actor="cfo", timestamp="t",
                                decision=APPROVE, reason="evidence is clear")

    record = encode_record("changeset", approved)
    back = decode_record(record, expected_kind="changeset")

    assert back.state is ChangeSetState.APPROVED
    assert isinstance(back.approval, ApprovalRecord)
    assert back.approval == approval


def test_a_structured_edit_and_action_plan_round_trip():
    edit = StructuredEdit(
        artifact="workflow/routes.yaml", path="approval_routes[0].condition.value",
        expected_current=1000, proposed=500, reason="lower the threshold",
        evidence="workflow/routes.yaml:approval_routes[0]")
    action_plan = ActionPlan(target="rule", actions=(Action(
        kind="WORKFLOW", target="workflow/routes.yaml",
        description="set the value", necessity="required", performer="system",
        impact_evidence=("workflow/routes.yaml:approval_routes[0]",),
        edits=(edit,), reason="carries the value the system reads"),))

    record = encode_record("action_plan", action_plan)
    back = decode_record(record, expected_kind="action_plan")

    assert back == action_plan
    assert back.actions[0].edits[0] == edit


def test_a_verification_result_round_trips():
    from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay

    baseline = InMemoryBaseline.from_artifacts(
        {"workflow/routes.yaml": b"approval_routes: []"})
    overlay = InMemoryOverlay(baseline, "SESSION-CODEC")
    before = overlay.head
    after = overlay.write("workflow/routes.yaml", b"approval_routes: [1]")

    result = verify(
        changeset=ChangeSet(trigger="T", base_snapshot=before.snapshot_id,
                            session_id="S"),
        snapshot_before=before, snapshot_after=after,
        parse=lambda artifact, raw: raw, read_at=lambda doc, path: doc)

    record = encode_record("verification", result)
    back = decode_record(record, expected_kind="verification")

    assert back == result
    assert back.alignment is result.alignment
    assert isinstance(back.alignment, AlignmentState)
    assert back.passed == result.passed


def test_decode_refuses_a_kind_mismatch():
    record = encode_record("shadow_change", assess())
    from app.api.codec import UnknownRecordKind

    with pytest.raises(UnknownRecordKind):
        decode_record(record, expected_kind="risk")


def test_decode_refuses_something_that_is_not_an_envelope():
    from app.api.codec import UnknownRecordKind

    with pytest.raises(UnknownRecordKind):
        decode_record({"not": "an envelope"})
