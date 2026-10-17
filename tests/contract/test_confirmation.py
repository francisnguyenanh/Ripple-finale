# -*- coding: utf-8 -*-
"""The two questions, and what answering them is allowed to change.

This is the task where the product stops describing and starts committing.
Almost every test here is about a refusal, because the dangerous failure is
not a wrong answer -- it is an answer that appears in the audit trail with
somebody's name on it that they did not give.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from core.change_intelligence.confirmation import (
    NONE_OF_THESE,
    Q_CURRENT_INTENT,
    Q_INTENTIONAL,
    UnansweredQuestion,
    confirm,
    questions_for,
    unanswered,
)
from core.change_intelligence.discovery import (
    PENDING_HUMAN,
    UnresolvedWithoutHuman,
    discover,
)
from core.change_intelligence.identity import resolve
from core.change_intelligence.ingest import ingest
from core.change_intelligence.knowledge import build_from_groups
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
MANIFEST = yaml.safe_load((SEED / "seed_manifest.yaml").read_text(encoding="utf-8"))

ACTOR = "procurement.manager@acme.example"
WHEN = "2026-09-20T09:15:00Z"
SESSION = "SESSION-CONFIRM"


@pytest.fixture(scope="module")
def report():
    built = build_baseline(SEED)
    overlay = InMemoryOverlay(InMemoryBaseline.from_artifacts(built["artifacts"]),
                              "SESSION-CONFIRM")
    ingestion = ingest(
        overlay.head,
        spreadsheet_reader=lambda raw, path, snap: list(read_xlsx(raw, path, snap)),
        semantic_hints=MANIFEST.get("semantic_hints", {}))
    resolution = resolve(
        ingestion.candidates,
        rule_id_to_semantic_id={r: v["semantic_id"] for r, v in MANIFEST["rules"].items()},
        known_semantic_ids=[v["semantic_id"] for v in MANIFEST["rules"].values()])
    model = build_from_groups(resolution, overlay.head, history=ingestion.records)
    return discover(model, history=ingestion.records)


@pytest.fixture(scope="module")
def hero(report):
    return report.by_semantic_id("approval_threshold")[0]


# --- the questions ---------------------------------------------------------------


def test_both_questions_are_asked_in_the_plan_s_order(hero):
    first, second = questions_for(hero)
    assert first.question_id == Q_INTENTIONAL
    assert second.question_id == Q_CURRENT_INTENT
    assert first.text == "Is this intentional?"


def test_every_option_is_a_source_the_reader_can_open(hero):
    """An option list containing anything but the evidence would be asking
    somebody to endorse a claim the product cannot show them."""
    _, question = questions_for(hero)
    offered = {o.value for o in question.options} - {NONE_OF_THESE}
    expected = {f"{c.artifact}#{c.locator}" for c in hero.claims}
    assert offered == expected


def test_the_options_carry_the_value_each_source_states(hero):
    """A person choosing between sources is choosing between numbers. A
    list of filenames makes them open five files to answer one question."""
    _, question = questions_for(hero)
    stated = {o.states for o in question.options if o.value != NONE_OF_THESE}
    assert stated == {1000000, 500000}


def test_none_of_these_is_always_offered(hero):
    """A question whose options are all wrong, and which must be answered
    anyway, manufactures a decision -- and the result is worse than no
    answer, because it carries a person's name."""
    _, question = questions_for(hero)
    assert NONE_OF_THESE in {o.value for o in question.options}


# --- answering -------------------------------------------------------------------


def test_choosing_a_source_produces_an_intent_naming_the_decider(hero):
    """The bridge to intent handling. A CONFLICT becomes something actionable only when
    an accountable person has said which side is current."""
    outcome = confirm(hero, actor=ACTOR, timestamp=WHEN, session_id=SESSION,
                      intentional="yes",
                      current_intent="data/approval_matrix.xlsx#Thresholds!2",
                      reason="Finance lowered it in June 2022; the workflow was missed")

    assert outcome.intent is not None
    assert outcome.intent.value == 500000
    assert outcome.intent.artifact == "data/approval_matrix.xlsx"
    assert outcome.intent.decided_by == ACTOR
    assert outcome.intent.decided_at == WHEN
    assert outcome.intent.semantic_id == "approval_threshold"


def test_the_resolution_status_names_the_person(hero):
    """"RESOLVED" with no name is how a decision becomes a property of the
    system rather than of whoever made it, and six months later nobody can
    be asked why."""
    assert hero.resolution_status == PENDING_HUMAN
    outcome = confirm(hero, actor=ACTOR, timestamp=WHEN, session_id=SESSION,
                      intentional="yes",
                      current_intent="data/approval_matrix.xlsx#Thresholds!2")
    assert outcome.finding.resolution_status == f"RESOLVED_BY:{ACTOR}"


def test_every_answer_becomes_an_audit_record(hero):
    """I12. Two questions answered, two records."""
    outcome = confirm(hero, actor=ACTOR, timestamp=WHEN, session_id=SESSION,
                      intentional="yes",
                      current_intent="application/config.py#L62",
                      reason="config is authoritative")
    assert len(outcome.decisions) == 2
    answers = {d.answer for d in outcome.decisions}
    assert f"{Q_INTENTIONAL}=yes" in answers
    assert f"{Q_CURRENT_INTENT}=application/config.py#L62" in answers
    for decision in outcome.decisions:
        assert decision.actor == ACTOR and decision.session_id == SESSION


