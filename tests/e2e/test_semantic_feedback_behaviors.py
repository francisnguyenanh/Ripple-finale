# -*- coding: utf-8 -*-
"""Gate G5 -- four near-identical sentences, four different consequences.

    "The threshold should be 500,000"              [intent]
    "1,000,000 is right; the spreadsheet is wrong" [correction]
    "The strategic exemption is deliberate"        [exception]
    "The threshold is measured before tax"         [clarification]

Four remarks about one rule, in the same register, from the same person, on
the same day. If they all take the same path then this is a notes app with
an enum on it, so the assertions compare the OUTCOMES to each other --
asserting each one "succeeds" would pass for a system that does nothing
seven different ways.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from core.change_intelligence.alignment import Gap
from core.change_intelligence.change_semantics import (
    SEMANTICS,
    Downstream,
    SemanticType,
    distinct_behaviours,
)
from core.change_intelligence.discovery import discover
from core.change_intelligence.evidence import EvidenceRef
from core.change_intelligence.feedback import (
    Feedback,
    FeedbackNotApplicable,
    UnattributedFeedback,
    apply,
    distinct_outcomes,
)
from core.change_intelligence.identity import resolve
from core.change_intelligence.ingest import ingest
from core.change_intelligence.knowledge import build_from_groups
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.states import ChangeSetState, TransitionError
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
MANIFEST = yaml.safe_load((SEED / "seed_manifest.yaml").read_text(encoding="utf-8"))

ACTOR = "process.owner@acme.example"
WHEN = "2026-09-20T10:00:00Z"
SESSION = "SESSION-G5"


@pytest.fixture(scope="module")
def report():
    built = build_baseline(SEED)
    overlay = InMemoryOverlay(InMemoryBaseline.from_artifacts(built["artifacts"]),
                              "SESSION-G5")
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


def _feedback(semantic_type, text, **kwargs):
    return Feedback(
        feedback_id=f"FB-{semantic_type.value}",
        semantic_type=semantic_type,
        text=text, actor=ACTOR, timestamp=WHEN, session_id=SESSION, **kwargs)


@pytest.fixture
def four(hero):
    """The four sentences of Gate G5, applied."""
    gaps = (Gap(gap_id="GAP-approval_threshold-basis",
                reason="is the threshold measured before or after tax?"),)

    intent = apply(_feedback(
        SemanticType.INTENT, "The threshold should be 500,000.",
        target_semantic_id="approval_threshold"), findings=[hero])

    correction = apply(_feedback(
        SemanticType.CORRECTION,
        "1,000,000 is right; the spreadsheet is the one that is wrong.",
        target_finding_id=hero.finding_id), findings=[hero])

    exception = apply(_feedback(
        SemanticType.EXCEPTION,
        "The strategic exemption is deliberate.",
        target_finding_id=hero.finding_id), findings=[hero])

    clarification = apply(_feedback(
        SemanticType.CLARIFICATION,
        "The threshold is measured on the value before tax.",
        target_semantic_id="approval_threshold"), findings=[hero], gaps=gaps)

    return intent, correction, exception, clarification


# --- the gate ---------------------------------------------------------------------


def test_four_feedback_classes_produce_four_different_outcomes(four):
    """The gate, stated as a comparison rather than as four successes."""
    assert len(distinct_outcomes(four)) == 4, (
        "four classes collapsed to fewer effects; this is a notes app with "
        "an enum on it")


def test_the_four_downstreams_are_themselves_distinct(four):
    intent, correction, exception, clarification = four
    assert intent.downstream is Downstream.OPEN_CHANGE_BRANCH
    assert correction.downstream is Downstream.REEVALUATE_INFERENCE
    assert exception.downstream is Downstream.MARK_INTENTIONAL
    assert clarification.downstream is Downstream.MAY_CLOSE_GAP
    assert len({o.downstream for o in four}) == 4


def test_only_intent_opens_a_change_branch(four):
    """The type that can cost money is the only one that can start work."""
    intent, correction, exception, clarification = four
    assert intent.intent_request is not None
    assert correction.intent_request is None
    assert exception.intent_request is None
    assert clarification.intent_request is None


def test_only_correction_rejects_an_inference(four):
    intent, correction, exception, clarification = four
    assert correction.inference_rejection is not None
    assert all(o.inference_rejection is None
               for o in (intent, exception, clarification))


def test_only_exception_marks_a_finding_intentional(four):
    intent, correction, exception, clarification = four
    assert exception.intentional_mark is not None
    assert all(o.intentional_mark is None
               for o in (intent, correction, clarification))


def test_the_contract_itself_offers_at_least_four_behaviours():
    """Counted from the contract rather than from this module, so the gate is met
    by the design and not by the implementation happening to branch."""
    assert len(distinct_behaviours()) >= 4
    assert len(SEMANTICS) == 7


# --- the architecture rule --------------------------------------------------------


def test_no_feedback_reaches_the_knowledge_model(four):
    """I8/I9. There is no path from a person's remark to a Fact.

    `apply` describes what should happen and a caller with the authority
    carries it out. A remark that could write to the KM would let anybody
    assert a fact by typing it, and the product's entire claim is that
    facts come from artifacts.
    """
    for outcome in four:
        assert outcome.touches_knowledge_model() is False


def test_the_module_cannot_write_anywhere():
    import inspect

    from core.change_intelligence import feedback

    source = inspect.getsource(feedback)
    for forbidden in ("import sqlite", "json.dump", "write_text",
                      "from core.kernel", "session.commit"):
        assert forbidden not in source, f"{forbidden} in feedback.py"


def test_an_intent_request_carries_the_sentence_unparsed(four):
    """Extraction is a provider's job (I11). A module that guessed at the
    number here would open a change branch on a guess."""
    intent, *_ = four
    assert intent.intent_request.text == "The threshold should be 500,000."
    assert not hasattr(intent.intent_request, "desired_value")


# --- every remark is recorded -------------------------------------------------------


def test_an_observation_triggers_nothing_and_is_still_recorded(hero):
    """The shortest branch in the module, and the one most often left out.

    An observation is the kind of feedback a person gives most often.
    Dropping it means "nobody told us" becomes unanswerable.
    """
    outcome = apply(_feedback(
        SemanticType.OBSERVATION,
        "This spreadsheet has not been updated since 2022."), findings=[hero])

    assert outcome.downstream is Downstream.LEDGER_ONLY
    assert outcome.intent_request is None
    assert outcome.inference_rejection is None
    assert outcome.intentional_mark is None
    assert outcome.gap_resolutions == ()
    assert outcome.transitions == ()
    assert outcome.ledger.text.startswith("This spreadsheet")
    assert outcome.ledger.actor == ACTOR


def test_every_class_produces_a_ledger_entry(four):
    for outcome in four:
        assert outcome.ledger.actor == ACTOR
        assert outcome.ledger.timestamp == WHEN
        assert outcome.ledger.session_id == SESSION
        assert outcome.ledger.semantic_type
        assert outcome.ledger.downstream


# --- clarification: the branch most likely to be written the easy way -----------------


def test_a_clarification_without_evidence_does_not_close_a_gap(four):
    """"Measured before tax" is useful context and, on its own, one more
    person's assertion. Closing a Gap with it converts an opinion into a
    settled fact -- the exact move this product exists to catch other
    systems making.
    """
    *_, clarification = four
    assert clarification.gap_resolutions
    for resolution in clarification.gap_resolutions:
        assert resolution.closes is False
        assert "no evidence" in resolution.why


def test_a_clarification_with_evidence_may_close_a_gap(hero):
    gaps = (Gap(gap_id="GAP-approval_threshold-basis", reason="before or after tax?"),)
    outcome = apply(_feedback(
        SemanticType.CLARIFICATION,
        "The policy says the figure is exclusive of tax.",
        target_semantic_id="approval_threshold",
        evidence=(EvidenceRef(artifact="docs/procurement-policy.md",
                              locator="body!22",
                              quote="Amounts are stated exclusive of tax."),)),
        findings=[hero], gaps=gaps)

    assert outcome.gap_resolutions
    assert all(r.closes for r in outcome.gap_resolutions)


# --- approval and rejection move a machine --------------------------------------------


def test_approval_and_rejection_are_state_transitions(hero):
    approved = apply(
        _feedback(SemanticType.APPROVAL, "Approved for this quarter."),
        findings=[hero],
        state=ChangeSetState.REVIEW, target_state=ChangeSetState.APPROVED)
    rejected = apply(
        _feedback(SemanticType.REJECTION, "Not until Finance has seen it."),
        findings=[hero],
        state=ChangeSetState.REVIEW, target_state=ChangeSetState.REJECTED)

    assert approved.transitions and rejected.transitions
    assert approved.downstream is rejected.downstream is Downstream.STATE_TRANSITION


def test_the_two_share_a_downstream_on_purpose():
    """Inventing a seventh Downstream so the gate's count looked better
    would be padding the metric rather than meeting it."""
    assert (SEMANTICS[SemanticType.APPROVAL].downstream
            is SEMANTICS[SemanticType.REJECTION].downstream)


def test_a_forbidden_transition_is_refused(hero):
    with pytest.raises(TransitionError):
        apply(_feedback(SemanticType.APPROVAL, "Skip review."),
              findings=[hero],
              state=ChangeSetState.PROPOSED,
              target_state=ChangeSetState.APPLIED)


# --- refusals ---------------------------------------------------------------------------


def test_feedback_with_no_actor_is_impossible():
    """I12, one step earlier than the state machine applies it."""
    with pytest.raises(UnattributedFeedback):
        Feedback(feedback_id="FB-1", semantic_type=SemanticType.OBSERVATION,
                 text="x", actor="", timestamp=WHEN, session_id=SESSION)


def test_feedback_with_no_timestamp_is_impossible():
    with pytest.raises(UnattributedFeedback):
        Feedback(feedback_id="FB-1", semantic_type=SemanticType.OBSERVATION,
                 text="x", actor=ACTOR, timestamp="", session_id=SESSION)


def test_an_exception_naming_a_finding_that_is_not_there_is_refused(hero):
    """Either a stale UI or a typo, and both are worth surfacing now rather
    than at the next report."""
    with pytest.raises(FeedbackNotApplicable) as caught:
        apply(_feedback(SemanticType.EXCEPTION, "Deliberate.",
                        target_finding_id="CONF-does-not-exist"),
              findings=[hero])
    assert "CONF-does-not-exist" in str(caught.value)


def test_an_approval_with_no_state_is_refused(hero):
    with pytest.raises(FeedbackNotApplicable):
        apply(_feedback(SemanticType.APPROVAL, "ok"), findings=[hero])
