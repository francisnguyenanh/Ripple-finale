"""Declarative lifecycle and authorization contract.

This domain guard is used by future v2 services before a CAS write. It does
not persist events or grant permissions; the server supplies current identity,
membership, policy and revision from authoritative stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from core.change_intelligence.v2.serialize import canonical_hash


class LifecycleError(ValueError):
    """An invalid edge, stale input, or unsatisfied transition precondition."""


class AuthorizationError(PermissionError):
    """The current principal has no authority for this transition."""


_TABLE: dict[str, dict[str, frozenset[str]]] = {
    "analysis": {
        "QUEUED": frozenset({"RUNNING", "CANCELLED"}),
        "RUNNING": frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "QUEUED"}),
        "SUCCEEDED": frozenset(), "PARTIAL": frozenset(),
        "FAILED": frozenset(), "CANCELLED": frozenset(),
    },
    "candidate": {
        "PROPOSED": frozenset({"VALIDATED", "REJECTED"}),
        "VALIDATED": frozenset({"PENDING_HUMAN", "REJECTED"}),
        "PENDING_HUMAN": frozenset({"CONFIRMED", "REJECTED"}),
        "CONFIRMED": frozenset(), "REJECTED": frozenset(),
    },
    "knowledge": {
        "DRAFT": frozenset({"VALIDATED"}),
        "VALIDATED": frozenset({"PENDING_HUMAN"}),
        "PENDING_HUMAN": frozenset({"ACCEPTED"}),
        "ACCEPTED": frozenset({"STALE", "SUPERSEDED"}),
        "STALE": frozenset({"SUPERSEDED"}), "SUPERSEDED": frozenset(),
    },
    "gap": {
        "OPEN": frozenset({"ANSWER_PROPOSED", "ACCEPTED_UNKNOWN"}),
        "ANSWER_PROPOSED": frozenset({"RESOLVED", "ACCEPTED_UNKNOWN", "OPEN"}),
        "RESOLVED": frozenset({"REOPENED"}),
        "ACCEPTED_UNKNOWN": frozenset({"REOPENED"}),
        "REOPENED": frozenset({"ANSWER_PROPOSED", "ACCEPTED_UNKNOWN"}),
    },
    "conflict": {
        "OPEN": frozenset({"DECISION_RECORDED", "EXCEPTION_ACCEPTED"}),
        "DECISION_RECORDED": frozenset({"REMEDIATION_PENDING", "EXCEPTION_ACCEPTED"}),
        "REMEDIATION_PENDING": frozenset({"VERIFIED_RESOLVED", "EXCEPTION_ACCEPTED"}),
        "VERIFIED_RESOLVED": frozenset(), "EXCEPTION_ACCEPTED": frozenset(),
    },
    "change": {
        "DRAFT": frozenset({"ANALYSED", "STALE"}),
        "ANALYSED": frozenset({"REVIEW_READY", "STALE"}),
        "REVIEW_READY": frozenset({"APPROVED", "STALE"}),
        "APPROVED": frozenset({"STAGED", "STALE"}),
        "STAGED": frozenset({"SOURCE_PUBLISHED", "FAILED", "RECOVERY_REQUIRED", "STALE"}),
        "SOURCE_PUBLISHED": frozenset({"REANALYSING", "RECOVERY_REQUIRED"}),
        "REANALYSING": frozenset({"MACHINE_VERIFIED", "FAILED", "RECOVERY_REQUIRED"}),
        "MACHINE_VERIFIED": frozenset({"AWAITING_ACCEPTANCE", "STALE"}),
        "AWAITING_ACCEPTANCE": frozenset({"COMPLETED", "STALE", "RECOVERY_REQUIRED"}),
        "COMPLETED": frozenset(), "STALE": frozenset({"DRAFT"}),
        "FAILED": frozenset({"RECOVERY_REQUIRED"}),
        "RECOVERY_REQUIRED": frozenset({"DRAFT"}),
    },
}

_HUMAN_PERMISSIONS = {
    ("candidate", "CONFIRMED"): "knowledge.confirm",
    ("candidate", "REJECTED"): "knowledge.confirm",
    ("knowledge", "ACCEPTED"): "knowledge.confirm",
    ("knowledge", "SUPERSEDED"): "knowledge.confirm",  # decisions.supersede (api_contract.py)
    ("gap", "RESOLVED"): "knowledge.confirm",
    ("gap", "ACCEPTED_UNKNOWN"): "knowledge.confirm",
    ("gap", "ANSWER_PROPOSED"): "knowledge.answer",
    ("gap", "OPEN"): "knowledge.confirm",  # answers.reject (api_contract.py)
    ("conflict", "DECISION_RECORDED"): "conflict.resolve",
    ("conflict", "EXCEPTION_ACCEPTED"): "conflict.resolve",
    ("change", "APPROVED"): "change.approve",
    ("change", "COMPLETED"): "change.accept_result",
    ("analysis", "CANCELLED"): "analysis.run",  # analysis.cancel (api_contract.py)
}

# Every remaining declared edge in _TABLE that is not a human-permission edge
# above. This is the enforced whitelist for service-only transitions: a pair
# absent from both this set and _HUMAN_PERMISSIONS raises LifecycleError in
# authorize_transition rather than silently accepting any service actor. Keep
# this hand-authored and in sync with _TABLE; adding a new lifecycle edge
# without deciding its authority here is a bug, not an omission to inherit.
_SERVICE_TARGETS = {
    ("analysis", "RUNNING"), ("analysis", "SUCCEEDED"),
    ("analysis", "PARTIAL"), ("analysis", "FAILED"), ("analysis", "QUEUED"),
    ("candidate", "VALIDATED"), ("candidate", "PENDING_HUMAN"),
    ("knowledge", "VALIDATED"), ("knowledge", "PENDING_HUMAN"), ("knowledge", "STALE"),
    ("gap", "REOPENED"),
    ("conflict", "REMEDIATION_PENDING"), ("conflict", "VERIFIED_RESOLVED"),
    ("change", "ANALYSED"), ("change", "STALE"), ("change", "REVIEW_READY"),
    ("change", "STAGED"), ("change", "SOURCE_PUBLISHED"), ("change", "FAILED"),
    ("change", "RECOVERY_REQUIRED"), ("change", "REANALYSING"),
    ("change", "MACHINE_VERIFIED"), ("change", "AWAITING_ACCEPTANCE"),
    ("change", "DRAFT"),
}


@dataclass(frozen=True)
class TransitionContext:
    actor_id: str
    actor_kind: str  # human, ai, service
    permissions: frozenset[str]
    current_permissions: frozenset[str]
    expected_revision: str
    current_revision: str
    policy_version: str
    current_policy_version: str
    decision_epoch: int
    current_decision_epoch: int
    read_only: bool = False
    evidence_valid: bool = True
    machine_verified: bool = True
    open_required_work: tuple[str, ...] = ()
    operation: str = ""
    current_membership_valid: bool = True
    approval_valid: bool = True
    requester_id: str = ""
    require_distinct_approver: bool = False
    unknown_reason: str = ""
    unknown_scope: str = ""
    unknown_owner: str = ""
    unknown_review_at: str = ""
    correctness_blocking_unknown: bool = False


@dataclass(frozen=True)
class TransitionEvent:
    machine: str
    source: str
    target: str
    actor_id: str
    permission: str | None


@dataclass(frozen=True)
class ApprovalBundle:
    proposal_hash: str
    source_hash: str
    knowledge_hash: str
    decision_epoch: int
    dependencies_hash: str
    review_hash: str
    tests_hash: str
    policy_version: str

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if name == "decision_epoch":
                if type(value) is not int or value < 0:
                    raise LifecycleError("decision_epoch must be a non-negative integer")
            elif not isinstance(value, str) or not value.strip():
                raise LifecycleError(f"{name} must be a non-empty string")


def transition_table(machine: str) -> Mapping[str, frozenset[str]]:
    try:
        return _TABLE[machine]
    except KeyError as exc:
        raise LifecycleError(f"unknown lifecycle: {machine}") from exc


def approval_digest(bundle: ApprovalBundle) -> str:
    """Bind the exact proposal, pinned inputs, dependencies, review and tests."""
    return canonical_hash({"ai_closed_loop_approval_v2": vars(bundle)})


def authorize_transition(machine: str, source: str, target: str,
                         context: TransitionContext) -> TransitionEvent:
    table = transition_table(machine)
    if source not in table or target not in table[source]:
        raise LifecycleError(f"forbidden transition: {machine} {source} -> {target}")
    if not context.actor_id.strip() or context.actor_kind not in {"human", "ai", "service"}:
        raise AuthorizationError("authenticated actor required")
    if context.actor_kind == "ai":
        raise AuthorizationError("model output cannot directly write lifecycle state")
    if context.read_only:
        raise LifecycleError("read-only workspace cannot mutate")
    if context.expected_revision != context.current_revision:
        raise LifecycleError("stale_revision")
    if (context.policy_version != context.current_policy_version
            or context.decision_epoch != context.current_decision_epoch):
        raise LifecycleError("stale policy or decision epoch")
    permission = _HUMAN_PERMISSIONS.get((machine, target))
    service_authorized_pair = False
    if machine == "candidate" and target == "REJECTED" and source != "PENDING_HUMAN":
        permission = None  # validator may reject a malformed candidate
        service_authorized_pair = True
    if context.operation == "source.restore":
        permission = "source.restore"
    if permission:
        if (machine, target) == ("gap", "ANSWER_PROPOSED") and context.actor_kind == "service":
            permission = None  # worker may record an AI proposal, never accept it
            service_authorized_pair = True
        elif context.actor_kind != "human" or not context.current_membership_valid:
            raise AuthorizationError("current human membership required")
        elif permission not in context.permissions or permission not in context.current_permissions:
            raise AuthorizationError(f"current permission {permission} required")
    if not permission:
        if not service_authorized_pair and (machine, target) not in _SERVICE_TARGETS:
            raise LifecycleError(f"no declared authority for {machine} {target}")
        if context.actor_kind != "service":
            raise AuthorizationError("service transition required")
    if target in {"RESOLVED", "CONFIRMED", "ACCEPTED", "VERIFIED_RESOLVED"} \
            and not context.evidence_valid:
        raise LifecycleError("invalid_evidence")
    if (machine, target) == ("gap", "ACCEPTED_UNKNOWN"):
        if not all((context.unknown_reason.strip(), context.unknown_scope.strip(),
                    context.unknown_owner.strip(), context.unknown_review_at.strip())):
            raise LifecycleError("accepted_unknown requires reason, scope, owner and review date")
        try:
            review_at = datetime.fromisoformat(context.unknown_review_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LifecycleError("accepted_unknown review date is invalid") from exc
        if review_at.tzinfo is None:
            raise LifecycleError("accepted_unknown review date needs a timezone")
    if (machine, target) == ("change", "APPROVED"):
        if context.correctness_blocking_unknown:
            raise LifecycleError("unresolved_gap")
        if context.require_distinct_approver and context.requester_id == context.actor_id:
            raise AuthorizationError("requester cannot approve under current policy")
    if (machine, target) == ("change", "STAGED"):
        if context.actor_kind != "service" or not context.approval_valid:
            raise AuthorizationError("apply service requires a current approval")
    if (machine, target) == ("change", "COMPLETED"):
        if not context.machine_verified or context.open_required_work:
            raise LifecycleError("open required work or missing machine verification")
    return TransitionEvent(machine, source, target, context.actor_id, permission)


def validate_approval_at_execution(bundle: ApprovalBundle, recorded_digest: str,
                                   context: TransitionContext) -> None:
    """Recheck at apply/publish time; a historical permission snapshot is insufficient."""
    if approval_digest(bundle) != recorded_digest:
        raise LifecycleError("approval digest conflict")
    if context.read_only or context.expected_revision != context.current_revision:
        raise LifecycleError("stale_revision")
    if bundle.policy_version != context.current_policy_version:
        raise LifecycleError("stale policy")
    if bundle.decision_epoch != context.current_decision_epoch:
        raise LifecycleError("stale decision epoch")
    if not context.current_membership_valid or "change.approve" not in context.current_permissions:
        raise AuthorizationError("approver permission revoked")
