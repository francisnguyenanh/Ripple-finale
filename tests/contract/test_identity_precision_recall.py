# -*- coding: utf-8 -*-
"""Identity resolution, measured both ways.

Recall answers "did it find the rule?". Precision answers "did it invent a
rule that is not there?". Only the second is catastrophic: a missed group
shows up as one rule reported twice, which a person notices immediately. A
wrong group is an impact analysis that is confidently and invisibly wrong,
and every number after it inherits the error.

So precision is asserted at 1.0 and recall is allowed to be imperfect --
the opposite of the usual instinct, and the reason it is written down here.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from core.change_intelligence.identity import (
    Candidate,
    Resolution,
    resolve,
    score,
)
from core.change_intelligence.ingest import ingest
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
MANIFEST = yaml.safe_load((SEED / "seed_manifest.yaml").read_text(encoding="utf-8"))

RULE_TO_SEMANTIC = {
    rule_id: rule["semantic_id"] for rule_id, rule in MANIFEST["rules"].items()
}


@pytest.fixture(scope="module")
def resolution() -> Resolution:
    built = build_baseline(SEED)
    baseline = InMemoryBaseline.from_artifacts(built["artifacts"])
    overlay = InMemoryOverlay(baseline, "SESSION-IDENTITY")
    ingestion = ingest(
        overlay.head,
        spreadsheet_reader=lambda raw, path, snap: list(read_xlsx(raw, path, snap)),
        # The project's own vocabulary, supplied by the project. Leaving it
        # out here would test a configuration no deployment uses.
        semantic_hints=MANIFEST.get("semantic_hints", {}),
    )
    return resolve(
        ingestion.candidates,
        rule_id_to_semantic_id=RULE_TO_SEMANTIC,
        known_semantic_ids=sorted(RULE_TO_SEMANTIC.values()),
    )


def truth_from(resolution: Resolution) -> dict[str, set[str]]:
    """Ground truth as sets of candidate refs, derived from the manifest.

    An earlier version keyed on (artifact, value) and scored the resolver as
    62% precise when it was in fact perfect. `application/config.py` states
    1,000,000 for BOTH BR-001 and BR-006, so that key is ambiguous and the
    later rule silently overwrote the earlier one -- the harness was wrong,
    not the thing it measured.

    The lesson is the one the product itself sells: an identifier that is
    not unique makes two different things look like one. So an explicit rule
    id wins, an unambiguous (artifact, value) is the fallback, and anything
    still ambiguous is EXCLUDED from scoring rather than guessed at. A
    measurement built on a guess measures the guess.
    """
    by_rule_id = {rule_id: rule["semantic_id"] for rule_id, rule in MANIFEST["rules"].items()}

    counts: dict[tuple[str, object], set[str]] = {}
    for rule_id, rule in MANIFEST["rules"].items():
        for rep in rule.get("representations", []):
            counts.setdefault((rep["artifact"], rep.get("value")), set()).add(
                rule["semantic_id"])
    unambiguous = {key: next(iter(v)) for key, v in counts.items() if len(v) == 1}

    truth: dict[str, set[str]] = {}
    for group in resolution.groups:
        for candidate in group.members:
            semantic_id = ""
            for rule_id in candidate.explicit_rule_ids:
                if rule_id in by_rule_id:
                    semantic_id = by_rule_id[rule_id]
                    break
            if not semantic_id:
                semantic_id = unambiguous.get((candidate.artifact, candidate.value), "")
            if semantic_id:
                truth.setdefault(semantic_id, set()).add(candidate.ref)
    return truth


# --- recall -------------------------------------------------------------------


def test_the_hero_rule_is_grouped_across_at_least_four_artifact_families(resolution):
    """Gate G3. Fewer than four families and the demo is a typo in one file."""
    group = resolution.by_semantic_id("approval_threshold")
    assert group is not None, "the hero rule was not grouped at all"
    assert len(group.families) >= 4, f"grouped only across {sorted(group.families)}"
    assert {"software", "workflow", "spreadsheet", "docs"} <= group.families


def test_the_hero_group_contains_the_disagreement(resolution):
    """A group that lost one of the two values has lost the conflict."""
    group = resolution.by_semantic_id("approval_threshold")
    assert group.values == {1_000_000, 500_000}


def test_a_rule_everyone_agrees_on_is_also_grouped(resolution):
    """Grouping must not depend on there being a disagreement to notice."""
    group = resolution.by_semantic_id("capex_finance_threshold")
    assert group is not None
    assert group.values == {5_000_000}
    assert len(group.families) >= 3


# --- precision ------------------------------------------------------------------


def test_two_different_rules_are_never_merged(resolution):
    """The failure R5 names as the worst in the phase.

    BR-001 and BR-004 are both amount thresholds in JPY with the same
    operator. Structurally they are near-identical; only the rule id tells
    them apart, which is exactly why tier 1 outranks tier 2.
    """
    approval = resolution.by_semantic_id("approval_threshold")
    capex = resolution.by_semantic_id("capex_finance_threshold")

    assert 5_000_000 not in approval.values, "a capex threshold was absorbed into BR-001"
    assert 1_000_000 not in capex.values, "an approval threshold was absorbed into BR-004"

    overlap = {c.ref for c in approval.members} & {c.ref for c in capex.members}
    assert not overlap, f"candidates claimed by both rules: {overlap}"


def test_precision_is_perfect_and_recall_is_reported(resolution):
    """Numbers, written down. Recall has no floor asserted here on purpose:
    the number belongs in the report, and a threshold in this test would
    become a target to tune towards rather than a measurement."""
    measured = score(resolution, truth_from(resolution))
    assert measured.precision == 1.0, (
        f"precision {measured.precision:.3f}; wrongly grouped: {measured.wrongly_grouped}"
    )
    assert measured.wrongly_grouped == ()
    assert measured.recall > 0.0


def test_every_group_can_say_how_it_knows(resolution):
    """A grouping with no stated basis is a claim, not a finding."""
    for group in resolution.groups:
        assert group.tier in {"deterministic", "structural", "bounded_semantic"}
        assert len(group.basis) > 20, f"{group.semantic_id} has no real basis: {group.basis!r}"


# --- abstention -------------------------------------------------------------------


def test_an_unattachable_value_becomes_a_gap_not_a_guess(resolution):
    """I5. Two candidates in this seed genuinely cannot be placed:

    the FAQ's "the figure is one million yen", whose sentence names no
    subject, and the hardcoded literal in emergency_purchase.py, which
    belongs to no rule any artifact declares. Both are correct answers.
    """
    assert resolution.unresolved, "nothing was unresolved -- the resolver is guessing"
    assert len(resolution.gaps) == len(resolution.unresolved)
    for gap in resolution.gaps:
        assert gap.gap_id.startswith("GAP-IDENTITY-")
        assert len(gap.reason) > 40


def test_the_seeded_hardcode_is_among_the_unresolved(resolution):
    """It belongs to no declared rule, so refusing to place it is right.

    Attaching it to BR-001 would be the appealing wrong answer: the value
    matches. The value matching is exactly what makes it invisible.
    """
    artifacts = {c.artifact for c in resolution.unresolved}
    assert "application/emergency_purchase.py" in artifacts


def test_with_no_proposer_tier_three_abstains_rather_than_degrading():
    """A system that starts guessing when the model is unavailable is worse
    than one that says it cannot answer."""
    candidates = [
        Candidate(ref="a", artifact="x.md", family="docs", locator="L1",
                  value=1_000_000, unit="JPY", text="some prose with no subject"),
    ]
    result = resolve(candidates, known_semantic_ids=["approval_threshold"])
    assert result.groups == []
    assert len(result.gaps) == 1
    assert "no semantic proposer configured" in result.gaps[0].reason


def test_a_low_confidence_proposal_is_refused():
    """Tier 3 proposes; the script decides. This is that decision."""

    class Timid:
        def propose(self, candidate, options):
            return (options[0], 0.40, "vaguely similar wording")

    candidates = [Candidate(ref="a", artifact="x.md", family="docs", locator="L1",
                            value=1_000_000, unit="JPY", text="prose")]
    result = resolve(candidates, known_semantic_ids=["approval_threshold"], proposer=Timid())
    assert result.groups == []
    assert "0.40" in result.gaps[0].reason


def test_a_confident_proposal_is_accepted_and_says_it_was_a_proposal():
    class Confident:
        def propose(self, candidate, options):
            return (options[0], 0.95, "states an approval limit in different words")

    candidates = [Candidate(ref="a", artifact="x.md", family="docs", locator="L1",
                            value=1_000_000, unit="JPY", text="prose")]
    result = resolve(candidates, known_semantic_ids=["approval_threshold"], proposer=Confident())
    assert len(result.groups) == 1
    assert result.groups[0].tier == "bounded_semantic"
    assert "0.95" in result.groups[0].basis


def test_a_proposer_that_raises_is_treated_as_abstention():
    """R10: live Gemini failing during judging must degrade to "I do not
    know", never to a wrong answer."""

    class Broken:
        def propose(self, candidate, options):
            raise RuntimeError("Vertex AI unavailable")

    candidates = [Candidate(ref="a", artifact="x.md", family="docs", locator="L1",
                            value=1, unit="JPY", text="prose")]
    result = resolve(candidates, known_semantic_ids=["approval_threshold"], proposer=Broken())
    assert result.groups == []
    assert "RuntimeError" in result.gaps[0].reason


