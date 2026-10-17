# -*- coding: utf-8 -*-
"""The P7 contracts, frozen before Phase 8 consumes them.

`docs/contracts/P7_FROZEN.md` states the definitions in prose. This file is
what stops the prose drifting away from the code, and each test names the
section it holds.

Shape and semantics, not figures. A test pinning a count would be a test
that the seed had not changed.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from core.change_intelligence import action_plan as plan_mod
from core.change_intelligence import apply_gate as gate_mod
from core.change_intelligence import changeset as cs_mod
from core.change_intelligence import impact as impact_mod
from core.change_intelligence import simulation as sim_mod
from core.change_intelligence import test_proposal as stale_mod
from core.change_intelligence.states import (
    CHANGESET_TRANSITIONS,
    ChangeSetState,
    TERMINAL_STATES,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
FROZEN = ROOT / "docs" / "contracts" / "P7_FROZEN.md"
PACKAGE = ROOT / "core" / "change_intelligence"


@pytest.fixture(scope="module")
def frozen_text():
    return FROZEN.read_text(encoding="utf-8")


# --- 1. schema ---------------------------------------------------------------


def test_the_frozen_document_exists_and_inherits_p6(frozen_text):
    assert "P6_FROZEN.md" in frozen_text
    assert (ROOT / "docs" / "contracts" / "P6_FROZEN.md").exists()


def test_the_changeset_carries_exactly_eleven_named_fields():
    assert len(cs_mod.FIELDS) == 11
    fields = set(cs_mod.ChangeSet.__dataclass_fields__)
    assert set(cs_mod.FIELDS) <= fields


def test_simulation_result_is_part_of_the_schema():
    assert "simulation_result" in cs_mod.FIELDS


def test_a_changeset_without_its_two_mandatory_fields_is_refused():
    for kwargs in ({"trigger": "", "base_snapshot": "S"},
                   {"trigger": "T", "base_snapshot": ""}):
        with pytest.raises(cs_mod.IncompleteChangeSet):
            cs_mod.ChangeSet(**kwargs)


def test_the_replay_chain_has_every_link_in_order():
    assert [name for name, _ in cs_mod.CHAIN] == [
        "base_snapshot", "trigger", "intent", "impact", "simulation",
        "proposal", "approval", "verification"]


# --- 2. identity -------------------------------------------------------------


def test_the_id_is_hashed_over_the_proposal_parts_only():
    assert cs_mod._PROPOSAL_PARTS == (
        "trigger", "intent", "base_snapshot", "affected_artifacts", "actions")
    for mutable in ("state", "history", "approval", "verification"):
        assert mutable not in cs_mod._PROPOSAL_PARTS, (
            f"{mutable} is workflow state; hashing it would make moving to "
            f"APPROVED invalidate the approval that caused the move")


def test_the_intent_version_is_part_of_the_identity():
    source = inspect.getsource(cs_mod._intent_key)
    assert "version" in source


# --- 3. state machine --------------------------------------------------------


def test_applied_has_exactly_one_outgoing_edge_and_it_is_verified():
    assert CHANGESET_TRANSITIONS[ChangeSetState.APPLIED] == frozenset(
        {ChangeSetState.VERIFIED}), (
        "I14: an apply that nothing checked must not reach a finished state")


def test_applied_is_not_terminal():
    assert ChangeSetState.APPLIED not in TERMINAL_STATES


def test_verifying_without_a_report_is_refused():
    with pytest.raises(gate_mod.NotVerified):
        gate_mod.mark_verified(
            cs_mod.ChangeSet(trigger="T", base_snapshot="S",
                             state=ChangeSetState.APPLIED),
            actor="qa", timestamp="t", report=None)


def test_no_edge_skips_review():
    assert ChangeSetState.APPROVED not in CHANGESET_TRANSITIONS[
        ChangeSetState.PROPOSED]


# --- 4 and 5. approval -------------------------------------------------------


def test_the_six_approval_fields_are_frozen():
    assert gate_mod.APPROVAL_FIELDS == (
        "actor", "timestamp", "changeset_id", "proposal_version",
        "decision", "reason")


def test_only_approve_authorises():
    assert gate_mod.DECISIONS == ("approve", "reject", "request_changes")
    approve_only = [d for d in gate_mod.DECISIONS
                    if gate_mod.ApprovalRecord(
                        actor="a", timestamp="t", changeset_id="c",
                        proposal_version="v", decision=d,
                        reason="r").is_approval]
    assert approve_only == ["approve"]


def test_the_version_is_read_at_the_decision_and_never_refreshed():
    source = inspect.getsource(gate_mod.decide)
    assert "proposal_version=changeset.proposal_version" in source


def test_every_refusal_raises_rather_than_returning():
    for name in ("check_approved", "check_version", "check_snapshot"):
        function = getattr(gate_mod, name)
        tree = ast.parse(inspect.getsource(function))
        raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
        returns = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Return) and n.value is not None]
        assert raises, f"{name} does not raise"
        assert not returns, (
            f"{name} returns a value a caller may ignore; a boundary whose "
            f"failure mode is ignorable has already been crossed")


# --- 6 and 7. the apply boundary ---------------------------------------------


def test_the_apply_path_takes_no_escape_hatch():
    tree = ast.parse((PACKAGE / "apply_gate.py").read_text(encoding="utf-8"))
    function = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "apply_changeset")
    names = {a.arg for a in function.args.args + function.args.kwonlyargs}
    for banned in ("force", "skip_approval", "unsafe", "bypass",
                   "ignore_approval", "auto_confirm", "confirm"):
        assert banned not in names


def test_all_three_checks_still_run_in_apply():
    body = ast.unparse(next(
        n for n in ast.walk(ast.parse(
            (PACKAGE / "apply_gate.py").read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == "apply_changeset"))
    for check in ("check_approved(", "check_version(", "check_snapshot("):
        assert check in body


def test_the_stale_edit_is_refused_rather_than_reconciled():
    source = inspect.getsource(
        __import__("core.change_intelligence.structured_diff",
                   fromlist=["apply_edits"]).apply_edits)
    assert "raise StaleDiff" in source


def test_applying_part_of_an_approved_change_is_refused():
    source = inspect.getsource(gate_mod.apply_changeset)
    assert "applying part of an approved change" in source


# --- 8. performer semantics --------------------------------------------------


def test_the_performer_table_is_frozen():
    assert plan_mod.PERFORMER == {
        "WORKFLOW": "system", "DATA": "system", "DOC": "system",
        "CODE": "human", "TEST": "human", "API": "human",
        "COMMUNICATION": "human"}


def test_communication_is_one_of_the_seven():
    assert "COMMUNICATION" in plan_mod.ACTION_KINDS
    assert len(plan_mod.ACTION_KINDS) == 7


def test_apply_writes_no_human_action():
    source = inspect.getsource(gate_mod.apply_changeset)
    assert 'performer == "human"' in source


def test_apply_says_what_it_did_not_do():
    source = inspect.getsource(gate_mod.apply_changeset)
    assert "carried out by a person" in source, (
        "silence about the actions apply did not perform reads as 'all of "
        "it was done'")


def test_required_still_cannot_be_constructed_without_evidence():
    with pytest.raises(plan_mod.UnjustifiedAction):
        plan_mod.Action(kind="CODE", target="a.py", description="d",
                        necessity=plan_mod.REQUIRED, performer="human",
                        impact_evidence=(), reason="r")


def test_the_four_necessities_are_frozen():
    assert plan_mod.NECESSITY == ("required", "recommended", "optional",
                                  "unsupported")


# --- 9. detection is not generation ------------------------------------------


def test_the_stale_test_detector_writes_nothing():
    source = inspect.getsource(stale_mod)
    for banned in ("write_text", "write_bytes", "open("):
        assert banned not in source


def test_the_detector_reads_neither_marker_nor_naming_convention():
    source = inspect.getsource(stale_mod)
    assert "seed_outdated" not in source
    assert "_old_" not in source


def test_the_affected_range_is_half_open():
    """A test asserting the boundary is about to become correct."""
    source = inspect.getsource(stale_mod.stale_tests)
    assert "low <= v < high" in source


def test_detection_walks_the_syntax_tree():
    source = inspect.getsource(stale_mod)
    assert "ast.walk" in source
    assert "re.compile" not in source


# --- 10. introduced vs inherited ---------------------------------------------


def test_both_lists_are_still_separately_named():
    for name in ("new_inconsistencies", "inherited_inconsistencies"):
        assert hasattr(sim_mod.SimulationResult, name)


def test_the_planner_takes_them_as_separate_arguments():
    parameters = inspect.signature(plan_mod.plan).parameters
    assert "introduced" in parameters and "inherited" in parameters


def test_only_the_route_that_failed_to_move_is_blamed():
    source = inspect.getsource(plan_mod.plan)
    assert "before[handler] != outcome.outcome" in source, (
        "the planner no longer distinguishes the route that followed the "
        "change from the route that ignored it; both would be accused")


def test_a_route_with_no_impact_node_is_still_represented():
    source = inspect.getsource(plan_mod.plan)
    assert "set(inconsistent_modules) - matched_modules" in source, (
        "a route the simulation caught but impact has no node for would be "
        "dropped, and only the configuration change proposed")


def test_an_inherited_contradiction_is_never_attributed_to_the_change():
    source = inspect.getsource(plan_mod.plan)
    assert "NOT caused by it" in source


# --- 11. actionability -------------------------------------------------------


def test_an_abstract_rule_node_produces_no_action():
    source = inspect.getsource(plan_mod.plan)
    assert 'node.kind == "business_rule"' in source


def test_a_non_actionable_node_stays_in_the_impact_set():
    """Evidence and context, not promoted into the action list."""
    assert "business_rule" in impact_mod.OBJECT_KINDS


# --- 12. no recommendation ---------------------------------------------------


def test_the_review_module_offers_no_recommendation():
    """Scanned by AST, not by text.

    A text scan flags the module docstring that FORBIDS the phrase -- the
    same defect the P6 freeze caught in the hardcoded-figure guard. What
    matters is what can reach the screen, so only string literals that are
    not docstrings are examined.
    """
    from core.change_intelligence import review

    tree = ast.parse(inspect.getsource(review))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)}

    for node in ast.walk(tree):
        if (not isinstance(node, ast.Constant)
                or not isinstance(node.value, str)
                or id(node) in docstrings):
            continue
        text = node.value.lower()
        for phrase in ("we recommend", "safe to approve", "should approve",
                       "auto_approve", "recommended_action"):
            assert phrase not in text, (
                f"a string the screen can render says {phrase!r}; once the "
                f"product advises, a signature stops meaning the person "
                f"judged it")


def test_build_screen_computes_nothing_of_its_own():
    from core.change_intelligence import review

    source = inspect.getsource(review.build_screen)
    for banned in ("analyse(", "assess(", "run(", "plan("):
        assert banned not in source


# --- 13 and 14. lineage and the declared debt --------------------------------


def test_the_frozen_document_records_the_wiring_debt_boundary(frozen_text):
    assert "core/kernel/changeset.py" in frozen_text
    assert "P8-T03" in frozen_text


def test_the_frozen_document_covers_every_required_section(frozen_text):
    for heading in ("ChangeSet schema", "State machine", "Approval invariants",
                    "Stale approval", "Snapshot rule", "Apply boundary",
                    "Action performer semantics",
                    "Detection is not generation",
                    "Introduced vs inherited contradictions",
                    "Actionability", "Benchmark lineage"):
        assert heading in frozen_text, f"{heading} missing"


def test_direct_indirect_unknown_are_inherited_not_redefined(frozen_text):
    assert impact_mod.TIERS == ("DIRECT", "INDIRECT", "UNKNOWN")
    assert "P6_FROZEN.md" in frozen_text