def test_the_original_finding_is_not_mutated(hero):
    """The module fixture is shared. More importantly, a caller that fails
    to persist the outcome must not have silently resolved the finding
    anyway."""
    confirm(hero, actor=ACTOR, timestamp=WHEN, session_id=SESSION,
            intentional="yes", current_intent="application/config.py#L62")
    assert hero.resolution_status == PENDING_HUMAN
    assert hero.gaps == ()


# --- the answers that are not resolutions ------------------------------------------


def test_unclear_is_an_answer_and_produces_a_gap(hero):
    """I5. `unclear` is not a skip. Treating it as "no response" would let
    the hardest findings quietly leave the queue, which is precisely the
    set a reviewer most needs to come back to."""
    outcome = confirm(hero, actor=ACTOR, timestamp=WHEN, session_id=SESSION,
                      intentional="unclear",
                      reason="need to ask whoever changed the matrix")

    assert outcome.gaps and "could not say" in outcome.gaps[0].reason
    assert outcome.intent is None
    assert outcome.finding.resolution_status == PENDING_HUMAN
    assert outcome.decisions, "an 'unclear' still has to be attributable"


def test_none_of_these_records_that_the_rule_lives_somewhere_unread(hero):
    """The most informative answer available, and the one a forced choice
    destroys: every representation the product found is wrong."""
    outcome = confirm(hero, actor=ACTOR, timestamp=WHEN, session_id=SESSION,
                      intentional="no", current_intent=NONE_OF_THESE,
                      reason="the real threshold is in the ERP, which nothing here reads")

    assert outcome.intent is None
    assert outcome.finding.resolution_status == PENDING_HUMAN
    assert outcome.gaps and "no represented source" in outcome.gaps[0].reason
    assert "ERP" in outcome.gaps[0].reason


def test_answering_only_the_first_question_resolves_nothing(hero):
    """"Yes, it is intentional" is a label. It does not say which side is
    current, so there is nothing for intent handling to act on."""
    outcome = confirm(hero, actor=ACTOR, timestamp=WHEN, session_id=SESSION,
                      intentional="yes")
    assert outcome.intent is None
    assert outcome.finding.resolution_status == PENDING_HUMAN
    assert len(outcome.decisions) == 1


# --- refusals ------------------------------------------------------------------------


def test_a_source_that_was_never_offered_is_refused(hero):
    """FAILURE INJECTION. A UI able to submit a value the question never
    offered is a UI whose audit trail records answers nobody gave."""
    with pytest.raises(UnansweredQuestion) as caught:
        confirm(hero, actor=ACTOR, timestamp=WHEN, session_id=SESSION,
                intentional="yes", current_intent="finance/budget_rules.py#L1")
    assert "budget_rules.py" in str(caught.value)


def test_an_invented_answer_to_the_first_question_is_refused(hero):
    with pytest.raises(UnansweredQuestion):
        confirm(hero, actor=ACTOR, timestamp=WHEN, session_id=SESSION,
                intentional="probably")


def test_an_anonymous_decision_is_impossible(hero):
    """I12, enforced by `HumanDecision` itself rather than by convention."""
    with pytest.raises(UnresolvedWithoutHuman):
        confirm(hero, actor="", timestamp=WHEN, session_id=SESSION,
                intentional="yes")


def test_a_decision_without_a_timestamp_is_impossible(hero):
    with pytest.raises(UnresolvedWithoutHuman):
        confirm(hero, actor=ACTOR, timestamp="", session_id=SESSION,
                intentional="yes")


def test_the_module_offers_no_way_to_answer_on_a_person_s_behalf():
    """FAILURE INJECTION, matching the guard on `discovery`.

    No default actor, no generated timestamp, no inferred choice. A
    convenience wrapper that filled any of them in would put a name on an
    answer nobody gave.
    """
    import inspect

    from core.change_intelligence import confirmation

    source = inspect.getsource(confirmation)
    for forbidden in ("datetime.now", "utcnow", "time.time",
                      "auto_confirm", "assume_intent", "default_actor"):
        assert forbidden not in source, f"{forbidden} exists in confirmation.py"

    signature = inspect.signature(confirmation.confirm)
    for required in ("actor", "timestamp", "session_id", "intentional"):
        assert signature.parameters[required].default is inspect.Parameter.empty, (
            f"{required} has a default; it must be supplied by a caller")


# --- the queue ------------------------------------------------------------------------


def test_open_findings_are_counted_from_the_findings_themselves(report, hero):
    """Not from a counter beside them, which is how the two stop agreeing."""
    assert len(unanswered(report.findings)) == len(report.findings)

    resolved = confirm(hero, actor=ACTOR, timestamp=WHEN, session_id=SESSION,
                       intentional="yes",
                       current_intent="application/config.py#L62").finding
    others = [f for f in report.findings if f.finding_id != hero.finding_id]
    assert len(unanswered(others + [resolved])) == len(report.findings) - 1


def test_a_confirmed_intent_does_not_delete_the_conflict(hero):
    """A person saying the spreadsheet is right does not make the workflow
    agree with it. The claims, the evidence and the conflict type all
    survive, because change planning has to repair the artifacts that still disagree."""
    outcome = confirm(hero, actor=ACTOR, timestamp=WHEN, session_id=SESSION,
                      intentional="yes",
                      current_intent="data/approval_matrix.xlsx#Thresholds!2")
    assert outcome.finding.kind == "CONFLICT"
    assert outcome.finding.conflict_type == "value_mismatch"
    assert len(outcome.finding.claims) == len(hero.claims)
    assert {c.value for c in outcome.finding.claims if c.value is not None} == {1000000, 500000}