def test_a_proposal_naming_an_unknown_rule_is_refused():
    """A model inventing a rule name must not create a rule."""

    class Inventive:
        def propose(self, candidate, options):
            return ("a_rule_that_does_not_exist", 0.99, "confident and wrong")

    candidates = [Candidate(ref="a", artifact="x.md", family="docs", locator="L1",
                            value=1, unit="JPY", text="prose")]
    result = resolve(candidates, known_semantic_ids=["approval_threshold"], proposer=Inventive())
    assert result.groups == []
    assert result.unresolved


# --- the scoring function itself ----------------------------------------------


def test_the_score_penalises_merging_two_rules():
    """Guards the measurement, not the resolver.

    A scorer that counted groups instead of pairs would give full marks to
    a system that produced the right NUMBER of groups with the wrong members
    in them.
    """
    merged = Resolution(groups=[
        type(resolve([], known_semantic_ids=[]).groups.__class__ and object)  # placeholder
    ]) if False else None

    from core.change_intelligence.identity import Group

    truth = {"rule_a": {"a1", "a2"}, "rule_b": {"b1", "b2"}}
    everything = Resolution(groups=[Group(
        semantic_id="everything",
        members=tuple(Candidate(ref=r, artifact="x", family="docs", locator="l")
                      for r in ["a1", "a2", "b1", "b2"]),
        tier="structural", basis="merged on purpose")])

    measured = score(everything, truth)
    assert measured.recall == 1.0, "it did group every true pair"
    assert measured.precision < 1.0, "and it must be punished for the false ones"
    assert measured.wrongly_grouped, "the offending cross-rule pairs must be named"


def test_the_score_penalises_splitting_one_rule():
    from core.change_intelligence.identity import Group

    truth = {"rule_a": {"a1", "a2"}}
    split = Resolution(groups=[
        Group(semantic_id="a-part-1",
              members=(Candidate(ref="a1", artifact="x", family="docs", locator="l"),),
              tier="structural", basis="split"),
        Group(semantic_id="a-part-2",
              members=(Candidate(ref="a2", artifact="x", family="docs", locator="l"),),
              tier="structural", basis="split"),
    ])
    measured = score(split, truth)
    assert measured.recall == 0.0
    assert measured.precision == 1.0, "splitting invents no false pair"
