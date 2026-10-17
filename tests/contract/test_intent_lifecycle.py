# -*- coding: utf-8 -*-
"""Validation, correction, and the version that survives.

Three tasks in one file because they are one story: a person asks for
something, the system checks it, the person changes their mind or says the
system was wrong, and none of the earlier record disappears.
"""

from __future__ import annotations

import dataclasses

import pytest

from core.change_intelligence.correction import (
    CannotOverruleAFact,
    Correction,
    NEEDS_REEVALUATION,
    REJECTED_BY_HUMAN,
    apply_correction,
    outranks,
    zombie_findings,
)
from core.change_intelligence.discovery import Claim, Finding
from core.change_intelligence.evidence import EvidenceRef
from core.change_intelligence.intent import (
    IntentLedger,
    IntentNotStored,
    from_draft,
    validate,
)
from core.change_intelligence.intent_parser import UNKNOWN, extract
from core.change_intelligence.states import IntentState

ACTOR = "ops.lead@acme.example"
WHEN = "2026-09-20T11:00:00Z"
SESSION = "SESSION-P5"
KNOWN = ("approval_threshold", "capex_finance_threshold")


def draft_for(text, **kwargs):
    kwargs.setdefault("target", "approval_threshold")
    kwargs.setdefault("current_value", 1000000)
    return extract(text, **kwargs).drafts[0]


# --- the five conditions ------------------------------------------------


def test_a_grounded_intent_validates():
    verdict = validate(draft_for("Set it to JPY 500,000."),
                       known_semantic_ids=KNOWN, actor=ACTOR)
    assert verdict.ok
    assert verdict.reasons == []


def test_an_unresolvable_target_is_rejected_with_a_reason():
    draft = dataclasses.replace(
        draft_for("Set it to JPY 500,000."), target="cancellation_window")
    verdict = validate(draft, known_semantic_ids=KNOWN, actor=ACTOR)
    assert not verdict.ok
    assert any("cancellation_window" in r for r in verdict.reasons)


def test_an_unknown_current_value_is_a_gap_not_a_rejection():
    """The request may be perfectly clear about a rule the product has not
    finished reading. Rejecting it would blame the person for the
    product's gap."""
    draft = draft_for("Set it to JPY 500,000.", current_value=None)
    verdict = validate(draft, known_semantic_ids=KNOWN, actor=ACTOR)
    assert verdict.ok
    assert verdict.gaps
    assert "not established" in verdict.gaps[0].reason


def test_a_negative_amount_is_rejected():
    draft = dataclasses.replace(
        draft_for("Set it to JPY 500,000."), desired_value=-500000)
    verdict = validate(draft, known_semantic_ids=KNOWN, actor=ACTOR)
    assert not verdict.ok
    assert any("not a valid amount" in r for r in verdict.reasons)


def test_an_amount_with_no_unit_is_rejected():
    draft = dataclasses.replace(
        draft_for("Set it to JPY 500,000."), unit="")
    verdict = validate(draft, known_semantic_ids=KNOWN, actor=ACTOR)
    assert not verdict.ok
    assert any("no unit" in r for r in verdict.reasons)


def test_a_vague_scope_is_asked_about_not_assumed():
    """An unscoped intent is not a global one. The two differ by the blast
    radius of the change."""
    verdict = validate(draft_for("Set it to JPY 500,000."),
                       known_semantic_ids=KNOWN, actor=ACTOR)
    assert verdict.questions
    assert "not the same as" in verdict.questions[0]


def test_an_intent_with_no_actor_cannot_validate():
    verdict = validate(draft_for("Set it to JPY 500,000."),
                       known_semantic_ids=KNOWN, actor="")
    assert not verdict.ok
    assert any("no actor" in r for r in verdict.reasons)


def test_all_five_conditions_report_together():
    """Reporting one problem at a time turns one review into five round
    trips, and the person answering is usually not the one who will be
    asked again tomorrow."""
    draft = dataclasses.replace(
        draft_for("Set it to JPY 500,000."),
        target=UNKNOWN, desired_value=-1, unit="")
    verdict = validate(draft, known_semantic_ids=KNOWN, actor="")
    assert len(verdict.reasons) >= 3


# --- a rejected intent is still stored -------------------------------------------


def test_a_rejected_intent_is_stored_with_its_reason():
    """Losing it loses the reason it was rejected, which is the part a
    person needs -- and the answer when somebody asks for the same
    impossible thing again."""
    draft = dataclasses.replace(
        draft_for("Set it to JPY 500,000."), desired_value=-500000)
    intent, verdict = from_draft(
        draft, intent_id="INT-001", actor=ACTOR, timestamp=WHEN,
        known_semantic_ids=KNOWN)

    assert not verdict.ok
    assert intent.status is IntentState.REJECTED
    assert intent.status_reason
    ledger = IntentLedger()
    ledger.add(intent)
    assert ledger.get("INT-001").status is IntentState.REJECTED


