# -*- coding: utf-8 -*-
"""Gate G8, direction 1 -- somebody changed a value, and nobody approved it.

    external edit -> snapshot A1 -> A2 -> diff -> ArtifactChanged
      -> detect -> scope -> reconcile -> ALERT -> a person decides

This is the direction that turns the product from a tool that helps you
change things into a system that notices when things changed. One direction
is an assistant; two directions is supervision.

"Somebody edited it out of band" is modelled exactly: a write straight to
the session overlay with no ChangeSet anywhere near it. That is not a
simulation of the situation, it IS the situation -- a change that did not
go through the approval path -- and it has to be caught by the same
mechanism that would catch a real one.

Every test here ends at a report. None of them ends with the system putting
the value back.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from core.change_intelligence.change_detector import detect, is_scoped, scope_for
from core.change_intelligence.events import EventBus, artifact_changed
from core.change_intelligence.lineage import VerifiedLineage
from core.change_intelligence.orchestrator import (
    AWAITING_HUMAN,
    BLOCKED_ON_FACT,
    Orchestrator,
)
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.shadow_change import baseline_fingerprint
from core.change_intelligence.structured_diff import read_at
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
WORKFLOW = "workflow/approval_routes.yaml"
PATH = "approval_routes[3].condition.value"
SESSION = "SESSION-G8-REALITY"


@pytest.fixture
def world():
    """Fresh baseline. Nothing carried over from another test."""
    artifacts = build_baseline(SEED)["artifacts"]
    baseline = InMemoryBaseline.from_artifacts(artifacts)
    overlay = InMemoryOverlay(baseline, SESSION)
    lineage = VerifiedLineage(session_id=SESSION,
                              baseline_id=baseline.baseline_id)
    return artifacts, baseline, overlay, lineage


def _external_edit(overlay, value):
    """A write with no ChangeSet. The definition of an out-of-band change."""
    document = yaml.safe_load(overlay.read(WORKFLOW))
    document["approval_routes"][3]["condition"]["value"] = value
    return overlay.write(
        WORKFLOW, yaml.safe_dump(document, sort_keys=False).encode("utf-8"))


# --- the edit is seen --------------------------------------------------------


def test_an_out_of_band_edit_produces_a_new_snapshot(world):
    _, _, overlay, _ = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    assert after.snapshot_id != before.snapshot_id
    assert after.parent_snapshot_id == before.snapshot_id


def test_the_diff_names_exactly_the_artifact_that_moved(world):
    _, _, overlay, _ = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    assert after.diff_from(before) == [WORKFLOW]


def test_the_change_is_detected_with_its_direction(world):
    _, _, overlay, _ = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    deltas = detect(before, after)
    assert len(deltas) == 1
    assert deltas[0].artifact == WORKFLOW
    assert deltas[0].change == "modified"


def test_an_added_artifact_is_distinguished_from_a_modified_one(world):
    _, _, overlay, _ = world
    before = overlay.head
    after = overlay.write("data/new_file.yaml", b"value: 1\n")
    assert detect(before, after)[0].change == "added"


def test_the_event_carries_both_snapshot_ids(world):
    _, _, overlay, _ = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    event = artifact_changed(
        session_id=SESSION, snapshot_before=before.snapshot_id,
        snapshot_after=after.snapshot_id, artifacts=[WORKFLOW],
        occurred_at="2026-09-20T11:00:00Z")
    assert event.payload["snapshot_before"] == before.snapshot_id
    assert event.payload["snapshot_after"] == after.snapshot_id
    assert event.is_loop_triggering


# --- the loop runs and reports -----------------------------------------------


def test_the_loop_reports_and_waits_for_a_person(world):
    _, _, overlay, lineage = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    turn = Orchestrator(session_id=SESSION, lineage=lineage).run_reality_direction(
        snapshot_before=before, snapshot_after=after, total_artifacts=30,
        occurred_at="2026-09-20T11:00:00Z")

    assert turn.status == AWAITING_HUMAN
    assert turn.scope.artifacts == (WORKFLOW,)


def test_the_out_of_band_edit_never_advances_the_verified_version(world):
    """The chain moved. The verified state did not.

    This is the whole distinction: a snapshot exists for every change, and
    a version exists only for the ones somebody confirmed.
    """
    _, _, overlay, lineage = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    Orchestrator(session_id=SESSION, lineage=lineage).run_reality_direction(
        snapshot_before=before, snapshot_after=after, total_artifacts=30)

    assert lineage.version == 0
    assert not lineage.is_verified(after.snapshot_id)


def test_the_loop_writes_nothing_back(world):
    """Detect and report. The value stays wrong until a person decides."""
    _, _, overlay, lineage = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    Orchestrator(session_id=SESSION, lineage=lineage).run_reality_direction(
        snapshot_before=before, snapshot_after=after, total_artifacts=30)

    assert read_at(yaml.safe_load(overlay.read(WORKFLOW)), PATH) == 300_000


def test_the_baseline_is_untouched_by_the_whole_direction(world):
    artifacts, baseline, overlay, lineage = world
    fingerprint = baseline_fingerprint(artifacts=artifacts)

    before = overlay.head
    after = _external_edit(overlay, 300_000)
    Orchestrator(session_id=SESSION, lineage=lineage).run_reality_direction(
        snapshot_before=before, snapshot_after=after, total_artifacts=30)

    assert baseline_fingerprint(
        artifacts={n: baseline.read(n) for n in baseline.names()}) == fingerprint


def test_reset_restores_the_approved_value(world):
    """The overlay is what made this direction possible in the first place."""
    _, baseline, overlay, _ = world
    original = read_at(yaml.safe_load(baseline.read(WORKFLOW)), PATH)
    _external_edit(overlay, 300_000)
    overlay.clear()
    assert read_at(yaml.safe_load(overlay.read(WORKFLOW)), PATH) == original


# --- scoping: proportional work ----------------------------------------------


def test_changing_one_artifact_does_not_reprocess_them_all(world):
    """If the scope equals the corpus, the word "scoped" is decoration."""
    artifacts, _, overlay, _ = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    scope = scope_for(detect(before, after), total_artifacts=len(artifacts))
    assert len(scope.artifacts) == 1
    assert len(scope.artifacts) < len(artifacts)
    assert is_scoped(scope)


def test_the_scope_reports_what_it_could_not_follow(world):
    """Small because little is affected, or small because nothing was
    supplied? Those are opposite facts."""
    _, _, overlay, _ = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    scope = scope_for(detect(before, after), total_artifacts=30)
    assert scope.notes
    assert any("not scoped" in note or "incomplete" in note
               for note in scope.notes)


def test_no_change_produces_an_empty_scope_that_says_so(world):
    _, _, overlay, _ = world
    scope = scope_for(detect(overlay.head, overlay.head), total_artifacts=30)
    assert scope.is_empty
    assert "nothing changed" in scope.notes[0]


# --- idempotency and replay --------------------------------------------------


def test_the_same_event_delivered_twice_is_handled_once(world):
    _, _, overlay, lineage = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    bus = EventBus()
    handled = []
    bus.subscribe("ArtifactChanged", lambda e: handled.append(e) or "done")

    event = artifact_changed(
        session_id=SESSION, snapshot_before=before.snapshot_id,
        snapshot_after=after.snapshot_id, artifacts=[WORKFLOW],
        occurred_at="2026-09-20T11:00:00Z")

    first, duplicate_first = bus.publish(event)
    second, duplicate_second = bus.publish(event)

    assert len(handled) == 1
    assert first == second
    assert not duplicate_first and duplicate_second


def test_a_duplicate_is_reported_as_a_duplicate(world):
    """"Nothing happened, it was a duplicate" and "nothing happened, nobody
    cared" are different facts."""
    bus = EventBus()
    event = artifact_changed(
        session_id=SESSION, snapshot_before="A", snapshot_after="B",
        artifacts=[WORKFLOW], occurred_at="t")
    bus.publish(event)
    _, duplicate = bus.publish(event)
    assert duplicate


