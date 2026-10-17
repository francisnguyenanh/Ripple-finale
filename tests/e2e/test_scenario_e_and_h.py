# -*- coding: utf-8 -*-
"""The two answers a system that wants to look useful never gives.

    Scenario E   "The workflow is right. The SOP is what's wrong."
                 -> change the document, touch nothing that runs

    Scenario H   "The threshold should be 1,000,000" (it already is)
                 -> ALIGNED, NO_CHANGE_REQUIRED, zero actions

Both run the whole human path -- feedback, extraction, validation,
reconciliation -- rather than calling the reconciler directly, because the
failure being guarded against is a step in the middle quietly inventing
work. A model asked to be helpful will always find something to do, so the
test walks the road it would have walked.
"""

from __future__ import annotations

import pytest

from core.change_intelligence.change_semantics import SemanticType
from core.change_intelligence.feedback import Feedback
from core.change_intelligence.feedback import apply as apply_feedback
from core.change_intelligence.intent import from_draft
from core.change_intelligence.intent_parser import extract
from core.change_intelligence.intent_reconciliation import (
    BEHAVIOUR_KINDS,
    KNOWLEDGE_KINDS,
    Position,
    reconcile_intent,
)
from core.change_intelligence.states import (
    AlignmentState,
    ChangeDecision,
    IntentState,
)

ACTOR = "process.owner@acme.example"
WHEN = "2026-09-20T12:00:00Z"
SESSION = "SESSION-EH"
TARGET = "approval_threshold"
KNOWN = (TARGET,)


def human_path(sentence, positions, current_value):
    """Feedback -> intent -> validation -> reconciliation, as a person walks it."""
    outcome = apply_feedback(Feedback(
        feedback_id="FB-1", semantic_type=SemanticType.INTENT,
        text=sentence, actor=ACTOR, timestamp=WHEN, session_id=SESSION,
        target_semantic_id=TARGET))
    assert outcome.intent_request is not None

    extraction = extract(outcome.intent_request.text,
                         target=TARGET, current_value=current_value)
    assert len(extraction.drafts) == 1
    draft = extraction.drafts[0]

    intent, verdict = from_draft(
        draft, intent_id="INT-EH", actor=ACTOR, timestamp=WHEN,
        known_semantic_ids=KNOWN, scope="procurement_requests")
    assert verdict.ok, verdict.reasons
    assert intent.status is IntentState.VALIDATED

    return intent, reconcile_intent(intent.desired_value, positions, target=TARGET)


# --- Scenario E -------------------------------------------------------------------


@pytest.fixture
def only_the_sop_is_wrong():
    """Everything that RUNS says 1,000,000. One document says 500,000."""
    return [
        Position("workflow", "workflow/approval_routes.yaml", 1000000, "L37"),
        Position("software", "application/config.py", 1000000, "L62"),
        Position("spreadsheet", "data/approval_matrix.xlsx", 1000000, "Thresholds!2"),
        Position("docs", "docs/procurement-sop.md", 500000, "body!29"),
        Position("tests", "tests/test_approval_rules.py", 1000000, "L78"),
    ]


def test_scenario_e_changes_the_document_and_nothing_that_runs(only_the_sop_is_wrong):
    """THE assertion of this module.

    A system that "fixes" the code here has changed working behaviour to
    match a stale document -- which is the most expensive possible way to
    be helpful.
    """
    _, outcome = human_path(
        "The threshold should be JPY 1,000,000.",
        only_the_sop_is_wrong, current_value=1000000)

    assert outcome.decision is ChangeDecision.CHANGE_REQUIRED
    assert outcome.actions, "the stale document was not noticed at all"

    kinds = {a.kind for a in outcome.actions}
    assert kinds <= KNOWLEDGE_KINDS
    assert not kinds & BEHAVIOUR_KINDS, (
        f"a knowledge-only correction proposed changes to {kinds & BEHAVIOUR_KINDS}")
    assert outcome.is_knowledge_only


def test_scenario_e_names_the_one_document_and_no_others(only_the_sop_is_wrong):
    _, outcome = human_path(
        "The threshold should be JPY 1,000,000.",
        only_the_sop_is_wrong, current_value=1000000)

    assert len(outcome.actions) == 1
    action = outcome.actions[0]
    assert action.artifact == "docs/procurement-sop.md"
    assert action.from_value == 500000
    assert action.to_value == 1000000


def test_scenario_e_leaves_every_running_artifact_out_of_the_proposal(
        only_the_sop_is_wrong):
    """Stated as the negative a reviewer would check."""
    _, outcome = human_path(
        "The threshold should be JPY 1,000,000.",
        only_the_sop_is_wrong, current_value=1000000)

    touched = {a.artifact for a in outcome.actions}
    for artifact in ("workflow/approval_routes.yaml", "application/config.py",
                     "tests/test_approval_rules.py"):
        assert artifact not in touched


# --- Scenario H -------------------------------------------------------------------


@pytest.fixture
def everything_already_agrees():
    return [
        Position("workflow", "workflow/approval_routes.yaml", 1000000, "L37"),
        Position("software", "application/config.py", 1000000, "L62"),
        Position("spreadsheet", "data/approval_matrix.xlsx", 1000000, "Thresholds!2"),
        Position("docs", "docs/procurement-sop.md", 1000000, "body!29"),
        Position("tests", "tests/test_approval_rules.py", 1000000, "L78"),
    ]


def test_scenario_h_asks_for_what_is_already_true_and_does_nothing(
        everything_already_agrees):
    """The product saying the right answer is sometimes nothing at all.

    A model asked to be helpful will always find work. Nothing in this path
    consults one.
    """
    _, outcome = human_path(
        "The threshold should be JPY 1,000,000.",
        everything_already_agrees, current_value=1000000)

    assert outcome.alignment is AlignmentState.ALIGNED
    assert outcome.decision is ChangeDecision.NO_CHANGE_REQUIRED
    assert len(outcome.actions) == 0
    assert "already true" in outcome.why


def test_scenario_h_still_produces_a_validated_intent(everything_already_agrees):
    """"Nothing to do" is not "your request was invalid". The person asked
    a reasonable question and deserves a recorded answer, not a rejection."""
    intent, outcome = human_path(
        "The threshold should be JPY 1,000,000.",
        everything_already_agrees, current_value=1000000)

    assert intent.status is IntentState.VALIDATED
    assert intent.desired_value == 1000000
    assert outcome.decision is ChangeDecision.NO_CHANGE_REQUIRED


def test_no_change_required_is_not_an_alignment_state():
    """Gate G5 asks for this by name."""
    assert "NO_CHANGE_REQUIRED" not in {s.name for s in AlignmentState}
    assert ChangeDecision.NO_CHANGE_REQUIRED in ChangeDecision


# --- the two are genuinely different outcomes ---------------------------------------


def test_e_and_h_do_not_collapse_into_each_other(
        only_the_sop_is_wrong, everything_already_agrees):
    """Both are "the code is fine". They are not the same answer, and a
    system that merged them would either invent a doc change in H or miss
    the stale document in E."""
    _, e = human_path("The threshold should be JPY 1,000,000.",
                      only_the_sop_is_wrong, current_value=1000000)
    _, h = human_path("The threshold should be JPY 1,000,000.",
                      everything_already_agrees, current_value=1000000)

    assert e.alignment is not h.alignment
    assert e.decision is not h.decision
    assert bool(e.actions) and not h.actions
    assert e.is_knowledge_only and not h.is_knowledge_only
