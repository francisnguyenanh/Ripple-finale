# -*- coding: utf-8 -*-
"""Apply must be atomic across artifacts.

`apply_gate.apply_changeset` (the earlier per-artifact shape) validates and writes
per artifact inside one loop, in `sorted(by_artifact)` order. With two
artifacts -- `workflow/routes.yaml` and `zzz/routes.yaml`, both moving
1000 -> 500 -- the first sorts before the second, so the first is written
to the overlay before the second is even looked at. If the second artifact
is missing, stale, or fails to serialise, `StaleDiff` (or another error) is
raised AFTER `workflow/routes.yaml` has already been rewritten and the
overlay head has already moved.

Three cases, each proving the same thing: a failure on the second artifact
must leave the head unchanged and EVERY artifact -- including the first --
at its prior value. Real `ChangeSet`, real `decide()`, real
`InMemoryOverlay`. No mock.

Written against the earlier per-artifact apply_changeset; the staged apply
makes all three green by staging every artifact before publishing one new
head (SS8.4.1).
"""

from __future__ import annotations

import json

import pytest

from core.change_intelligence.action_plan import Action, ActionPlan
from core.change_intelligence.apply_gate import APPROVE, apply_changeset, decide
from core.change_intelligence.changeset import ChangeSet
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.states import ChangeSetState
from core.change_intelligence.structured_diff import StructuredEdit

ARTIFACT_1 = "workflow/routes.yaml"
ARTIFACT_2 = "zzz/routes.yaml"


def _document():
    return {"approval_routes": [
        {"id": "R", "condition": {"field": "amount", "op": ">=",
                                  "value": 1000}}]}


def _edit(artifact, current=1000, proposed=500):
    return StructuredEdit(
        artifact=artifact, path="approval_routes[0].condition.value",
        expected_current=current, proposed=proposed,
        reason="an approved intent lowered this",
        evidence=f"{artifact}:approval_routes[0]")


class _Sim:
    cases: list = []
    total = 0
    changed: list = []

    def new_inconsistencies(self):
        return []

    def inherited_inconsistencies(self):
        return []


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


def _node(artifact):
    from core.change_intelligence.impact import ImpactNode

    return ImpactNode(identifier=artifact, kind="workflow_config",
                      tier="DIRECT", reason="carries the value",
                      evidence=f"{artifact}:approval_routes[0]")


def _risk():
    from core.change_intelligence.risk import assess

    return assess()


def _plan(edits_by_artifact):
    actions = tuple(
        Action(kind="WORKFLOW", target=artifact, description="set the value",
               necessity="required", performer="system",
               impact_evidence=(f"{artifact}:approval_routes[0]",),
               edits=(edit,),
               reason="carries the value the system reads")
        for artifact, edit in edits_by_artifact.items())
    return ActionPlan(target="rule", actions=actions)


def _overlay(artifacts):
    baseline = InMemoryBaseline.from_artifacts(
        {name: json.dumps(_document()).encode("utf-8") for name in artifacts})
    return InMemoryOverlay(baseline, "SESSION-ATOMIC")


def _approved_two_artifact_changeset(overlay, *, second_edit):
    edits_by_artifact = {
        ARTIFACT_1: _edit(ARTIFACT_1),
        ARTIFACT_2: second_edit,
    }
    changeset = ChangeSet(
        trigger="INTENT-1", base_snapshot=overlay.head.snapshot_id,
        intent=_intent(), affected_nodes=(_node(ARTIFACT_1), _node(ARTIFACT_2)),
        affected_artifacts=(ARTIFACT_1, ARTIFACT_2), evidence=("quote",),
        simulation_result=_Sim(), actions=_plan(edits_by_artifact),
        risk=_risk(), session_id=overlay.session_id, created_by="analyst",
        created_at="2026-09-20T09:00:00Z")
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="analyst",
                                    timestamp="t")
    approved, _ = decide(reviewed, actor="cfo", timestamp="t",
                         decision=APPROVE, reason="evidence is clear")
    return approved


def _assert_nothing_moved(overlay, head_before):
    assert overlay.head.snapshot_id == head_before, (
        "the head advanced even though the apply raised; a partial write "
        "was published")
    for artifact in (ARTIFACT_1, ARTIFACT_2):
        assert json.loads(overlay.read(artifact))[
            "approval_routes"][0]["condition"]["value"] == 1000, (
            f"{artifact} was mutated even though the overall apply raised; "
            f"applying part of an approved change is worse than applying "
            f"none of it")


# --- case 1: the second artifact is missing from `documents` -----------------


def test_missing_second_artifact_leaves_every_artifact_unchanged():
    overlay = _overlay((ARTIFACT_1, ARTIFACT_2))
    approved = _approved_two_artifact_changeset(
        overlay, second_edit=_edit(ARTIFACT_2))
    head_before = overlay.head.snapshot_id

    with pytest.raises(Exception):
        apply_changeset(
            approved, overlay=overlay,
            documents={ARTIFACT_1: _document()},  # artifact 2 withheld
            actor="ops.lead", timestamp="2026-09-20T10:00:00Z")

    _assert_nothing_moved(overlay, head_before)