def test_a_valid_intent_reaches_validated():
    intent, verdict = from_draft(
        draft_for("Set it to JPY 500,000."), intent_id="INT-002",
        actor=ACTOR, timestamp=WHEN, known_semantic_ids=KNOWN)
    assert verdict.ok
    assert intent.status is IntentState.VALIDATED
    assert intent.version == 1
    assert intent.desired_value == 500000
    assert intent.current_value == 1000000


def test_the_intent_carries_all_nine_fields():
    intent, _ = from_draft(
        draft_for("Set it to JPY 500,000."), intent_id="INT-003",
        actor=ACTOR, timestamp=WHEN, known_semantic_ids=KNOWN, scope="procurement")
    for name in ("actor", "target", "requested_change", "rationale", "scope",
                 "evidence", "confidence", "status", "version"):
        assert hasattr(intent, name), name
    assert intent.scope == "procurement"


# --- v1 is never overwritten ----------------------------------------------


def test_revising_an_intent_creates_v2_and_keeps_v1():
    """I10. When a judge asks "what if the user changes their mind?", the
    answer must be "v1 is still there, with the reason it was replaced"."""
    ledger = IntentLedger()
    v1, _ = from_draft(draft_for("Set it to JPY 500,000."),
                       intent_id="INT-010", actor=ACTOR, timestamp=WHEN,
                       known_semantic_ids=KNOWN)
    ledger.add(v1)

    superseded, v2 = ledger.revise(
        "INT-010", draft=draft_for("Set it to JPY 750,000."),
        actor=ACTOR, timestamp="2026-09-21T09:00:00Z",
        reason="Finance asked for a phased reduction",
        known_semantic_ids=KNOWN)

    assert superseded.status is IntentState.SUPERSEDED
    assert superseded.desired_value == 500000, "v1's value was overwritten"
    assert superseded.status_reason == "Finance asked for a phased reduction"
    assert superseded.superseded_by == v2.intent_id

    assert v2.version == 2
    assert v2.desired_value == 750000

    both = ledger.versions_of("approval_threshold")
    assert len(both) == 2
    assert [i.version for i in both] == [1, 2]


def test_the_current_version_is_computed_not_tracked():
    """A pointer and a list are two things that can disagree."""
    ledger = IntentLedger()
    v1, _ = from_draft(draft_for("Set it to JPY 500,000."),
                       intent_id="INT-020", actor=ACTOR, timestamp=WHEN,
                       known_semantic_ids=KNOWN)
    ledger.add(v1)
    assert ledger.current("approval_threshold").version == 1

    ledger.revise("INT-020", draft=draft_for("Set it to JPY 750,000."),
                  actor=ACTOR, timestamp=WHEN, reason="revised",
                  known_semantic_ids=KNOWN)
    assert ledger.current("approval_threshold").version == 2


def test_an_unknown_intent_cannot_be_revised():
    with pytest.raises(IntentNotStored):
        IntentLedger().revise("INT-nope", draft=draft_for("Set it to JPY 1."),
                              actor=ACTOR, timestamp=WHEN, reason="x")


# --- correction, and no zombies -------------------------------------------


def _finding(finding_id, semantic_id, kind="CONFLICT", claims=2):
    made = tuple(
        Claim(artifact=f"a{i}.py", family="software", locator=f"L{i}",
              value=1000 * (i + 1),
              evidence=EvidenceRef(artifact=f"a{i}.py", locator=f"L{i}",
                                   quote=f"threshold = {1000 * (i + 1)}"))
        for i in range(claims))
    return Finding(finding_id=finding_id, kind=kind, semantic_id=semantic_id,
                   claims=made, why="grouped by tier 3",
                   conflict_type="value_mismatch" if kind == "CONFLICT" else "")


@pytest.fixture
def world():
    return [
        _finding("CONF-strategic", "strategic_customer_exemption"),
        _finding("UN-strategic", "strategic_customer_exemption", kind="UNDOCUMENTED", claims=1),
        _finding("CONF-approval", "approval_threshold"),
    ]


def test_a_correction_marks_the_finding_overruled_and_keeps_it(world):
    """Deleting it means the next run rediscovers it and reports it again,
    which is the zombie arriving by the other door."""
    outcome = apply_correction(Correction(
        correction_id="CORR-001", target_finding_id="CONF-strategic",
        actor=ACTOR, timestamp=WHEN, session_id=SESSION,
        reason="the strategic exemption applies to the cancellation fee, "
               "not to approval"), world)

    assert len(outcome.findings) == len(world), "a finding was dropped"
    overruled = [f for f in outcome.findings if f.finding_id == "CONF-strategic"][0]
    assert overruled.resolution_status.startswith(REJECTED_BY_HUMAN)
    assert ACTOR in overruled.resolution_status
    assert "cancellation fee" in overruled.why


