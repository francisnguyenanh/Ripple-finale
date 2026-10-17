"""Declarative v2 API registry; no endpoint or success stub.

Transport adapters must authenticate the principal, resolve workspace scope,
and feed this metadata to domain services. Registry entries define the public
surface and a common request envelope; persistence/idempotency is handled by the store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


ERROR_CODES = frozenset({
    "forbidden", "stale_revision", "invalid_evidence", "unresolved_gap",
    "unsupported_format", "unsupported_simulation", "provider_unavailable",
    "budget_exceeded", "runner_failed", "recovery_required", "idempotency_conflict",
    "invalid_request", "not_found", "knowledge_rebuilding",
})


@dataclass(frozen=True)
class Operation:
    permission: str
    mutates: bool
    async_job: bool = False
    response_fields: tuple[str, ...] = ("schema_version", "workspace_id", "revision")
    v1_reuse: str = "new v2 boundary"


def _op(permission: str, mutates: bool = False, async_job: bool = False,
        v1_reuse: str = "new v2 boundary") -> Operation:
    response = ("schema_version", "workspace_id", "operation_id", "status") \
        if async_job else ("schema_version", "workspace_id", "revision")
    return Operation(permission, mutates, async_job, response, v1_reuse)


OPERATIONS: Mapping[str, Operation] = {
    "workspace.create": _op("workspace.create", True),
    "workspace.select": _op("project.read"),
    "workspace.read": _op("project.read"),
    "membership.grant": _op("membership.manage", True),
    "membership.revoke": _op("membership.manage", True),
    "membership.list": _op("membership.manage"),
    "source.import.prepare": _op("source.import", True, True, "onboarding parsers"),
    "source.import.commit": _op("source.import", True, True, "ingest/identity"),
    "source.refresh.prepare": _op("source.import", True, True, "onboarding parsers"),
    "source.refresh.commit": _op("source.import", True, True, "ingest/identity"),
    "source.revisions.list": _op("project.read"),
    "source.revisions.show": _op("project.read"),
    "source.artifact.read": _op("project.read"),
    "source.artifact.export": _op("project.read"),
    "analysis.start": _op("analysis.run", True, True, "orchestrator"),
    "analysis.status": _op("project.read"),
    "analysis.cancel": _op("analysis.run", True),
    "evidence.show": _op("project.read"),
    "knowledge.show": _op("project.read"),
    "relations.show": _op("project.read"),
    "questions.list": _op("project.read"),
    "questions.answer": _op("knowledge.answer", True),
    "answers.confirm": _op("knowledge.confirm", True),
    "answers.reject": _op("knowledge.confirm", True),
    "knowledge.confirm": _op("knowledge.confirm", True),
    "conflicts.resolve": _op("conflict.resolve", True),
    "decisions.list": _op("project.read"),
    "decisions.supersede": _op("knowledge.confirm", True),
    "intent.parse": _op("intent.create", True, True, "intent parser"),
    "intent.confirm": _op("intent.create", True),
    "impact.analyse": _op("intent.create", True, True, "impact.analyse"),
    "simulation.run": _op("intent.create", True, True, "simulation.run"),
    "proposal.generate": _op("intent.create", True, True, "action_plan.plan"),
    "proposal.read": _op("project.read"),
    "proposal.revise": _op("intent.create", True),
    "review.run": _op("intent.create", True, True, "review board"),
    "review.read": _op("project.read"),
    "change.approve": _op("change.approve", True, False, "apply_gate"),
    "change.reject": _op("change.approve", True),
    "change.apply": _op("apply.execute", True, True, "apply_gate"),
    "verification.read": _op("project.read"),
    "verification.retry": _op("change.accept_result", True, True, "verification"),
    "result.accept": _op("change.accept_result", True),
    "result.request_changes": _op("change.accept_result", True),
    "audit.read": _op("project.read"),
    "lineage.read": _op("project.read"),
    "restore.preview": _op("source.restore", True, True),
    "restore.approve": _op("source.restore", True),
    "restore.apply": _op("apply.execute", True, True, "apply_gate"),
}


def validate_request(name: str, request: Mapping[str, object]) -> Operation:
    """Validate the transport-independent envelope; domain payload is typed separately."""
    if name not in OPERATIONS:
        raise ValueError("unknown v2 operation")
    op = OPERATIONS[name]
    required = ("schema_version", "workspace_id") + \
        (("idempotency_key", "expected_revision", "payload") if op.mutates else ())
    for field in required:
        value = request.get(field)
        if field == "payload":
            if not isinstance(value, dict):
                raise ValueError("payload must be an object")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required")
    if request["schema_version"] != "1.0.0":
        raise ValueError("unsupported schema_version")
    return op
