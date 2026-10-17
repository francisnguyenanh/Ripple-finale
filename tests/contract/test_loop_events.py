# -*- coding: utf-8 -*-
"""Six subscribers, read from the schemas, and never seven.

The classification lives in the JSON Schemas as `x-loop-triggering`. This
file's job is to make sure nothing restates it. A list copied into a Python
module is a list that drifts, and it drifts toward subscribing to more
things, because that is the direction somebody debugging a missed update
pushes it.
"""

from __future__ import annotations

import ast
import inspect
import json

import pytest

from core.change_intelligence import events as module
from core.schema.event_registry import SCHEMA_DIR
from core.change_intelligence.events import (
    Event,
    EventBus,
    NotLoopTriggering,
    UnknownEvent,
    artifact_changed,
    change_applied,
    internal,
    loop_triggering,
    verification_completed,
)


# --- the classification has one source ---------------------------------------


def test_six_events_are_loop_triggering():
    assert len(loop_triggering()) == 6


def test_three_events_are_internal():
    assert len(internal()) == 3


def test_together_they_are_the_nine_domain_events():
    assert len(set(loop_triggering()) | set(internal())) == 9


def test_the_classification_comes_from_the_schemas_not_from_this_module():
    """A copied list drifts, and it drifts toward subscribing to more."""
    source = inspect.getsource(module)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        for name in ("HumanFeedbackAdded", "IntentAccepted", "ChangeApproved",
                     "ShadowChangeCreated", "ImpactCalculated",
                     "ChangeProposed"):
            assert node.value != name, (
                f"{name} is named as a literal in events.py; the "
                f"classification belongs in the schema and nowhere else")


def test_each_schema_declares_its_own_classification():
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert "x-loop-triggering" in schema, path.name


def test_the_six_are_the_ones_the_plan_names():
    assert set(loop_triggering()) == {
        "ArtifactChanged", "HumanFeedbackAdded", "IntentAccepted",
        "ChangeApproved", "ChangeApplied", "VerificationCompleted"}


def test_the_listing_is_sorted_so_dispatch_order_is_deterministic():
    assert list(loop_triggering()) == sorted(loop_triggering())


# --- the cycle guard ---------------------------------------------------------


@pytest.mark.parametrize("name", sorted(internal()))
def test_an_internal_event_cannot_be_subscribed_to_the_loop(name):
    """The guard that stops a cycle no unit test would reveal."""
    bus = EventBus()
    with pytest.raises(NotLoopTriggering, match="creates a cycle"):
        bus.subscribe(name, lambda e: None)


@pytest.mark.parametrize("name", sorted(loop_triggering()))
def test_a_loop_triggering_event_can_be_subscribed(name):
    bus = EventBus()
    bus.subscribe(name, lambda e: None)
    assert name in bus.handlers


def test_an_unknown_event_type_is_refused():
    bus = EventBus()
    with pytest.raises(UnknownEvent):
        bus.subscribe("SomethingNobodyDeclared", lambda e: None)


def test_an_event_of_an_unknown_type_cannot_be_built():
    """A typo becomes an error, not an event nobody subscribes to."""
    with pytest.raises(UnknownEvent, match="closed"):
        Event(event_type="ArtefactChanged", dedup_key="k",
              session_id="S", occurred_at="t")


@pytest.mark.parametrize("field", ["event_type", "dedup_key", "session_id",
                                   "occurred_at"])
def test_an_event_missing_a_required_field_is_refused(field):
    kwargs = dict(event_type="ArtifactChanged", dedup_key="k",
                  session_id="S", occurred_at="t")
    kwargs[field] = ""
    with pytest.raises(UnknownEvent):
        Event(**kwargs)


# --- idempotency -------------------------------------------------------------


def test_the_same_key_is_handled_once():
    bus = EventBus()
    calls = []
    bus.subscribe("ArtifactChanged", lambda e: calls.append(e))
    event = artifact_changed(session_id="S", snapshot_before="A",
                             snapshot_after="B", artifacts=["x"],
                             occurred_at="t")
    bus.publish(event)
    bus.publish(event)
    bus.publish(event)
    assert len(calls) == 1
    assert bus.count_of("ArtifactChanged") == 1


def test_a_redelivery_returns_the_first_result():
    bus = EventBus()
    bus.subscribe("ArtifactChanged", lambda e: "handled once")
    event = artifact_changed(session_id="S", snapshot_before="A",
                             snapshot_after="B", artifacts=["x"],
                             occurred_at="t")
    first, _ = bus.publish(event)
    second, duplicate = bus.publish(event)
    assert first == second == ("handled once",)
    assert duplicate


def test_dedup_is_by_key_not_by_id():
    """An id is designed to differ; it cannot answer "is this the same fact"."""
    bus = EventBus()
    calls = []
    bus.subscribe("ArtifactChanged", lambda e: calls.append(e))
    for identifier in ("EVT-1", "EVT-2"):
        bus.publish(Event(
            event_type="ArtifactChanged", dedup_key="same-fact",
            session_id="S", occurred_at="t", event_id=identifier))
    assert len(calls) == 1


def test_the_ledger_holds_only_what_was_dispatched():
    bus = EventBus()
    bus.subscribe("ArtifactChanged", lambda e: None)
    event = artifact_changed(session_id="S", snapshot_before="A",
                             snapshot_after="B", artifacts=["x"],
                             occurred_at="t")
    bus.publish(event)
    bus.publish(event)
    assert len(bus.handled()) == 1


def test_an_event_with_no_subscriber_is_still_not_a_duplicate():
    bus = EventBus()
    _, duplicate = bus.publish(artifact_changed(
        session_id="S", snapshot_before="A", snapshot_after="B",
        artifacts=["x"], occurred_at="t"))
    assert not duplicate


# --- the builders ------------------------------------------------------------


def test_the_artifact_changed_key_is_built_from_content_addresses():
    event = artifact_changed(session_id="S", snapshot_before="SNAP-A",
                             snapshot_after="SNAP-B", artifacts=["x"],
                             occurred_at="t")
    assert event.dedup_key == "ArtifactChanged:SNAP-A:SNAP-B"


def test_the_artifact_list_is_sorted():
    event = artifact_changed(session_id="S", snapshot_before="A",
                             snapshot_after="B", artifacts=["z", "a"],
                             occurred_at="t")
    assert event.payload["artifacts"] == ["a", "z"]


def test_change_applied_names_the_changeset_and_the_new_snapshot():
    event = change_applied(session_id="S", changeset_id="CHG-1",
                           new_snapshot="SNAP-B", occurred_at="t")
    assert event.payload == {"changeset_id": "CHG-1",
                             "new_snapshot": "SNAP-B"}


def test_verification_completed_carries_the_alignment_and_what_is_open():
    event = verification_completed(
        session_id="S", changeset_id="CHG-1", snapshot="SNAP-B",
        alignment="DRIFT", unresolved=["the spreadsheet still says 1000000"],
        occurred_at="t")
    assert event.payload["alignment"] == "DRIFT"
    assert event.payload["unresolved"]


def test_the_module_reads_no_clock_and_mints_no_id():
    """Timestamps arrive as arguments. A generated id would break replay."""
    from tests.contract.test_verification import _executable_source

    source = _executable_source(module)
    for banned in ("datetime.now", "time.time", "uuid", "random"):
        assert banned not in source