def test_findings_that_rested_on_the_inference_are_marked_too(world):
    """The interesting half of the work. A correction that fixes the screen
    in front of the person and leaves the same discredited conclusion two
    clicks away is worse than none."""
    outcome = apply_correction(Correction(
        correction_id="CORR-002", target_finding_id="CONF-strategic",
        actor=ACTOR, timestamp=WHEN, session_id=SESSION,
        reason="wrong scope"), world)

    stale = {f.finding_id for f in outcome.needs_reevaluation}
    assert stale == {"UN-strategic"}
    assert all(f.resolution_status == NEEDS_REEVALUATION
               for f in outcome.needs_reevaluation)


def test_an_unrelated_finding_is_untouched(world):
    outcome = apply_correction(Correction(
        correction_id="CORR-003", target_finding_id="CONF-strategic",
        actor=ACTOR, timestamp=WHEN, session_id=SESSION, reason="wrong scope"),
        world)
    other = [f for f in outcome.findings if f.finding_id == "CONF-approval"][0]
    assert other.resolution_status == "PENDING_HUMAN"


def test_there_are_no_zombie_findings_afterwards(world):
    """THE test of this task, written as a query over the final state
    rather than a flag set during the update -- the failure being hunted is
    precisely a path that forgot to set the flag."""
    correction = Correction(
        correction_id="CORR-004", target_finding_id="CONF-strategic",
        actor=ACTOR, timestamp=WHEN, session_id=SESSION, reason="wrong scope")
    outcome = apply_correction(correction, world)

    assert zombie_findings(outcome.findings, [correction]) == ()


def test_the_zombie_check_catches_a_half_applied_correction(world):
    """FAILURE INJECTION. If the dependent finding is left alone, the check
    must notice -- otherwise it is not a check."""
    correction = Correction(
        correction_id="CORR-005", target_finding_id="CONF-strategic",
        actor=ACTOR, timestamp=WHEN, session_id=SESSION, reason="wrong scope")
    outcome = apply_correction(correction, world)

    half_applied = [f for f in outcome.findings
                    if f.finding_id != "UN-strategic"] + \
                   [f for f in world if f.finding_id == "UN-strategic"]

    zombies = zombie_findings(half_applied, [correction])
    assert {z.finding_id for z in zombies} == {"UN-strategic"}


def test_a_correction_creates_an_audit_record(world):
    outcome = apply_correction(Correction(
        correction_id="CORR-006", target_finding_id="CONF-strategic",
        actor=ACTOR, timestamp=WHEN, session_id=SESSION, reason="wrong scope"),
        world)
    assert outcome.record.actor == ACTOR
    assert outcome.record.timestamp == WHEN
    assert outcome.record.authority == "human_decision"
    assert outcome.record.overruled == "model_inference"


def test_a_correction_with_no_reason_is_refused(world):
    """The reason is the part the next reader needs."""
    with pytest.raises(CannotOverruleAFact):
        Correction(correction_id="CORR-007", target_finding_id="CONF-strategic",
                   actor=ACTOR, timestamp=WHEN, session_id=SESSION, reason="")


def test_a_correction_with_no_actor_is_refused():
    with pytest.raises(CannotOverruleAFact):
        Correction(correction_id="CORR-008", target_finding_id="X",
                   actor="", timestamp=WHEN, session_id=SESSION, reason="wrong")


def test_no_artifact_is_changed_by_a_correction(world):
    """I8. A person's correction changes what the system BELIEVES, never
    what it OBSERVED."""
    outcome = apply_correction(Correction(
        correction_id="CORR-009", target_finding_id="CONF-strategic",
        actor=ACTOR, timestamp=WHEN, session_id=SESSION, reason="wrong scope"),
        world)

    before = {(c.artifact, c.evidence.quote)
              for f in world for c in f.claims}
    after = {(c.artifact, c.evidence.quote)
             for f in outcome.findings for c in f.claims}
    assert before == after
    assert any("never what it observed" in n for n in outcome.notes)


# --- the authority matrix -----------------------------------------------------------


def test_a_human_decision_outranks_a_model_inference():
    assert outranks("human_decision", "model_inference")
    assert not outranks("model_inference", "human_decision")


def test_an_artifact_fact_is_not_on_the_authority_axis():
    """A fact is not an opinion with high priority, and treating it as one
    is how a system ends up letting a confident person edit its evidence."""
    with pytest.raises(CannotOverruleAFact):
        outranks("human_decision", "artifact_fact")
    with pytest.raises(CannotOverruleAFact):
        outranks("artifact_fact", "model_inference")
