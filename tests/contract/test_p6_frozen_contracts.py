# -*- coding: utf-8 -*-
"""The P6 contracts, frozen before Phase 7 consumes them.

`docs/contracts/P6_FROZEN.md` states the definitions in prose. This file is
what stops the prose drifting away from the code. Every test here maps to a
numbered section of that document.

These are deliberately *shape and semantics* tests, not figure tests. A test
that pinned `recall == 1.00` would be a test that the seed had not changed,
which is a different and much less interesting claim.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from core.change_intelligence import impact as impact_mod
from core.change_intelligence import risk as risk_mod
from core.change_intelligence import simulation as sim_mod

ROOT = pathlib.Path(__file__).resolve().parents[2]
FROZEN = ROOT / "docs" / "contracts" / "P6_FROZEN.md"


# --- 1. what the metrics claim ---------------------------------------------


def test_the_frozen_document_exists_and_scopes_its_own_metrics():
    text = FROZEN.read_text(encoding="utf-8")
    assert "not** proof of general correctness" in text, (
        "the frozen contract must say what the 1.00 scores are measured "
        "against; a bare 1.00 reads as a claim about the world")
    assert "answer-key layer" in text


def test_the_answer_key_is_not_generated_from_the_engine():
    """The 24 scenarios stay an independent layer.

    If the corpus ever gains a generator, the key stops checking the engine
    and starts describing it.
    """
    corpus = ROOT / "reference_system" / "scenarios" / "corpus.yaml"
    assert corpus.exists()
    generators = list((ROOT / "reference_system" / "scenarios").rglob("*.py"))
    assert not generators, (
        f"{generators} would regenerate the hand-written answer key from "
        f"code; the key must stay hand-written (P6 freeze item 1)")


# --- 2. introduced vs inherited ---------------------------------------------


def test_introduced_and_inherited_contradictions_are_separately_named():
    for name in ("new_inconsistencies", "inherited_inconsistencies"):
        assert hasattr(sim_mod.SimulationResult, name), (
            f"{name} is part of the frozen P7 entry contract; a consumer "
            f"that has to subtract one list from another will eventually "
            f"present an inherited defect as one the change caused")


def test_the_two_lists_are_disjoint_by_construction():
    """A case cannot be both. Otherwise "introduced" is not a claim."""
    source = inspect.getsource(sim_mod.SimulationResult.new_inconsistencies)
    assert 'not c.disagreeing_handlers("current")' in source


def test_inherited_does_not_require_the_shadow_to_disagree():
    source = inspect.getsource(sim_mod.SimulationResult.inherited_inconsistencies)
    assert '"current"' in source and '"shadow"' not in source


# --- 3. DIRECT / INDIRECT / UNKNOWN ------------------------------------------


def test_the_three_tiers_are_exactly_these_three():
    assert impact_mod.TIERS == ("DIRECT", "INDIRECT", "UNKNOWN")


def test_unknown_stays_a_valid_placement():
    node = impact_mod.ImpactNode(
        identifier="docs/unclear.md", kind="document",
        tier=impact_mod.UNKNOWN, reason="no evidence places this")
    assert node.tier == "UNKNOWN"


def test_only_operative_kinds_can_be_direct():
    """The frozen boundary: carrying the value, not mentioning the rule."""
    assert impact_mod._OPERATIVE_KINDS == {
        "workflow_config", "spreadsheet", "software"}


@pytest.mark.parametrize("kind", ["document", "test", "role", "notification"])
def test_a_document_or_test_is_never_promoted_to_direct(kind):
    assert kind not in impact_mod._OPERATIVE_KINDS, (
        f"{kind} artifacts may need updating, but they are not where the "
        f"change is made (P6 freeze item 3)")


def test_a_node_still_cannot_exist_without_a_reason():
    with pytest.raises(impact_mod.IncompleteImpact):
        impact_mod.ImpactNode(identifier="x", kind="document",
                              tier="DIRECT", reason="")


# --- 4. risk ------------------------------------------------------------------


def test_nothing_measured_is_never_high():
    assessment = risk_mod.assess()
    assert assessment.level == risk_mod.MEDIUM, (
        "a risk assessment computed on no input reported "
        f"{assessment.level}; that is a false alarm")
    assert assessment.points == 0


def test_an_unmeasured_dimension_contributes_no_points():
    assessment = risk_mod.assess()
    unmeasured = [d for d in assessment.dimensions if not d.measured]
    assert unmeasured
    assert all(d.is_floor_only for d in unmeasured)
    assert assessment.points == sum(
        d.points for d in assessment.dimensions if d.measured)


def test_risk_reports_known_unknown_evidence_and_reason():
    assessment = risk_mod.assess()
    assert assessment.dimensions
    assert all(d.detail for d in assessment.dimensions)
    assert assessment.floors, (
        "an unmeasured assessment must say what it did not look at")
    assert isinstance(assessment.why, str)


def test_risk_offers_no_recommendation_field():
    """A level is not a decision. Nothing here may become one."""
    banned = ("recommendation", "recommended_action", "should_approve",
              "auto_approve", "verdict", "decision")
    for name in banned:
        assert not hasattr(risk_mod.RiskAssessment, name), (
            f"RiskAssessment.{name} would let a downstream phase turn a "
            f"level into an automatic decision (P6 freeze item 4)")


def test_the_dimensions_are_frozen():
    assert risk_mod.DIMENSIONS == (
        "scope", "customer_impact", "financial_impact",
        "compliance_exposure", "workflow_disruption", "unknown")


# --- 5. handler collision -----------------------------------------------------


def test_handlers_are_discovered_by_signature_not_by_name():
    assert sim_mod._HANDLER_PARAMS == ("request", "config")
    source = inspect.getsource(sim_mod.discover_handlers)
    assert "signature" in source


def test_a_collision_requires_both_routes_to_accept_the_input():
    """A route that declines an input is not in disagreement about it."""
    source = inspect.getsource(sim_mod.CaseResult.disagreeing_handlers)
    assert "error" in source, (
        "a handler that raised or declined must be excluded before two "
        "answers are called a disagreement (P6 freeze item 5)")


def test_a_collision_requires_materially_different_outcomes():
    source = inspect.getsource(sim_mod.CaseResult.disagreeing_handlers)
    assert "set(" in source or "len(" in source


def test_discovery_rejects_a_callable_of_the_wrong_shape():
    """The positive half of item 5: shape is the whole admission test."""
    import types

    module = types.ModuleType("probe")

    def right(request, config):
        return "ok"

    def wrong(request):
        return "ok"

    right.__module__ = wrong.__module__ = "probe"
    module.right, module.wrong = right, wrong
    found = {h.name for h in sim_mod.discover_handlers(module)}
    assert found == {"probe.right"}, (
        "discovery admitted a callable that is not a decision path, or "
        "rejected one that is; neither name was consulted")


# --- 6. the hard-coding guard -------------------------------------------------


def test_the_hardcoding_guard_walks_the_ast():
    guard = ROOT / "tests" / "e2e" / "test_whatif.py"
    tree = ast.parse(guard.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and "never_literals" in node.name:
            body = ast.unparse(node)
            assert "ast." in body, (
                "the no-hardcoded-figure guard must read the syntax tree; a "
                "text grep flags a docstring that EXPLAINS a number, which "
                "is the opposite of the defect (P6 freeze item 6)")
            return
    pytest.fail("the hardcoded-figure guard is missing")


# --- 7 and 8. the P7 entry contract ------------------------------------------


def test_the_frozen_document_lists_every_input_p7_consumes():
    text = FROZEN.read_text(encoding="utf-8")
    for concept in ("alignment state", "introduced contradictions",
                    "inherited contradictions", "impacted artifacts",
                    "deterministic risk", "evidence references",
                    "simulation results"):
        assert concept in text, f"{concept} missing from the P7 entry contract"


def test_the_state_transition_is_written_down():
    text = FROZEN.read_text(encoding="utf-8")
    assert ("Analyze -> Prepare ChangeSet -> Human Review -> "
            "Approve/Reject -> Verify") in text
