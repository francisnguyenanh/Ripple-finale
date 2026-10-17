# -*- coding: utf-8 -*-
"""Event contracts.

Nine domain events; six of them drive the reconcile loop. The two numbers
are not in tension -- they are two layers -- but only if the classification
is declared in ONE place and read from there by both P1 and P8. That is what
`x-loop-triggering` is for, and what the last test in this file guards.
"""

from __future__ import annotations

import json

import pytest
from conftest import EVENT_NAMES, EVENTS, is_valid

LOOP_TRIGGERING = {
    "ArtifactChanged",
    "HumanFeedbackAdded",
    "IntentAccepted",
    "ChangeApproved",
    "ChangeApplied",
    "VerificationCompleted",
}

INTERNAL = {"ShadowChangeCreated", "ImpactCalculated", "ChangeProposed"}


def schema(name: str) -> dict:
    return json.loads((EVENTS / f"{name}.json").read_text(encoding="utf-8"))


def envelope(name: str, payload: dict, *, loop: bool | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "event_id": "EVT-0001",
        "dedup_key": f"{name}:SNAP-A0:SNAP-A1",
        "session_id": "SESSION-A",
        "occurred_at": "2026-09-19T12:00:00Z",
        "loop_triggering": (name in LOOP_TRIGGERING) if loop is None else loop,
        "event_type": name,
        "payload": payload,
    }


PAYLOADS = {
    "ArtifactChanged": {"snapshot_before": "SNAP-A0", "snapshot_after": "SNAP-A1",
                        "artifacts": ["approval_matrix.csv"]},
    "HumanFeedbackAdded": {"comment_id": "CMT-001", "semantic_type": "intent"},
    "IntentAccepted": {"intent_id": "INTENT-042", "version": 1, "actor": "ops.lead@example.com"},
    "ChangeApproved": {"changeset_id": "CS-001", "approval": {
        "schema_version": "1.0", "actor": "finance@example.com",
        "timestamp": "2026-09-19T11:00:00Z", "changeset_id": "CS-001",
        "proposal_version": 1, "decision": "APPROVED", "reason": "impact reviewed"}},
    "ChangeApplied": {"changeset_id": "CS-001", "new_snapshot": "SNAP-A1"},
    "VerificationCompleted": {"changeset_id": "CS-001", "alignment": "DRIFT",
                              "unresolved": ["emergency path still hardcoded"]},
    "ShadowChangeCreated": {"shadow_id": "SHADOW-001", "base_snapshot": "SNAP-A0",
                            "target": "approval_threshold", "proposed_value": 500000},
    "ImpactCalculated": {"shadow_id": "SHADOW-001", "edge_count": 5, "unknown_count": 1},
    "ChangeProposed": {"changeset_id": "CS-001", "base_snapshot": "SNAP-A0"},
}


def test_exactly_nine_domain_events_exist():
    on_disk = sorted(p.stem for p in EVENTS.glob("*.json"))
    assert on_disk == sorted(EVENT_NAMES)
    assert len(on_disk) == 9


def test_exactly_six_are_loop_triggering():
    declared = {n for n in EVENT_NAMES if schema(n)["x-loop-triggering"]}
    assert declared == LOOP_TRIGGERING
    assert len(declared) == 6
    assert declared | INTERNAL == set(EVENT_NAMES)
    assert not declared & INTERNAL


@pytest.mark.parametrize("name", EVENT_NAMES)
def test_a_well_formed_event_validates(name):
    assert is_valid(name, envelope(name, PAYLOADS[name]), events=True)


@pytest.mark.parametrize("name", EVENT_NAMES)
@pytest.mark.parametrize("field", ["event_id", "dedup_key", "session_id", "occurred_at",
                                   "loop_triggering", "event_type", "payload"])
def test_every_envelope_field_is_required(name, field):
    """Idempotency is in the contract from P1, not added at P10-T02.

    `dedup_key` in particular: without it, a redelivered event and a genuine
    second occurrence are indistinguishable, and the reconcile loop either
    double-processes or silently drops work.
    """
    event = envelope(name, PAYLOADS[name])
    del event[field]
    assert not is_valid(name, event, events=True)


@pytest.mark.parametrize("name", EVENT_NAMES)
def test_an_event_cannot_claim_to_be_another_event(name):
    event = envelope(name, PAYLOADS[name])
    event["event_type"] = "SomeOtherEvent"
    assert not is_valid(name, event, events=True)


@pytest.mark.parametrize("name", EVENT_NAMES)
def test_payload_rejects_a_field_the_contract_does_not_declare(name):
    """Producer/consumer contract: a producer that has grown a field must
    fail loudly rather than have the consumer ignore it."""
    event = envelope(name, dict(PAYLOADS[name], undeclared_field="x"))
    assert not is_valid(name, event, events=True)


@pytest.mark.parametrize("name", sorted(LOOP_TRIGGERING))
def test_a_loop_triggering_event_may_not_declare_itself_internal(name):
    """The flag on the instance must agree with the flag in the schema.

    Otherwise P8 could be handed an `ArtifactChanged` marked
    `loop_triggering: false` and would skip the reverse drift it exists to
    catch.
    """
    event = envelope(name, PAYLOADS[name], loop=False)
    assert not is_valid(name, event, events=True), (
        f"{name} accepted loop_triggering=false although its schema declares it true"
    )


@pytest.mark.parametrize("name", sorted(INTERNAL))
def test_an_internal_event_may_not_declare_itself_loop_triggering(name):
    """The dangerous direction.

    `ShadowChangeCreated` triggering the loop means every accepted intent
    spawns IntentAccepted -> ShadowChangeCreated -> reconcile ->
    ShadowChangeCreated, forever.
    """
    event = envelope(name, PAYLOADS[name], loop=True)
    assert not is_valid(name, event, events=True)


def test_artifact_changed_needs_at_least_one_artifact():
    """An ArtifactChanged with no artifacts is a wakeup, not an event."""
    event = envelope("ArtifactChanged", {"snapshot_before": "SNAP-A0",
                                         "snapshot_after": "SNAP-A1", "artifacts": []})
    assert not is_valid("ArtifactChanged", event, events=True)


def test_verification_completed_carries_an_alignment_state_not_a_decision():
    event = envelope("VerificationCompleted", {"changeset_id": "CS-001",
                                               "alignment": "NO_CHANGE_REQUIRED",
                                               "unresolved": []})
    assert not is_valid("VerificationCompleted", event, events=True)
