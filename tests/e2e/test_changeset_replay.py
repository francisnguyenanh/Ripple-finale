# -*- coding: utf-8 -*-
"""From the id alone, the whole chain comes back.

    base_snapshot -> trigger -> intent -> impact -> simulation
      -> proposal -> approval -> execution -> verification

The question an audit asks is never "what changed" -- git answers that. It
is "why was this done, on what evidence, and who agreed". A ChangeSet with
a hole in the chain cannot answer it, and a hole is easy to leave: every
link is optional at the moment it is being skipped.
"""

from __future__ import annotations

import pytest

from core.change_intelligence.apply_gate import APPROVE, apply_changeset, decide, mark_verified
from core.change_intelligence.changeset import (
    BrokenChain,
    CHAIN,
    ChangeSet,
    FIELDS,
    IncompleteChangeSet,
    replay,
    summary,
)
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
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


@pytest.fixture
def overlay():
    import yaml

    baseline = InMemoryBaseline.from_artifacts(
        {ARTIFACT: yaml.safe_dump(_document()).encode("utf-8")})
    return InMemoryOverlay(baseline, "SESSION-REPLAY")


@pytest.fixture
def changeset(overlay):
    return ChangeSet(
        trigger="INTENT-1", base_snapshot=overlay.head.snapshot_id,
        intent=_intent(), affected_nodes=(_node(),),
        affected_artifacts=(ARTIFACT,), evidence=("quote",),
        simulation_result=_Sim(), actions=_plan(), risk=_risk(),
        session_id="SESSION-REPLAY", created_by="analyst",
        created_at="2026-09-20T09:00:00Z")


# --- the eleven fields -------------------------------------------------------


def test_the_changeset_carries_exactly_the_eleven_named_fields(changeset):
    for name in FIELDS:
        assert hasattr(changeset, name), f"{name} is missing"
    assert len(FIELDS) == 11


def test_simulation_result_is_one_of_them(changeset):
    """The field that makes this more than a task list.

    Without it a ChangeSet says "here are files to edit" and the reviewer
    takes that on trust. With it they see what happens to the requests the
    business processes -- which is what they are qualified to judge.
    """
    assert "simulation_result" in FIELDS
    assert changeset.simulation_result is not None


def test_a_changeset_without_a_base_snapshot_is_refused():
    with pytest.raises(IncompleteChangeSet, match="base_snapshot"):
        ChangeSet(trigger="T", base_snapshot="")


def test_a_changeset_without_a_trigger_is_refused():
    with pytest.raises(IncompleteChangeSet, match="trigger"):
        ChangeSet(trigger="", base_snapshot="S")


# --- the chain ---------------------------------------------------------------


def test_the_chain_is_complete_through_the_proposal(changeset):
    links = replay(changeset, through="proposal")
    assert [name for name, _ in links] == [
        "base_snapshot", "trigger", "intent", "impact", "simulation",
        "proposal"]


def test_a_changeset_under_review_is_not_reported_as_broken(changeset):
    """It genuinely has no approval yet; saying so would be noise."""
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="a",
                                    timestamp="t")
    assert replay(reviewed, through="proposal")


def test_a_hole_earlier_than_the_stopping_point_is_still_caught(changeset):
    changeset.intent = None
    with pytest.raises(BrokenChain, match="intent"):
        replay(changeset, through="proposal")


def test_a_missing_simulation_breaks_the_chain(changeset):
    changeset.simulation_result = None
    with pytest.raises(BrokenChain, match="simulation"):
        replay(changeset, through="proposal")


def test_asking_for_approval_before_there_is_one_reports_the_hole(changeset):
    with pytest.raises(BrokenChain, match="approval"):
        replay(changeset, through="approval")


def test_the_full_chain_closes_after_verification(overlay, changeset):
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="a",
                                    timestamp="t")
    approved, _ = decide(reviewed, actor="cfo", timestamp="t",
                         decision=APPROVE, reason="ok")
    applied, _ = apply_changeset(
        approved, overlay=overlay, documents={ARTIFACT: _document()},
        actor="ops", timestamp="t")
    verified = mark_verified(applied, actor="qa", timestamp="t",
                             report={"re_read": ARTIFACT})

    links = replay(verified, through="verification")
    assert all(present for _, present in links)
    assert len(links) == len(CHAIN)


# --- identity ----------------------------------------------------------------


def test_the_id_is_stable_across_two_identical_changesets(overlay):
    def build():
        return ChangeSet(
            trigger="INTENT-1", base_snapshot=overlay.head.snapshot_id,
            intent=_intent(), affected_nodes=(_node(),),
            affected_artifacts=(ARTIFACT,), evidence=("quote",),
            simulation_result=_Sim(), actions=_plan(), risk=_risk())

    assert build().changeset_id == build().changeset_id


def test_the_id_changes_when_the_proposal_changes(changeset):
    from core.change_intelligence.structured_diff import StructuredEdit

    before = changeset.changeset_id
    changeset.actions = _plan([StructuredEdit(
        artifact=ARTIFACT, path="approval_routes[0].condition.value",
        expected_current=1000, proposed=250, reason="a different proposal")])
    assert changeset.changeset_id != before


def test_the_id_does_not_change_when_the_state_changes(changeset):
    before = changeset.changeset_id
    moved = changeset.with_state(ChangeSetState.REVIEW, actor="a",
                                 timestamp="t")
    assert moved.changeset_id == before


def test_the_intent_version_is_part_of_the_identity(changeset):
    """An intent revised to v2 is a different request."""
    before = changeset.proposal_version
    changeset.intent.version = 2
    assert changeset.proposal_version != before


# --- reading -----------------------------------------------------------------


def test_introduced_and_inherited_are_separately_readable(changeset):
    assert changeset.introduced_contradictions == ()
    assert changeset.inherited_contradictions == ()


def test_system_edits_exclude_the_actions_a_person_performs(changeset):
    from core.change_intelligence.action_plan import Action, ActionPlan

    changeset.actions = ActionPlan(target="rule", actions=(
        _plan().actions[0],
        Action(kind="CODE", target="app/handler.py", description="d",
               necessity="required", performer="human",
               impact_evidence=("app/handler.py:12",), reason="hardcodes it")))
    assert all(e.artifact == ARTIFACT for e in changeset.system_edits())


def test_every_state_move_is_recorded_with_an_actor(changeset):
    moved = changeset.with_state(ChangeSetState.REVIEW, actor="analyst",
                                 timestamp="2026-09-20T09:10:00Z",
                                 reason="ready for review")
    assert moved.history[-1].actor == "analyst"
    assert moved.history[-1].timestamp


def test_the_summary_names_the_state_and_the_required_count(changeset):
    text = summary(changeset)
    assert changeset.changeset_id in text
    assert "PROPOSED" in text
    assert "1 required" in text
