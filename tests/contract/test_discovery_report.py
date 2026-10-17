# -*- coding: utf-8 -*-
"""The first screen, and the claims it is allowed to make.

The plan is explicit that the alignment score leaves this screen, and the
reason is worth keeping in a test rather than a comment: a composite number
invites "by what standard?", and the attention that question costs would
otherwise have gone to a specific finding.

The other half of the file is the `high_impact` formula. The plan requires
it to be written out and testable, so it is tested term by term -- including
the case that made the first version wrong.
"""

from __future__ import annotations


import pathlib

import pytest
import yaml

from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from core.change_intelligence.discovery import discover
from core.change_intelligence.discovery_report import (
    HIGH_IMPACT_THRESHOLD,
    IMPACT_LEVELS,
    UNKNOWN_IMPACT,
    alignment_score,
    assess_impact,
    build_screen,
    render,
    stale_from_history,
    value_gap,
)
from core.change_intelligence.graph import build as build_graph
from core.change_intelligence.identity import resolve
from core.change_intelligence.ingest import ingest
from core.change_intelligence.knowledge import build_from_groups
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
MANIFEST = yaml.safe_load((SEED / "seed_manifest.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def world():
    built = build_baseline(SEED)
    overlay = InMemoryOverlay(InMemoryBaseline.from_artifacts(built["artifacts"]),
                              "SESSION-REPORT")
    ingestion = ingest(
        overlay.head,
        spreadsheet_reader=lambda raw, path, snap: list(read_xlsx(raw, path, snap)),
        semantic_hints=MANIFEST.get("semantic_hints", {}))
    resolution = resolve(
        ingestion.candidates,
        rule_id_to_semantic_id={r: v["semantic_id"] for r, v in MANIFEST["rules"].items()},
        known_semantic_ids=[v["semantic_id"] for v in MANIFEST["rules"].values()])
    model = build_from_groups(resolution, overlay.head, history=ingestion.records)
    report = discover(model, history=ingestion.records)
    graph = build_graph(ingestion, resolution)
    return ingestion, model, report, graph


@pytest.fixture(scope="module")
def screen(world):
    ingestion, model, report, graph = world
    return build_screen(report, model=model, records=ingestion.records, graph=graph)


# --- the score that is not on the screen -------------------------------------------


def test_the_alignment_score_is_computed(world):
    """It still exists. Removing it would answer the objection by deleting
    the capability, which is not the same thing."""
    _, model, _, _ = world
    aligned, total, score = alignment_score(model)
    assert total > 0
    assert 0.0 <= score <= 1.0
    assert score == aligned / total


def test_the_alignment_score_is_not_on_the_first_screen(screen):
    """"82% by what standard?" is the first question a reader asks, and it
    costs the attention a specific finding would have had."""
    assert "alignment" in screen.technical
    text = render(screen)
    assert "alignment" not in text.lower()
    assert "%" not in text


def test_the_headline_is_a_finding_not_a_summary(screen):
    """The sentence a reader repeats afterwards is the one they read
    first."""
    assert "approval_threshold" in screen.headline
    assert "500000" in screen.headline or "500,000" in screen.headline


def test_the_counts_are_things_a_reader_can_verify(screen):
    for key in ("undocumented", "stale", "conflicts", "unknown", "high_impact"):
        assert isinstance(screen.counts[key], int)
    assert screen.counts["undocumented"] == 3
    assert screen.counts["conflicts"] == 3
    assert screen.counts["conflicts_touching_money"] >= 1


# --- the formula ---------------------------------------------------------------------


def test_every_level_is_one_of_three_and_none_of_them_is_low(screen):
    """A finding on this screen has already cleared detection and carries
    evidence. LOW reads as permission to skip, and there is nothing here a
    reader has permission to skip."""
    assert "LOW" not in IMPACT_LEVELS
    for assessment in screen.impacts.values():
        assert assessment.level in IMPACT_LEVELS


def test_the_abandoned_exemption_is_not_ranked_beneath_notice(world, screen):
    """REGRESSION GUARD, and the case that made the first formula wrong.

    `strategic_customer_exemption` waives approval entirely. It was declared
    temporary in November 2020, raised by governance in 2021 with "no
    follow-up recorded", and states no number at all -- so every
    money-shaped term in the first sketch missed it and it came out LOW.
    """
    _, _, report, _ = world
    finding = [f for f in report.findings
               if f.semantic_id == "strategic_customer_exemption"][0]
    assessment = screen.impacts[finding.finding_id]

    assert assessment.level != "LOW"
    fired = {t.name for t in assessment.terms if t.fired}
    assert "declared_temporary" in fired
    assert "declared_unsynchronised" in fired
    assert "CHG-2020-011" in assessment.why


def test_the_hero_conflict_ranks_first(screen):
    assert screen.ranked
    first = screen.ranked[0]
    assert first.semantic_id == "approval_threshold"
    assert screen.impacts[first.finding_id].level == "HIGH"


def test_each_term_of_the_formula_is_named_and_auditable(world, screen):
    """A ranking a reader cannot audit is one they will either trust blindly
    or ignore, and both are worse than an arbitrary order."""
    _, _, report, _ = world
    expected = {"families", "money", "compliance", "value_gap",
                "declared_temporary", "declared_unsynchronised", "reach"}
    for finding in report.findings:
        assessment = screen.impacts[finding.finding_id]
        assert {t.name for t in assessment.terms} == expected
        assert assessment.points == sum(1 for t in assessment.terms if t.fired)
        for term in assessment.terms:
            assert term.detail


def test_the_threshold_is_a_written_constant(world, screen):
    _, _, report, _ = world
    for finding in report.findings:
        assessment = screen.impacts[finding.finding_id]
        if assessment.level == "HIGH":
            assert assessment.points >= HIGH_IMPACT_THRESHOLD


def test_an_unmeasured_term_makes_the_verdict_unknown_not_reassuring(world):
    """I5 in the place it is easiest to forget. Counting an unmeasured term
    as zero is how a finding nobody assessed comes out looking safe."""
    _, _, report, _ = world
    narrow = [f for f in report.findings
              if f.semantic_id == "emergency_purchase_ceiling"][0]

    with_graph = assess_impact(narrow, graph=None)
    assert with_graph.level == UNKNOWN_IMPACT
    assert "reach" in with_graph.unmeasured
    assert "not measured" in render_terms(with_graph)


def render_terms(assessment) -> str:
    return " ".join(t.detail for t in assessment.terms)


def test_the_value_gap_of_a_non_numeric_conflict_is_zero_not_an_error(world):
    """A scope or existence mismatch has no value gap and is not thereby
    unimportant. It simply does not score on this term."""
    _, _, report, _ = world
    finding = [f for f in report.findings
               if f.semantic_id == "strategic_customer_exemption"][0]
    assert value_gap(finding) == 0.0


# --- staleness --------------------------------------------------------------------


def test_stale_means_an_intention_nobody_carried_out(world):
    """Not "older". The tempting definition is `pick_newest` under another
    name, and it would decide every conflict in the product by date."""
    ingestion, _, _, _ = world
    stale = stale_from_history(ingestion.records)
    assert stale
    ids = {s.change_id for s in stale}
    assert "CHG-2022-006" in ids, "the unsynchronised approval-matrix change"
    assert "CHG-2020-011" in ids, "the temporary exemption that was never reverted"
    for item in stale:
        assert item.author and item.date
        assert "temporary" in item.why or "separately" in item.why


def test_no_representation_is_called_stale_for_being_older(world):
    """FAILURE INJECTION. Every stale entry must name a change that said
    something about itself -- none may be produced by comparing dates."""
    ingestion, _, report, _ = world
    stale = stale_from_history(ingestion.records)
    for item in stale:
        assert item.change_id.startswith("CHG-")

    from core.change_intelligence import discovery_report

    # Checked as ATTRIBUTES, not by grepping the source. The module
    # docstring names `pick_newest` in order to explain why it does not
    # exist, and a test that cannot tell a refusal from an implementation
    # would force that explanation to be deleted.
    for forbidden in ("pick_newest", "prefer_newest", "most_recent_wins",
                      "latest_wins", "resolve_by_date"):
        assert not hasattr(discovery_report, forbidden), (
            f"{forbidden} exists in discovery_report")


def test_stale_survives_an_empty_history(world):
    assert stale_from_history([]) == ()
    assert stale_from_history(None) == ()


# --- I9 -----------------------------------------------------------------------------


def test_the_screen_renders_deterministically(world):
    """I9. Two runs of one snapshot must be diffable, or nothing downstream
    can tell a change in the enterprise from a change in the renderer."""
    ingestion, model, report, graph = world
    first = render(build_screen(report, model=model, records=ingestion.records,
                                graph=graph))
    second = render(build_screen(report, model=model, records=ingestion.records,
                                 graph=graph))
    assert first == second


def test_nothing_on_this_screen_came_from_a_model():
    """The counts, the ranking and the headline are all script decisions.
    A model narrates a single finding at explanation time; it does not decide what a
    reader sees first."""
    import inspect

    from core.change_intelligence import discovery_report

    source = inspect.getsource(discovery_report)
    for forbidden in ("proposer", "vertex", "explain(", "llm"):
        assert forbidden not in source.lower()


def test_the_rendering_names_the_unmeasured_terms(world):
    """A reader must be able to see that a verdict was reached on partial
    inputs."""
    ingestion, model, report, _ = world
    text = render(build_screen(report, model=model, records=ingestion.records,
                               graph=None))
    assert "was not measured" in text
    assert UNKNOWN_IMPACT in text
