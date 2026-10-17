# -*- coding: utf-8 -*-
"""Apply writes into the session. The baseline does not move. I17, I18.

This is the same promise P6 made about what-if, extended to the one
operation that genuinely changes something. It matters more here, not less:
a what-if that corrupted the baseline would produce wrong answers, but an
APPLY that corrupted it would destroy the only thing every session is
compared against, and nothing downstream would have a reference to notice.

Three claims:

  the baseline hash is identical after an apply
  another session sees the old value
  reset returns the session to the baseline
"""

from __future__ import annotations

import yaml

import pytest

from core.change_intelligence.apply_gate import APPROVE, apply_changeset, decide
from core.change_intelligence.changeset import ChangeSet
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.shadow_change import baseline_fingerprint
from core.change_intelligence.states import ChangeSetState

from tests.security.test_approval_bypass import (  # noqa: F401
    ARTIFACT,
    _document,
    _intent,
    _node,
    _plan,
    _risk,
    _Sim,
)


def _artifacts():
    return {ARTIFACT: yaml.safe_dump(_document()).encode("utf-8")}


@pytest.fixture
def baseline():
    return InMemoryBaseline.from_artifacts(_artifacts())


def _approved_changeset(overlay):
    changeset = ChangeSet(
        trigger="INTENT-1", base_snapshot=overlay.head.snapshot_id,
        intent=_intent(), affected_nodes=(_node(),),
        affected_artifacts=(ARTIFACT,), evidence=("quote",),
        simulation_result=_Sim(), actions=_plan(), risk=_risk(),
        session_id=overlay.session_id, created_by="analyst",
        created_at="2026-09-20T09:00:00Z")
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="analyst",
                                    timestamp="t")
    approved, _ = decide(reviewed, actor="cfo", timestamp="t",
                         decision=APPROVE, reason="evidence is clear")
    return approved


def _apply(overlay):
    return apply_changeset(
        _approved_changeset(overlay), overlay=overlay,
        documents={ARTIFACT: _document()}, actor="ops.lead",
        timestamp="2026-09-20T10:00:00Z")


def test_the_baseline_hash_is_identical_after_an_apply(baseline):
    before = baseline_fingerprint(artifacts=_artifacts())
    overlay = InMemoryOverlay(baseline, "SESSION-A")
    _apply(overlay)
    after = baseline_fingerprint(
        artifacts={name: baseline.read(name) for name in baseline.names()})
    assert before == after, (
        "applying an approved change moved the baseline; every session is "
        "compared against it, and nothing downstream has a reference left "
        "to notice with")


def test_the_baseline_bytes_are_unchanged(baseline):
    original = baseline.read(ARTIFACT)
    overlay = InMemoryOverlay(baseline, "SESSION-A")
    _apply(overlay)
    assert baseline.read(ARTIFACT) == original


def test_the_session_sees_the_new_value(baseline):
    overlay = InMemoryOverlay(baseline, "SESSION-A")
    _apply(overlay)
    assert yaml.safe_load(overlay.read(ARTIFACT))[
        "approval_routes"][0]["condition"]["value"] == 500


def test_another_session_still_sees_the_old_value(baseline):
    """I18. An applied change is not a fact about everybody yet."""
    mine = InMemoryOverlay(baseline, "SESSION-A")
    theirs = InMemoryOverlay(baseline, "SESSION-B")
    _apply(mine)
    assert yaml.safe_load(theirs.read(ARTIFACT))[
        "approval_routes"][0]["condition"]["value"] == 1000


def test_reset_still_works_after_an_apply(baseline):
    overlay = InMemoryOverlay(baseline, "SESSION-A")
    _apply(overlay)
    overlay.clear()
    assert overlay.read(ARTIFACT) == baseline.read(ARTIFACT)


def test_apply_produces_a_new_snapshot_rather_than_editing_one(baseline):
    """Snapshots are addressable. An edited one would break replay."""
    overlay = InMemoryOverlay(baseline, "SESSION-A")
    before = overlay.head.snapshot_id
    _, result = _apply(overlay)
    assert result.snapshot.snapshot_id != before
    assert result.snapshot.parent_snapshot_id == before


def test_the_applied_artifact_is_listed_as_changed(baseline):
    overlay = InMemoryOverlay(baseline, "SESSION-A")
    _, result = _apply(overlay)
    assert result.snapshot.list_changed() == [ARTIFACT]
