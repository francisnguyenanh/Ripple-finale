# -*- coding: utf-8 -*-
"""The P8 contracts, frozen before Phase 9 consumes them.

`docs/contracts/P8_FROZEN.md` states them in prose; this stops the prose
drifting from the code. Same shape as the P6 and P7 freeze files, and each
test names the section it holds.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from core.change_intelligence import change_detector as detector_mod
from core.change_intelligence import events as events_mod
from core.change_intelligence import lineage as lineage_mod
from core.change_intelligence import orchestrator as orch_mod
from core.change_intelligence import verification as verify_mod
from core.change_intelligence.states import (
    CHANGESET_TRANSITIONS,
    AlignmentState,
    ChangeSetState,
)
from core.schema import event_registry

from tests.contract.test_verification import _executable_source

ROOT = pathlib.Path(__file__).resolve().parents[2]
FROZEN = ROOT / "docs" / "contracts" / "P8_FROZEN.md"


@pytest.fixture(scope="module")
def frozen_text():
    return FROZEN.read_text(encoding="utf-8")


# --- 1 and 2. the result schema ----------------------------------------------


def test_the_document_exists_and_inherits_the_earlier_freezes(frozen_text):
    for earlier in ("P6_FROZEN.md", "P7_FROZEN.md"):
        assert earlier in frozen_text
        assert (ROOT / "docs" / "contracts" / earlier).exists()


def test_verification_never_reads_the_changeset_state():
    assert ".state" not in inspect.getsource(verify_mod.verify), (
        "a state field is a declared fact, and this module accepts only "
        "what the artifact says")


def test_the_four_edit_outcomes_are_frozen():
    assert verify_mod.OUTCOMES == ("APPLIED", "MISSING", "DIVERGED",
                                   "UNREADABLE")


def test_unreadable_is_unknown_not_drift():
    """Nothing was observed to disagree. It still blocks a pass."""
    source = inspect.getsource(verify_mod._decide)
    index = source.index("UNREADABLE")
    assert "AlignmentState.UNKNOWN" in source[index:index + 400]


def test_the_six_dimensions_are_frozen():
    assert verify_mod.DIMENSIONS == ("workflow", "software", "data",
                                     "documents", "tests", "evidence")


def test_alignment_reuses_the_p6_vocabulary():
    """Four values, not a parallel set."""
    assert len(AlignmentState) == 4
    source = _executable_source(verify_mod)
    assert "AlignmentState." in source


def test_aligned_cannot_carry_open_problems():
    for kwargs in ({"unresolved": ["x"]}, {"blocked_by": ["tests_failing"]}):
        with pytest.raises(verify_mod.CannotVerify):
            verify_mod.VerificationResult(
                changeset_id="C", snapshot_before="A", snapshot_after="B",
                alignment=AlignmentState.ALIGNED, **kwargs)


def test_only_aligned_passes():
    for state in AlignmentState:
        result = verify_mod.VerificationResult(
            changeset_id="C", snapshot_before="A", snapshot_after="B",
            alignment=state)
        assert result.passed is (state is AlignmentState.ALIGNED)


# --- 3. snapshot relationship ------------------------------------------------


def test_verifying_a_snapshot_against_itself_is_refused():
    source = inspect.getsource(verify_mod.verify)
    assert "snapshot_before.snapshot_id == snapshot_after.snapshot_id" in source


def test_the_diff_is_the_only_source_of_what_actually_changed():
    source = inspect.getsource(verify_mod.check_unexpected)
    assert "diff_from" in source


# --- 4. lineage --------------------------------------------------------------


def test_the_version_is_derived_not_stored():
    assert "version" not in lineage_mod.VerifiedLineage.__dataclass_fields__


def test_the_baseline_is_version_zero():
    assert lineage_mod.BASELINE_VERSION == 0


def test_exactly_one_function_advances_the_version():
    advancers = [
        name for name, member in vars(lineage_mod.VerifiedLineage).items()
        if callable(member) and not name.startswith("__")
        and "self.entries.append" in inspect.getsource(member)]
    assert advancers == ["record_verified"], (
        f"{advancers} append to the lineage; one counter means one place "
        f"that moves it")


def test_the_increment_is_idempotent_on_the_snapshot():
    source = inspect.getsource(lineage_mod.VerifiedLineage.record_verified)
    assert "existing is not None" in source


def test_a_gap_in_the_chain_raises():
    source = inspect.getsource(lineage_mod.VerifiedLineage.record_verified)
    assert "LineageMismatch" in source


def test_failed_attempts_are_kept_with_their_reason():
    assert hasattr(lineage_mod.VerifiedLineage, "record_failure")
    assert "reason" in lineage_mod.FailedAttempt.__dataclass_fields__


def test_this_lineage_does_not_reach_into_the_other_one():
    source = _executable_source(lineage_mod)
    for symbol in ("make_ref", "ArtifactRef", "DEFAULT_REF_VERSION"):
        assert symbol not in source


# --- 5 and 6. intended and unexpected ----------------------------------------


def test_the_edit_check_is_three_way():
    """approved / old / anything else -- so MISSING and DIVERGED differ."""
    source = inspect.getsource(verify_mod.check_edits)
    assert "actual == edit.proposed" in source
    assert "actual == edit.expected_current" in source


def test_relatedness_is_membership_not_resemblance():
    """Docstrings stripped: the prose explaining the rule names the very
    words the rule forbids."""
    assert "moved - expected" in inspect.getsource(verify_mod.check_unexpected)
    source = _executable_source(verify_mod).lower()
    for banned in ("similar", "ratio(", "fuzz", "difflib"):
        assert banned not in source, (
            f"verification compares by {banned!r}; relatedness is membership "
            f"of the approved set, or an out-of-band edit gets absorbed the "
            f"moment it touches the same file")


# --- 7. reverse drift --------------------------------------------------------


def test_reverse_drift_is_a_conflict():
    source = inspect.getsource(verify_mod._decide)
    index = source.index("REVERSE_DRIFT")
    assert "AlignmentState.CONFLICT" in source[index:index + 300]


def test_reverse_drift_says_nothing_authorised_it():
    source = inspect.getsource(verify_mod.check_unexpected)
    assert "no ChangeSet" in source


# --- 8. no auto-repair -------------------------------------------------------


def test_nothing_in_verification_writes():
    tree = ast.parse(inspect.getsource(verify_mod))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("write", "clear", "write_text",
                                       "write_bytes")):
            pytest.fail(f"verification writes at line {node.lineno}")


def test_every_next_action_offers_at_least_two_options():
    source = inspect.getsource(verify_mod.next_actions)
    assert source.count("options=(") >= 3
    assert "LEGITIMISE, REVERT" in source


def test_neither_loop_direction_ends_at_a_repair():
    for name in ("run_reality_direction", "run_verification"):
        source = inspect.getsource(getattr(orch_mod.Orchestrator, name))
        for banned in ("overlay.write", "apply_changeset", "revert("):
            assert banned not in source, (
                f"{name} repairs; P8 detects and reports, a person decides")


# --- 9. the five blockers ----------------------------------------------------


def test_the_five_blockers_are_frozen():
    assert verify_mod.BLOCKERS == (
        "tests_failing", "evidence_stale", "intent_conflict_open",
        "artifact_stale", "approval_evidence_missing")


def test_the_approval_is_rechecked_rather_than_trusted():
    source = inspect.getsource(verify_mod.blockers_for)
    assert "proposal_version" in source
    assert "is_approval" in source


def test_all_applicable_blockers_are_reported_together():
    found = verify_mod.blockers_for(
        tests_failing=("t",), stale_evidence=("e",), open_conflicts=("c",),
        stale_artifacts=("a",))
    assert len(found) == 4, "the guard stops early and hides the rest"


# --- 10. states and events ---------------------------------------------------


def test_applied_to_verified_is_still_the_only_way_in():
    sources = [state for state, targets in CHANGESET_TRANSITIONS.items()
               if ChangeSetState.VERIFIED in targets]
    assert sources == [ChangeSetState.APPLIED]


def test_six_loop_triggering_and_three_internal():
    assert len(event_registry.loop_triggering()) == 6
    assert len(event_registry.internal()) == 3
    assert len(event_registry.known()) == 9


def test_no_module_restates_the_classification():
    tree = ast.parse(inspect.getsource(events_mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in event_registry.internal(), (
                f"{node.value} is a literal in events.py; the classification "
                f"belongs in the schema and nowhere else")


def test_subscribing_an_internal_event_raises():
    bus = events_mod.EventBus()
    for name in event_registry.internal():
        with pytest.raises(events_mod.NotLoopTriggering):
            bus.subscribe(name, lambda e: None)


def test_dedup_is_by_key_not_by_id():
    source = inspect.getsource(events_mod.EventBus.publish)
    assert "event.dedup_key" in source
    assert "event_id" not in source


def test_a_duplicate_is_reported_as_one():
    source = inspect.getsource(events_mod.EventBus.publish)
    assert "True" in source and "was_duplicate" in inspect.getdoc(
        events_mod.EventBus.publish)


# --- 11. scoping -------------------------------------------------------------


def test_a_missing_input_is_a_note_not_a_smaller_answer():
    source = inspect.getsource(detector_mod.scope_for)
    assert source.count("notes.append") >= 3


def test_an_artifact_the_graph_does_not_know_is_reported_unreachable():
    assert "unreachable" in detector_mod.Scope.__dataclass_fields__


def test_the_scope_carries_the_denominator():
    assert "total_artifacts" in detector_mod.Scope.__dataclass_fields__
    assert hasattr(detector_mod.Scope, "fraction")


# --- 12. I8 ------------------------------------------------------------------


def test_the_fact_guard_runs_before_any_work():
    source = inspect.getsource(orch_mod.Orchestrator.run_reality_direction)
    guard = source.index("guard_loop_never_touches_ir")
    for later in ("classify_change", "find_chunks_to_reindex"):
        assert source.index(later) > guard, (
            f"{later} runs before the Fact guard; a scope reaching the Fact "
            f"layer would be partly processed and then halted")


def test_a_fact_error_gap_leaves_the_loop():
    source = inspect.getsource(orch_mod.Orchestrator.correction_for)
    assert "fact_correction_flow" in source


# --- 13, 14, 15. authority, determinism, one semantics -----------------------


def test_the_orchestrator_holds_no_domain_verdict():
    source = _executable_source(orch_mod)
    for banned in ("AlignmentState.", "HIGH_AT", "MEDIUM_AT", "DIRECT",
                   "INDIRECT"):
        assert banned not in source, (
            f"the orchestrator names {banned}; two implementations of one "
            f"rule disagree eventually")


@pytest.mark.parametrize("module", [verify_mod, lineage_mod, events_mod,
                                    detector_mod, orch_mod])
def test_no_p8_module_reads_a_clock_or_mints_an_id(module):
    source = _executable_source(module)
    for banned in ("datetime.now", "time.time", "uuid", "random."):
        assert banned not in source, f"{module.__name__} uses {banned}"


def test_no_p8_module_asks_a_model_for_a_verdict():
    for module in (verify_mod, lineage_mod, orch_mod):
        source = _executable_source(module).lower()
        for banned in ("proposer", "gemini", "llm"):
            assert banned not in source, f"{module.__name__} reaches for {banned}"


# --- 16, 17, 18. the decisions -----------------------------------------------


def test_h1_is_recorded_as_resolved(frozen_text):
    assert "H1 — resolved" in frozen_text
    assert (ROOT / "docs" / "verification"
            / "h1_and_wiring_decisions.md").exists()


def test_the_frozen_document_states_which_debts_were_paid(frozen_text):
    for module in ("fact_correction", "reindex", "loop_controller",
                   "impact_graph"):
        assert module in frozen_text


def test_the_frozen_document_covers_every_required_section(frozen_text):
    for heading in ("Verification result schema", "Snapshot relationship",
                    "Lineage and version semantics",
                    "Intended-change verification",
                    "Unexpected-change detection", "Reverse drift",
                    "No auto-repair", "State transitions and events",
                    "Human authority", "Determinism",
                    "H1 — resolved", "P8-T03 wiring ownership",
                    "Benchmark lineage"):
        assert heading in frozen_text, f"{heading} missing"
