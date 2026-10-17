# -*- coding: utf-8 -*-
"""Graph quality, counted by hand.

The plan is explicit that "the graph is not empty" is not a test. A missing
edge type costs impact recall, and it costs it invisibly: nothing goes red,
the impact set is simply smaller than the truth. So three artifacts are
picked, their edges counted by hand, and the graph asserted against the
count.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from core.change_intelligence.graph import (
    EDGE_TYPES,
    SEMANTIC_EDGE_TYPES,
    Edge,
    EnterpriseGraph,
    Node,
    build,
)
from core.change_intelligence.identity import resolve
from core.change_intelligence.ingest import ingest
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
MANIFEST = yaml.safe_load((SEED / "seed_manifest.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def graph() -> EnterpriseGraph:
    built = build_baseline(SEED)
    baseline = InMemoryBaseline.from_artifacts(built["artifacts"])
    ingestion = ingest(
        InMemoryOverlay(baseline, "SESSION-GRAPH").head,
        spreadsheet_reader=lambda raw, path, snap: list(read_xlsx(raw, path, snap)),
        semantic_hints=MANIFEST.get("semantic_hints", {}),
    )
    resolution = resolve(
        ingestion.candidates,
        rule_id_to_semantic_id={r: v["semantic_id"] for r, v in MANIFEST["rules"].items()},
        known_semantic_ids=[v["semantic_id"] for v in MANIFEST["rules"].values()],
    )
    return build(ingestion, resolution)


# --- integrity ----------------------------------------------------------------


def test_no_edge_points_at_a_node_that_does_not_exist(graph):
    """41 of 82 endpoints dangled in the inherited framework's real project.

    Every coverage metric computed over such a graph measures the empty set
    and then reports 100%.
    """
    dangling = graph.dangling_edges()
    assert dangling == [], f"{len(dangling)} dangling edges, first: {dangling[:3]}"


def test_an_edge_without_a_reason_cannot_be_constructed():
    with pytest.raises(ValueError, match="no reason"):
        Edge(src="a", dst="b", type="CALLS", reason="")


def test_an_edge_of_an_undeclared_type_cannot_be_constructed():
    with pytest.raises(ValueError, match="not one of"):
        Edge(src="a", dst="b", type="INFLUENCES", reason="invented")


# --- edge type coverage ----------------------------------------------------------


def test_at_least_nine_of_the_edge_types_are_present(graph):
    """Gate G3 asks for >= 9 of 11. Counted, not assumed."""
    present = {t for t, n in graph.counts().items() if n > 0}
    assert len(present) >= 9, f"only {len(present)} edge types present: {sorted(present)}"


def test_the_five_semantic_edge_types_that_carry_the_product(graph):
    """IMPLEMENTS / DOCUMENTS / CONFIGURES are what make "this rule moved,
    what else moves?" answerable. TESTS and EXEMPTS are checked separately
    because this seed exercises them more narrowly."""
    counts = graph.counts()
    for edge_type in ("IMPLEMENTS", "DOCUMENTS", "CONFIGURES"):
        assert counts[edge_type] > 0, f"no {edge_type} edges -- impact cannot cross families"


def test_contradicts_is_absent_at_this_stage(graph):
    """CONTRADICTS is a CONCLUSION, produced at P4.

    Drawing it here would mean the ingestion layer had decided two
    statements disagree, which is reconciliation's job and a human's call
    (I15).
    """
    assert graph.counts()["CONTRADICTS"] == 0


# --- counted by hand --------------------------------------------------------------


def test_the_workflow_file_configures_exactly_the_rules_it_names(graph):
    """Hand count of `workflow/approval_routes.yaml`.

    The file names five rules: four routes (BR-001, BR-002, BR-004, BR-005)
    and one exception (BR-006). All five are configured here.

    This test asserted TWO until P4, and its own docstring recorded the
    reason as though it were a property of the seed: the predicates on
    `customer_tier` and `supplier_risk` "state no value and therefore form
    no rule group". That was a limitation of ingestion, not a fact about the
    file -- `approver_role: NONE` for strategic customers is a business rule
    whether or not it carries a number. Hand-counting the FILE rather than
    the pipeline's output is what made the gap visible.
    """
    configures = [e for e in graph.edges("CONFIGURES")
                  if e.src == "artifact:workflow/approval_routes.yaml"]
    assert len(configures) == 5, [e.dst for e in configures]
    assert {e.dst for e in configures} == {
        "rule:approval_threshold", "rule:capex_finance_threshold",
        "rule:strategic_customer_exemption", "rule:high_risk_supplier_escalation",
        "rule:emergency_purchase_ceiling"}


def test_the_spreadsheet_documents_exactly_two_rules(graph):
    """Hand count of `data/approval_matrix.xlsx`.

    Three rows; BR-005 has no threshold value, so two rows state a rule.
    """
    documents = [e for e in graph.edges("DOCUMENTS")
                 if e.src == "artifact:data/approval_matrix.xlsx"]
    assert len(documents) == 2
    assert {e.dst for e in documents} == {
        "rule:approval_threshold", "rule:capex_finance_threshold"}


def test_the_sop_documents_both_thresholds_it_states(graph):
    """Hand count of `docs/procurement-sop.md`: the section 4 table states
    the approval threshold and the capex threshold."""
    documents = {e.dst for e in graph.edges("DOCUMENTS")
                 if e.src == "artifact:docs/procurement-sop.md"}
    assert documents == {"rule:approval_threshold", "rule:capex_finance_threshold"}


def test_every_semantic_edge_cites_where_it_read_the_claim(graph):
    """An impact edge with no evidence is an assertion (I2)."""
    for edge in graph.edges():
        if edge.type in SEMANTIC_EDGE_TYPES:
            assert edge.evidence, f"{edge.src} -{edge.type}-> {edge.dst} cites nothing"
            assert len(edge.reason) > 20


# --- the hero rule ------------------------------------------------------------------


def test_the_hero_rule_is_stated_by_at_least_four_artifact_families(graph):
    artifacts = graph.artifacts_stating("approval_threshold")
    families = {graph.node(a).family for a in artifacts if graph.node(a)}
    assert len(artifacts) >= 6, artifacts
    assert {"software", "workflow", "spreadsheet", "docs"} <= families


def test_an_artifact_is_counted_once_in_its_own_impact(graph):
    """An adapter may NOTICE a claim is about a rule; identity resolution
    DECIDES it. Keeping both drew two edges to one rule -- from the file and
    from the claim inside it -- and the file appeared twice in its own
    impact set, inflating every recall figure that followed.
    """
    direct = graph.impact_scope("approval_threshold")["direct"]
    assert len(direct) == len(set(direct))
    assert all(node.startswith("artifact:") for node in direct), direct


def test_direct_and_indirect_impact_are_reported_separately(graph):
    """Merging them is how precision dies: a neighbour four hops out would
    count the same as the artifact that literally states the rule."""
    scope = graph.impact_scope("approval_threshold", max_depth=3)
    assert scope["direct"] and scope["indirect"]
    assert not set(scope["direct"]) & set(scope["indirect"])


def test_a_deeper_walk_finds_more_indirect_and_the_same_direct(graph):
    """DIRECT is a fact about the rule; INDIRECT is a choice about depth.

    If DIRECT moved with depth, the two would not be different things.
    """
    shallow = graph.impact_scope("approval_threshold", max_depth=2)
    deep = graph.impact_scope("approval_threshold", max_depth=3)
    assert shallow["direct"] == deep["direct"]
    assert len(deep["indirect"]) > len(shallow["indirect"])


def test_impact_reaches_inside_an_affected_file(graph):
    """An artifact and the module parsed out of it arrive under two ids.

    Unlinked, the graph is two disconnected halves and impact stops at the
    file boundary -- able to say WHICH file states a rule, never which
    function inside it does.
    """
    indirect = graph.impact_scope("approval_threshold", max_depth=3)["indirect"]
    assert any(node.startswith("python:") for node in indirect), indirect


def test_a_rule_nobody_states_has_an_empty_impact(graph):
    """The check must be able to find nothing, or it finds everything."""
    scope = graph.impact_scope("a_rule_that_does_not_exist")
    assert scope == {"direct": [], "indirect": []}


# --- determinism -------------------------------------------------------------------


def test_building_twice_gives_the_same_graph(graph):
    built = build_baseline(SEED)
    baseline = InMemoryBaseline.from_artifacts(built["artifacts"])
    ingestion = ingest(
        InMemoryOverlay(baseline, "SESSION-GRAPH-2").head,
        spreadsheet_reader=lambda raw, path, snap: list(read_xlsx(raw, path, snap)),
        semantic_hints=MANIFEST.get("semantic_hints", {}),
    )
    resolution = resolve(
        ingestion.candidates,
        rule_id_to_semantic_id={r: v["semantic_id"] for r, v in MANIFEST["rules"].items()},
        known_semantic_ids=[v["semantic_id"] for v in MANIFEST["rules"].values()],
    )
    again = build(ingestion, resolution)

    assert [n.id for n in again.nodes()] == [n.id for n in graph.nodes()]
    assert [(e.type, e.src, e.dst) for e in again.edges()] == [
        (e.type, e.src, e.dst) for e in graph.edges()]
