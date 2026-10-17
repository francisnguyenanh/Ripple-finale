"""Acceptance and boundary tests for the v2 lifecycle policy module."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

import core.change_intelligence.v2.lifecycle as lifecycle_module
from core.change_intelligence.v2.lifecycle import (
    _HUMAN_PERMISSIONS,
    _SERVICE_TARGETS,
    _TABLE,
    ApprovalBundle,
    AuthorizationError,
    LifecycleError,
    TransitionContext,
    approval_digest,
    authorize_transition,
    transition_table,
    validate_approval_at_execution,
)
from core.change_intelligence.v2.api_contract import ERROR_CODES, OPERATIONS, validate_request


def context(**changes):
    base = TransitionContext(
        actor_id="human-1", actor_kind="human", permissions=frozenset({
            "knowledge.answer", "knowledge.confirm", "conflict.resolve", "change.approve",
            "change.accept_result", "source.restore"}),
        current_permissions=frozenset({"knowledge.answer", "knowledge.confirm", "conflict.resolve", "change.approve",
                                       "change.accept_result", "source.restore"}),
        expected_revision="r1", current_revision="r1", policy_version="p1",
        current_policy_version="p1", decision_epoch=1, current_decision_epoch=1,
    )
    return replace(base, **changes)


def test_every_lifecycle_has_declared_edges_and_terminal_states():
    for machine in ("analysis", "candidate", "knowledge", "gap", "conflict", "change"):
        assert transition_table(machine)
    assert "COMPLETED" not in transition_table("change")["SOURCE_PUBLISHED"]


def test_ai_cannot_confirm_gap_or_approve_apply_or_accept_result():
    for machine, source, target in (
        ("gap", "ANSWER_PROPOSED", "RESOLVED"),
        ("change", "REVIEW_READY", "APPROVED"),
        ("change", "AWAITING_ACCEPTANCE", "COMPLETED"),
    ):
        with pytest.raises(AuthorizationError):
            authorize_transition(machine, source, target, context(actor_kind="ai"))


def test_ai_output_cannot_directly_write_even_nonhuman_state():
    with pytest.raises(AuthorizationError):
        authorize_transition("candidate", "PROPOSED", "VALIDATED", context(actor_kind="ai"))


def test_human_cannot_claim_service_validation_or_reject_candidate_without_permission():
    with pytest.raises(AuthorizationError):
        authorize_transition("candidate", "PROPOSED", "VALIDATED", context())
    with pytest.raises(AuthorizationError):
        authorize_transition("candidate", "PENDING_HUMAN", "REJECTED", context(
            current_permissions=frozenset()))


def test_answer_proposal_requires_answer_permission_and_does_not_resolve_gap():
    event = authorize_transition("gap", "OPEN", "ANSWER_PROPOSED", context())
    assert event.target == "ANSWER_PROPOSED"
    with pytest.raises(AuthorizationError):
        authorize_transition("gap", "OPEN", "ANSWER_PROPOSED", context(
            current_permissions=frozenset()))


def test_gap_answer_does_not_close_conflict_or_count_as_evidence():
    with pytest.raises(LifecycleError):
        authorize_transition("conflict", "OPEN", "VERIFIED_RESOLVED", context())
    with pytest.raises(LifecycleError):
        authorize_transition("gap", "ANSWER_PROPOSED", "RESOLVED", context(evidence_valid=False))


def test_exception_does_not_turn_conflict_into_aligned_or_verified():
    event = authorize_transition("conflict", "OPEN", "EXCEPTION_ACCEPTED", context())
    assert event.target == "EXCEPTION_ACCEPTED"
    with pytest.raises(LifecycleError):
        authorize_transition("conflict", "EXCEPTION_ACCEPTED", "VERIFIED_RESOLVED", context())


def test_complete_requires_machine_verification_human_acceptance_and_no_open_work():
    with pytest.raises(LifecycleError):
        authorize_transition("change", "AWAITING_ACCEPTANCE", "COMPLETED", context(open_required_work=("edit",)))
    with pytest.raises(LifecycleError):
        authorize_transition("change", "AWAITING_ACCEPTANCE", "COMPLETED", context(machine_verified=False))
    event = authorize_transition("change", "AWAITING_ACCEPTANCE", "COMPLETED", context())
    assert event.permission == "change.accept_result"


def test_stale_and_read_only_cannot_mutate():
    for changed in ({"current_revision": "r2"}, {"read_only": True}):
        with pytest.raises(LifecycleError):
            authorize_transition("change", "REVIEW_READY", "APPROVED", context(**changed))


def test_restore_does_not_restore_historic_acl():
    with pytest.raises(AuthorizationError):
        authorize_transition("change", "REVIEW_READY", "APPROVED", context(
            current_permissions=frozenset(), operation="source.restore"))


def test_approval_digest_binds_dependencies_review_and_tests():
    bundle = ApprovalBundle("proposal-hash", "source-hash", "km-hash", 1,
                            "dependencies-hash", "review-hash", "tests-hash", "p1")
    first = approval_digest(bundle)
    assert first == approval_digest(bundle)
    for field in ("dependencies_hash", "review_hash", "tests_hash", "policy_version"):
        assert approval_digest(replace(bundle, **{field: "changed"})) != first


def test_execution_rechecks_current_policy_permission_and_digest():
    bundle = ApprovalBundle("p", "s", "k", 1, "d", "r", "t", "p1")
    digest = approval_digest(bundle)
    validate_approval_at_execution(bundle, digest, context())
    with pytest.raises(AuthorizationError):
        validate_approval_at_execution(bundle, digest, context(current_permissions=frozenset()))
    with pytest.raises(LifecycleError):
        validate_approval_at_execution(bundle, digest, context(current_policy_version="p2"))
    with pytest.raises(LifecycleError):
        validate_approval_at_execution(bundle, "forged", context())


def test_api_registry_covers_all_contract_capabilities():
    for name in ("workspace.create", "membership.revoke", "source.import.prepare",
                 "source.import.commit", "analysis.start", "questions.answer",
                 "knowledge.confirm", "conflicts.resolve", "proposal.generate",
                 "change.approve", "change.apply", "result.accept",
                 "restore.preview", "restore.approve", "restore.apply"):
        assert name in OPERATIONS
    assert OPERATIONS["change.apply"].permission == "apply.execute"
    assert OPERATIONS["result.accept"].permission == "change.accept_result"


def test_api_mutation_envelope_requires_idempotency_and_expected_revision():
    request = {"schema_version": "1.0.0", "workspace_id": "w1",
               "idempotency_key": "k1", "expected_revision": "r1", "payload": {}}
    validate_request("change.approve", request)
    for missing in ("workspace_id", "idempotency_key", "expected_revision"):
        with pytest.raises(ValueError):
            validate_request("change.approve", {k: v for k, v in request.items() if k != missing})
    with pytest.raises(ValueError):
        validate_request("change.approve", {**request, "schema_version": "9.0.0"})


def test_api_json_schema_declares_mutation_job_and_error_contracts():
    path = Path(__file__).resolve().parents[2] / "docs/contracts/AI_CLOSED_LOOP_V2_API.schema.json"
    definitions = json.loads(path.read_text(encoding="utf-8"))["$defs"]
    mutation = definitions["mutationRequest"]
    assert {"idempotency_key", "expected_revision", "payload"} <= set(mutation["required"])
    assert {"operation_id", "status"} <= set(definitions["jobResponse"]["required"])
    assert set(definitions["error"]["properties"]["code"]["enum"]) == ERROR_CODES


def test_accepted_unknown_requires_reason_scope_owner_and_review_date():
    with pytest.raises(LifecycleError):
        authorize_transition("gap", "OPEN", "ACCEPTED_UNKNOWN", context())
    event = authorize_transition("gap", "OPEN", "ACCEPTED_UNKNOWN", context(
        unknown_reason="source does not specify", unknown_scope="branch-x",
        unknown_owner="human-1", unknown_review_at="2026-12-01T00:00:00Z"))
    assert event.target == "ACCEPTED_UNKNOWN"
    with pytest.raises(LifecycleError):
        authorize_transition("gap", "OPEN", "ACCEPTED_UNKNOWN", context(
            unknown_reason="unspecified", unknown_scope="branch-x",
            unknown_owner="human-1", unknown_review_at="not-a-date"))


def test_critical_unknown_still_blocks_patch_approval():
    with pytest.raises(LifecycleError):
        authorize_transition("change", "REVIEW_READY", "APPROVED", context(
            correctness_blocking_unknown=True))


def test_distinct_approver_policy_blocks_self_approval():
    with pytest.raises(AuthorizationError):
        authorize_transition("change", "REVIEW_READY", "APPROVED", context(
            requester_id="human-1", require_distinct_approver=True))


def test_transition_cartesian_product_refuses_every_undeclared_edge():
    for machine in ("analysis", "candidate", "knowledge", "gap", "conflict", "change"):
        table = transition_table(machine)
        for source in table:
            for target in table:
                if target not in table[source]:
                    with pytest.raises(LifecycleError):
                        authorize_transition(machine, source, target, context())


def test_analysis_cancel_authorized_for_human_with_analysis_run_permission():
    event = authorize_transition("analysis", "RUNNING", "CANCELLED", context(
        permissions=frozenset({"analysis.run"}),
        current_permissions=frozenset({"analysis.run"})))
    assert event.target == "CANCELLED"
    assert event.permission == "analysis.run"


def test_analysis_cancel_rejected_for_human_without_analysis_run_permission():
    with pytest.raises(AuthorizationError):
        authorize_transition("analysis", "RUNNING", "CANCELLED", context(
            permissions=frozenset(), current_permissions=frozenset()))


def test_knowledge_supersede_authorized_for_human_with_confirm_permission():
    event = authorize_transition("knowledge", "ACCEPTED", "SUPERSEDED", context())
    assert event.target == "SUPERSEDED"
    assert event.permission == "knowledge.confirm"


def test_knowledge_supersede_rejected_for_human_without_confirm_permission():
    with pytest.raises(AuthorizationError):
        authorize_transition("knowledge", "ACCEPTED", "SUPERSEDED", context(
            current_permissions=frozenset()))


def test_gap_reject_reopens_gap_for_human_with_confirm_permission():
    event = authorize_transition("gap", "ANSWER_PROPOSED", "OPEN", context())
    assert event.target == "OPEN"
    assert event.permission == "knowledge.confirm"


def test_gap_reject_rejected_for_human_without_confirm_permission():
    with pytest.raises(AuthorizationError):
        authorize_transition("gap", "ANSWER_PROPOSED", "OPEN", context(
            current_permissions=frozenset()))


def test_every_reachable_target_has_exactly_one_declared_authority():
    """Every (machine, target) pair in _TABLE must be resolvable to a human
    permission or a service-only authorization, never both, never neither.
    This is a structural guard: an edge added to
    _TABLE without a matching decision here must fail this test, not silently
    default to "any service actor" at runtime."""
    for machine, table in _TABLE.items():
        for source, targets in table.items():
            for target in targets:
                pair = (machine, target)
                in_human = pair in _HUMAN_PERMISSIONS
                in_service = pair in _SERVICE_TARGETS
                assert in_human or in_service, (
                    f"no declared authority for {machine} {source}->{target}")
                assert not (in_human and in_service), (
                    f"{machine} {source}->{target} is declared both human and service")


def test_service_target_removed_from_whitelist_is_refused_at_runtime(monkeypatch):
    """Failure injection: if _SERVICE_TARGETS silently loses an entry (drift
    that was previously undetected because _SERVICE_TARGETS was never read), authorize_transition must refuse the
    edge rather than fall back to accepting any service actor."""
    trimmed = frozenset(pair for pair in lifecycle_module._SERVICE_TARGETS
                         if pair != ("analysis", "QUEUED"))
    monkeypatch.setattr(lifecycle_module, "_SERVICE_TARGETS", trimmed)
    with pytest.raises(LifecycleError, match="no declared authority"):
        authorize_transition("analysis", "RUNNING", "QUEUED", context(
            actor_kind="service", permissions=frozenset(), current_permissions=frozenset()))
