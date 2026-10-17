# -*- coding: utf-8 -*-
"""The Knowledge Model, and the four negative cases it must refuse.

The gates exist because a Knowledge Model record is something the rest of
the product is entitled to trust. Anything that reaches it without passing
every gate makes every downstream claim unverifiable, and nothing later
would notice.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from core.change_intelligence.evidence import EvidenceRef
from core.change_intelligence.identity import resolve
from core.change_intelligence.ingest import ingest
from core.change_intelligence.knowledge import (
    Candidate,
    PromotionRefused,
    build_from_groups,
    open_gap,
    promote,
    propose_and_promote,
)
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.states import AlignmentState, KnowledgeState
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
MANIFEST = yaml.safe_load((SEED / "seed_manifest.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def world():
    built = build_baseline(SEED)
    baseline = InMemoryBaseline.from_artifacts(built["artifacts"])
    overlay = InMemoryOverlay(baseline, "SESSION-KM")
    ingestion = ingest(
        overlay.head,
        spreadsheet_reader=lambda raw, path, snap: list(read_xlsx(raw, path, snap)),
        semantic_hints=MANIFEST.get("semantic_hints", {}),
    )
    resolution = resolve(
        ingestion.candidates,
        rule_id_to_semantic_id={r: v["semantic_id"] for r, v in MANIFEST["rules"].items()},
        known_semantic_ids=[v["semantic_id"] for v in MANIFEST["rules"].values()],
    )
    return overlay, ingestion, resolution


def good_evidence(snapshot) -> tuple[EvidenceRef, ...]:
    return (EvidenceRef(
        artifact="workflow/approval_routes.yaml",
        locator="L37",
        quote="value: 1000000",
        snapshot_id=snapshot.snapshot_id),)


def good_candidate(snapshot, **overrides) -> Candidate:
    base = dict(
        knowledge_type="rule",
        semantic_id="approval_threshold",
        statement="Requests at or above 1,000,000 JPY need manager approval.",
        evidence=good_evidence(snapshot),
        derived_from=("workflow:approval_routes.yaml:condition:PROCUREMENT_STANDARD",),
        confidence=0.9,
        proposer="test",
    )
    base.update(overrides)
    return Candidate(**base)


# --- the deterministic half --------------------------------------------------


def test_the_knowledge_model_is_built_without_any_model(world):
    """The groups already say what belongs together; comparing values is
    arithmetic, not judgement."""
    overlay, ingestion, resolution = world
    model = build_from_groups(resolution, overlay.head, history=ingestion.records)

    assert model.records
    assert all(r.evidence and r.derived_from for r in model.records), "I2"


def test_the_hero_rule_is_recorded_as_a_conflict_with_both_sides(world):
    overlay, ingestion, resolution = world
    model = build_from_groups(resolution, overlay.head, history=ingestion.records)

    record = model.by_semantic_id("approval_threshold")[0]
    assert record.alignment is AlignmentState.CONFLICT
    # A representation whose value is None is a document that COVERS the
    # rule without stating a number ("the system approves small requests").
    # It belongs in the record's lineage and must stay out of the value
    # comparison -- see `discovery._comparable`. The assertion is about the
    # stated values, which are the two sides of the conflict.
    stated = {r.value for r in record.representations if r.value is not None}
    assert stated == {1_000_000, 500_000}, "a conflict that lost a side is not a conflict"
    assert "1000000" in record.statement and "500000" in record.statement


def test_a_rule_everyone_agrees_on_is_recorded_as_aligned(world):
    overlay, ingestion, resolution = world
    model = build_from_groups(resolution, overlay.head, history=ingestion.records)

    record = model.by_semantic_id("capex_finance_threshold")[0]
    assert record.alignment is AlignmentState.ALIGNED


def test_no_record_names_a_winning_side(world):
    """I15. Reconciliation reports; a person decides."""
    overlay, ingestion, resolution = world
    model = build_from_groups(resolution, overlay.head, history=ingestion.records)

    for record in model.conflicts():
        assert not hasattr(record, "resolved_value")
        assert len({r.value for r in record.representations}) > 1


def test_every_record_carries_its_lineage(world):
    """I2. A record whose derivation cannot be traced is an assertion."""
    overlay, ingestion, resolution = world
    model = build_from_groups(resolution, overlay.head, history=ingestion.records)
    for record in model.records:
        assert record.derived_from
        assert len(record.derived_from) == len(set(record.derived_from))


def test_the_gaps_from_identity_resolution_survive_into_the_model(world):
    """A rule the system could not place must stay visible. Dropping gaps
    at this boundary is how UNKNOWN silently becomes 'nothing to report'."""
    overlay, ingestion, resolution = world
    model = build_from_groups(resolution, overlay.head, history=ingestion.records)
    assert len(model.gaps) == len(resolution.gaps)
    assert model.gaps


# --- the four negative cases -------------------------------------------------


def test_malformed_json_is_rejected_by_the_schema_gate(world):
    """The model returned something that is not the declared shape."""
    overlay, _, _ = world
    with pytest.raises(PromotionRefused, match="schema gate"):
        promote(good_candidate(overlay.head, knowledge_type="speculation"), overlay.head)


def test_a_fabricated_quote_is_rejected_by_the_evidence_gate(world):
    """The single most important refusal in the system.

    A model that invents a plausible quote produces a record that looks
    exactly like a real one -- and every claim of evidence-grounding
    downstream becomes false at once.
    """
    overlay, _, _ = world
    invented = (EvidenceRef(artifact="workflow/approval_routes.yaml", locator="L37",
                            quote="value: 42", snapshot_id=overlay.head.snapshot_id),)
    with pytest.raises(PromotionRefused, match="evidence gate"):
        promote(good_candidate(overlay.head, evidence=invented), overlay.head)


def test_a_candidate_with_no_evidence_is_rejected(world):
    """I2."""
    overlay, _, _ = world
    with pytest.raises(PromotionRefused, match="no evidence"):
        promote(good_candidate(overlay.head, evidence=()), overlay.head)


def test_an_unknown_without_a_gap_id_is_rejected(world):
    """I5, on both paths: a gap cannot be promoted as knowledge, and a gap
    cannot be opened without a reason."""
    overlay, _, _ = world
    with pytest.raises(PromotionRefused, match="Gap ID"):
        promote(good_candidate(overlay.head, knowledge_type="gap"), overlay.head)

    with pytest.raises(PromotionRefused):
        open_gap("GAP-001", "")
    with pytest.raises(PromotionRefused):
        open_gap("", "a reason with no id")

    gap = open_gap("GAP-001", "no artifact states the emergency ceiling")
    assert gap.gap_id and gap.reason


def test_a_candidate_with_no_lineage_is_rejected(world):
    overlay, _, _ = world
    with pytest.raises(PromotionRefused, match="lineage"):
        promote(good_candidate(overlay.head, derived_from=()), overlay.head)


def test_confidence_outside_the_range_is_rejected(world):
    overlay, _, _ = world
    with pytest.raises(PromotionRefused, match="confidence"):
        promote(good_candidate(overlay.head, confidence=1.4), overlay.head)


def test_the_gates_run_in_order(world):
    """Schema before evidence.

    A malformed candidate has no reliable evidence field to check, and
    reporting "evidence invalid" for something that was never the right
    shape sends the reader hunting in the wrong place.
    """
    overlay, _, _ = world
    broken = good_candidate(overlay.head, knowledge_type="nonsense", evidence=())
    with pytest.raises(PromotionRefused, match="schema gate"):
        promote(broken, overlay.head)


# --- the proposer boundary -----------------------------------------------------


def test_with_no_proposer_the_model_is_empty_not_guessed(world):
    overlay, _, _ = world
    model = propose_and_promote(None, ["anything"], overlay.head)
    assert model.records == [] and model.rejected == []


def test_a_proposer_that_raises_produces_a_rejection_not_a_record(world):
    """R10 again: a provider failure must never become knowledge."""
    overlay, _, _ = world

    class Broken:
        def propose(self, request):
            raise RuntimeError("Vertex AI unavailable")

    model = propose_and_promote(Broken(), ["r"], overlay.head)
    assert model.records == []
    assert "RuntimeError" in model.rejected[0][1]


def test_a_proposer_that_abstains_is_recorded_as_abstaining(world):
    overlay, _, _ = world

    class Unsure:
        def propose(self, request):
            return None

    model = propose_and_promote(Unsure(), ["r"], overlay.head)
    assert model.rejected and "abstained" in model.rejected[0][1]


def test_malformed_output_is_recorded_rather_than_repaired(world):
    """Repairing it would hide how often the model produces nonsense --
    which is exactly the rate the negative evals at P9 measure."""
    overlay, _, _ = world

    class Sloppy:
        def propose(self, request):
            return Candidate(knowledge_type="rule", semantic_id="",
                             statement="something", evidence=(), derived_from=())

    model = propose_and_promote(Sloppy(), ["r"], overlay.head)
    assert model.records == []
    assert len(model.rejected) == 1
    assert "semantic_id" in model.rejected[0][1]


def test_a_well_formed_proposal_is_promoted(world):
    """The gates must let a good candidate through, or they are a wall."""
    overlay, _, _ = world

    class Good:
        def propose(self, request):
            return good_candidate(overlay.head)

    model = propose_and_promote(Good(), ["r"], overlay.head)
    assert len(model.records) == 1
    assert model.records[0].state is KnowledgeState.PROPOSED
    assert model.rejected == []


# --- recency and the DRIFT/CONFLICT boundary ------------------------------------


def test_without_a_recency_marker_a_disagreement_is_a_conflict(world):
    """The seed's hero rule has exactly this shape, and it is why the demo
    says CONFLICT rather than DRIFT."""
    overlay, ingestion, resolution = world
    model = build_from_groups(resolution, overlay.head, history=None)
    record = model.by_semantic_id("approval_threshold")[0]
    assert record.alignment is AlignmentState.CONFLICT