def test_the_dedup_key_is_the_same_for_the_same_transition(world):
    first = artifact_changed(session_id=SESSION, snapshot_before="A",
                             snapshot_after="B", artifacts=["x"],
                             occurred_at="t1")
    second = artifact_changed(session_id=SESSION, snapshot_before="A",
                              snapshot_after="B", artifacts=["x"],
                              occurred_at="t2")
    assert first.dedup_key == second.dedup_key, (
        "the key includes the time, so a redelivery looks like a new fact")


def test_two_different_transitions_do_not_collide():
    first = artifact_changed(session_id=SESSION, snapshot_before="A",
                             snapshot_after="B", artifacts=["x"],
                             occurred_at="t")
    second = artifact_changed(session_id=SESSION, snapshot_before="B",
                              snapshot_after="C", artifacts=["x"],
                              occurred_at="t")
    assert first.dedup_key != second.dedup_key


def test_replaying_the_direction_gives_the_same_result():
    def run():
        artifacts = build_baseline(SEED)["artifacts"]
        baseline = InMemoryBaseline.from_artifacts(artifacts)
        overlay = InMemoryOverlay(baseline, SESSION)
        lineage = VerifiedLineage(session_id=SESSION,
                                  baseline_id=baseline.baseline_id)
        before = overlay.head
        after = _external_edit(overlay, 300_000)
        turn = Orchestrator(session_id=SESSION,
                            lineage=lineage).run_reality_direction(
            snapshot_before=before, snapshot_after=after,
            total_artifacts=len(artifacts))
        return (after.snapshot_id, turn.scope.artifacts, turn.status,
                turn.loop_kind, lineage.version)

    assert run() == run()


