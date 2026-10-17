"""Dependency invalidation / reanalysis after a committed decision
event, registered as job kind "invalidation".

Deterministic, zero-provider-call executor (same shape as
`infra.worker.relations.RelationGraphExecutor` -- no live model account/
budget config needed to run it). Consumes a committed decision event
(`decision_id`, `target` -- the decided candidate/claim id) and:

1. Scans the SAME candidate-id-membership graph `PostgresV2Store.
   knowledge_draft` already reconstructs (no dedicated chain-id column
   exists) for every COMPLETED `relation`/`finding` job that names
   `target` in its own `payload.candidate_ids` -- these are the real
   dependents that must be marked stale.
2. Marks each dependent stale via a durable, separate marker
   (`v2_stale_markers`) -- never a mutation of the dependent job's own
   `execution_result` (an outbox-terminal job is not a terminal
   DOMAIN fact once a new decision makes its content stale).
3. Schedules a deduplicated reanalysis job for each dependent: the SAME
   kind, over the SAME `candidate_ids`, via the existing job-creation path
   (`PostgresV2Store.create_job`), never a new creation path. The
   reanalysis job_id is derived deterministically from
   `(decision_id, dependent_job_id)`, so re-processing the SAME decision
   event twice (duplicate/out-of-order invalidation job, or a worker
   restart mid-processing) replays the same job_id through `create_job`'s
   own idempotency machinery and never double-schedules.
4. Records the causation chain (`v2_reanalysis_causation`): decision_id ->
   this invalidation job -> each dependent's reanalysis job, so the chain is
   traceable and durable (survives a worker restart -- nothing here is
   in-memory).
5. Bounds the loop with `job_quota.load_reanalysis_budget` (a real,
   shared budget/call-limit ceiling). Exhausting the
   budget does not raise/loop silently -- it stops scheduling further
   dependents and records an explicit `non_convergence_reason` in the
   result so a human sees WHY some dependents were left unscheduled,
   instead of a silent partial run.

Unknown/missing graph coverage: a dependent that cannot be resolved to a
real, persisted job is simply absent from the scan (there is nothing to
mark stale that does not exist) -- this is recorded in the result's
`dependent_count` (0 is a valid, visible outcome, never hidden).
"""

from __future__ import annotations

from infra.worker import job_quota
from infra.worker.budget_config import BudgetExceeded
from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest,
    NonRetryableExecutionError, ProviderCallBudget,
)


class InvalidationExecutor:
    """See module docstring. Zero provider calls (`external = False`)."""

    external = False
    executor_id = "invalidation.dependency_scan_reanalysis.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        payload = ctx.job["payload"]
        decision_id = payload.get("decision_id")
        target = payload.get("target")
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise NonRetryableExecutionError(
                "decision_id_required", "payload.decision_id must be a non-empty string")
        if not isinstance(target, str) or not target.strip():
            raise NonRetryableExecutionError(
                "target_required", "payload.target must be a non-empty string")
        return ExecutionRequest(prompt=None,
                                schema={"type": "object", "executor": self.executor_id},
                                provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
               calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        tenant_id, workspace_id = job["tenant_id"], job["workspace_id"]
        payload = job["payload"]
        decision_id = payload["decision_id"]
        target = payload["target"]
        invalidation_job_id = job["job_id"]
        store = ctx.store

        # Crash-redelivery idempotency: a reanalysis job this SAME decision
        # already scheduled (payload.caused_by_decision_id == decision_id) is this
        # decision's OUTPUT, never its input. Without this, a worker that died after
        # creating the reanalysis job but before its causation row would, on redelivery,
        # see that (already re-run) job as a fresh dependent and schedule a second one.
        dependents = [d for d in store.find_dependent_jobs_for_candidate(
                          tenant_id, workspace_id, target)
                      if (d.get("payload") or {}).get("caused_by_decision_id") != decision_id]

        already_caused = store.causation_for_decision(tenant_id, workspace_id, decision_id)
        budget = job_quota.load_reanalysis_budget()
        # Idempotent scheduling budget: dependents already covered by a prior
        # (possibly duplicate/out-of-order) run of this SAME decision event
        # never count against the ceiling again -- only genuinely new work
        # can exhaust it.
        new_dependents = [d for d in dependents if d["job_id"] not in already_caused]

        scheduled: list[dict] = []
        stale_marked: list[str] = []
        deferred: list[str] = []
        non_convergence_reason = None

        for dependent in dependents:
            dep_job_id = dependent["job_id"]
            if store.mark_job_stale(tenant_id, workspace_id, dep_job_id, decision_id):
                stale_marked.append(dep_job_id)

        for i, dependent in enumerate(new_dependents):
            dep_job_id, dep_kind = dependent["job_id"], dependent["kind"]
            if len(already_caused) + len(scheduled) >= budget:
                deferred.append(dep_job_id)
                non_convergence_reason = (
                    f"reanalysis budget exhausted: {budget} reanalysis job(s) already "
                    f"scheduled/attempted for decision {decision_id!r}; "
                    f"{len(new_dependents) - i} dependent(s) left unscheduled "
                    "(remaining scope reported, not silently dropped)")
                continue
            reanalysis_job_id = f"reanalysis-{decision_id}-{dep_job_id}"
            reanalysis_payload = dict(dependent["payload"])
            reanalysis_payload["caused_by_decision_id"] = decision_id
            reanalysis_payload["caused_by_job_id"] = dep_job_id
            head = store.head(tenant_id, workspace_id)
            try:
                store.create_job(
                    tenant_id, workspace_id, reanalysis_job_id, dep_kind,
                    reanalysis_payload, expected_version=head.version,
                    idempotency_key=f"job-create-{reanalysis_job_id}",
                    budget_reservation=job_quota.reservation_for_system_job(dep_kind))
            except BudgetExceeded:
                deferred.extend(d["job_id"] for d in new_dependents[i:])
                non_convergence_reason = (f"project cost cap reached: {len(new_dependents) - i} dependent(s) left unscheduled "
                                          "(remaining scope reported, not silently dropped)")
                break
            store.record_reanalysis_causation(
                tenant_id, workspace_id, decision_id=decision_id,
                invalidation_job_id=invalidation_job_id,
                dependent_job_id=dep_job_id, reanalysis_job_id=reanalysis_job_id)
            scheduled.append({"dependent_job_id": dep_job_id,
                              "reanalysis_job_id": reanalysis_job_id, "kind": dep_kind})

        result = {
            "executor_id": self.executor_id,
            "decision_id": decision_id, "target": target,
            "dependent_count": len(dependents),
            "stale_marked": stale_marked,
            "scheduled_reanalysis": scheduled,
            "already_caused": list(already_caused.values()),
            "deferred_budget_exhausted": deferred,
            "non_convergence_reason": non_convergence_reason,
        }
        return ExecutionOutcome(result=result, model_id=None)