# --- case 2: the second artifact's value is stale -----------------------------


def test_stale_value_on_second_artifact_leaves_every_artifact_unchanged():
    overlay = _overlay((ARTIFACT_1, ARTIFACT_2))
    # expected_current disagrees with the document's actual 1000 -> StaleDiff
    approved = _approved_two_artifact_changeset(
        overlay, second_edit=_edit(ARTIFACT_2, current=999))
    head_before = overlay.head.snapshot_id

    with pytest.raises(Exception):
        apply_changeset(
            approved, overlay=overlay,
            documents={ARTIFACT_1: _document(), ARTIFACT_2: _document()},
            actor="ops.lead", timestamp="2026-09-20T10:00:00Z")

    _assert_nothing_moved(overlay, head_before)


# --- case 3: the serialiser raises on the second artifact ---------------------


def test_serialiser_raising_on_second_artifact_leaves_every_artifact_unchanged():
    overlay = _overlay((ARTIFACT_1, ARTIFACT_2))
    approved = _approved_two_artifact_changeset(
        overlay, second_edit=_edit(ARTIFACT_2))
    head_before = overlay.head.snapshot_id

    def _serialise(artifact, document):
        if artifact == ARTIFACT_2:
            raise ValueError("boom: serialiser refuses this artifact")
        return json.dumps(document).encode("utf-8")

    with pytest.raises(Exception):
        apply_changeset(
            approved, overlay=overlay,
            documents={ARTIFACT_1: _document(), ARTIFACT_2: _document()},
            actor="ops.lead", timestamp="2026-09-20T10:00:00Z",
            serialise=_serialise)

    _assert_nothing_moved(overlay, head_before)


# --- case 4: the WRITE layer itself fails on the second artifact -------------
#
# Probe: both artifacts are validated and
# serialised successfully in Phase 1 -- this is not a validate/serialise
# failure. The failure is in the write layer, and it must land while the
# SECOND artifact is being committed. Phase 1/2 splitting alone does not
# save this case, because a loop of `overlay.write()` calls in Phase 3 still
# advances the head on the first call before the second one raises.
#
# The fix is `overlay.write_all()`: exactly one call, exactly one new
# snapshot, nothing visible until every artifact in the batch is in it. This
# test patches the overlay's write-layer primitive to fail while it is
# processing the second artifact and asserts nothing moved -- valid against
# both the pre-fix `write()` loop and the post-fix `write_all()` single call.


def _break_write_layer_on_second_artifact(overlay):
    """Monkeypatch the overlay's write-layer entry point(s) so that
    processing ARTIFACT_2 raises, wherever in the call chain that happens.

    Works two ways because this test spans a before/after boundary:
      - pre-fix: `apply_changeset` calls `overlay.write()` once per
        artifact in `sorted()` order; ARTIFACT_1 sorts first, so patching
        `write` to raise on artifact 2 raises on the SECOND call.
      - post-fix: `apply_changeset` calls `overlay.write_all()` exactly
        once with both artifacts staged inside; patching `write_all` (or,
        if present, the per-path staging primitive it uses) to raise when
        it reaches ARTIFACT_2 exercises the same failure one layer down.
    """
    if hasattr(overlay, "_stage_blob"):
        original_stage = overlay._stage_blob

        def _stage_blob(path, content):
            if path == ARTIFACT_2:
                raise RuntimeError("boom: the write layer refuses this artifact")
            return original_stage(path, content)

        overlay._stage_blob = _stage_blob
        return

    if hasattr(overlay, "write_all"):
        def _write_all(contents):
            if ARTIFACT_2 in contents:
                raise RuntimeError("boom: the write layer refuses this artifact")
            raise AssertionError(
                "write_all was called without the second artifact staged; "
                "that means the write path split the batch, which is the "
                "defect this test exists to catch")

        overlay.write_all = _write_all
        return

    original_write = overlay.write

    def _write(path, content):
        if path == ARTIFACT_2:
            raise RuntimeError("boom: the write layer refuses this artifact")
        return original_write(path, content)

    overlay.write = _write


def test_write_layer_failure_on_second_artifact_leaves_every_artifact_unchanged():
    overlay = _overlay((ARTIFACT_1, ARTIFACT_2))
    approved = _approved_two_artifact_changeset(
        overlay, second_edit=_edit(ARTIFACT_2))
    head_before = overlay.head.snapshot_id

    _break_write_layer_on_second_artifact(overlay)

    with pytest.raises(Exception):
        apply_changeset(
            approved, overlay=overlay,
            documents={ARTIFACT_1: _document(), ARTIFACT_2: _document()},
            actor="ops.lead", timestamp="2026-09-20T10:00:00Z")

    _assert_nothing_moved(overlay, head_before)