# --- I8: the loop cannot reach the Fact layer --------------------------------


def test_a_scope_that_reaches_the_fact_layer_halts(world):
    _, _, overlay, lineage = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    turn = Orchestrator(session_id=SESSION, lineage=lineage).run_reality_direction(
        snapshot_before=before, snapshot_after=after, total_artifacts=30,
        scope_layers={"IR-0001": "ir", "KM-0002": "km"})

    assert turn.status == BLOCKED_ON_FACT
    assert any("fact correction" in note for note in turn.notes)


def test_the_halt_happens_before_any_work_is_done(world):
    """Partly processed and then halted is worse than not started."""
    _, _, overlay, lineage = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    turn = Orchestrator(session_id=SESSION, lineage=lineage).run_reality_direction(
        snapshot_before=before, snapshot_after=after, total_artifacts=30,
        scope_layers={"IR-0001": "ir"})

    assert turn.loop_kind == "", "classification ran after the Fact guard"
    assert turn.reindexed == ()


def test_a_scope_that_stays_off_the_fact_layer_proceeds(world):
    _, _, overlay, lineage = world
    before = overlay.head
    after = _external_edit(overlay, 300_000)

    turn = Orchestrator(session_id=SESSION, lineage=lineage).run_reality_direction(
        snapshot_before=before, snapshot_after=after, total_artifacts=30,
        scope_layers={"KM-0002": "km"})

    assert turn.status == AWAITING_HUMAN


def test_a_fact_error_gap_leaves_the_loop_entirely(world):
    _, _, overlay, lineage = world
    orchestrator = Orchestrator(session_id=SESSION, lineage=lineage)

    request = orchestrator.correction_for({
        "id": "GAP-001", "category": "fact_error",
        "related": ["IR-0001"], "question": "the quoted line is not there"})

    assert request is not None
    assert request.requires_new_ir_version
    assert request.requires_g1_rerun


def test_an_ordinary_gap_stays_in_the_loop(world):
    _, _, overlay, lineage = world
    orchestrator = Orchestrator(session_id=SESSION, lineage=lineage)
    assert orchestrator.correction_for(
        {"id": "GAP-002", "category": "ambiguity"}) is None
