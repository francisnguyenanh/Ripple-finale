"""What-if preview / risk-readiness report, job kind "whatif",
executor_id "whatif.preview.v1".

Contract:

  - Reuses the generic `POST /v2/jobs` / `GET /v2/jobs/{id}` route pair,
    same as `impact`/`simulation` (no second dispatch mechanism).
  - Zero provider calls (`external = False`): every input is an
    already-persisted "impact"/"simulation" job id, resolved via the SAME
    shared `ctx.store.job_record` primitive `ImpactAnalysisExecutor` already
    uses to resolve `relation_job_ids` -- no new read primitive invented.
  - Consumes an ImpactReport (`infra/worker/impact.py`'s
    `execution_result`: `existing_conflicts` -- real Finding dicts with
    `kind`/`severity`/`lifecycle_state`/`authority_required` -- and
    `coverage.complete`) and a SimulationReport
    (`infra/worker/simulation.py`'s `execution_result`: `status` in
    {EXECUTED, STATIC_ONLY, NOT_EXECUTABLE}, `coverage`
    known/excluded/unknown/malformed/duplicate, `outcomes`,
    `changed_count`). Neither report's job needs to actually exist in this
    executor's own module contract -- a missing/wrong-kind/not-completed
    reference is a real, named readiness blocker (see `_resolve_report`
    below), never a crash and never a silently-skipped input.
  - Readiness rules (deterministic, driven by
    `infra.worker.whatif_config.load_whatif_risk_config` -- see that
    module's docstring for the actual key/default table; this executor
    reads the loaded config, it does not hardcode a second copy of the
    thresholds):
      * any OPEN impact-report finding whose `severity` is in
        `config.blocking_severities` blocks readiness (an open
        critical GAP blocks readiness, never silently passed -- this
        schema's real highest severities, HIGH/MEDIUM by default, stand in
        for "critical"; see whatif_config's module docstring for why no
        literal "CRITICAL" level exists in this repo's finding schema);
      * (when `config.block_on_pending_manual_action`) any OPEN
        impact-report finding whose `authority_required` is set blocks
        readiness (a still-pending required manual action);
      * (when `config.block_on_partial_coverage`) an impact report with
        `coverage.complete is False`, OR a simulation report whose
        denominator carries any excluded/unknown/malformed/duplicate
        count, blocks readiness (partial coverage must never be
        silently upgraded to "complete");
      * an impact or simulation job id that does not resolve at all, is
        the wrong kind, or has not COMPLETED, is itself a named blocker
        (`report_unavailable:<reason>`) -- never treated as "no report,
        so nothing to check" (that would silently pass an under-evidenced
        preview).
    Zero blockers -> `readiness == "READY"`; one or more -> `readiness ==
    "BLOCKED"`, with every blocker's specific reason listed, never merely
    "unknown".
  - `payload.override_readiness` (or ANY other client-supplied
    readiness/safety field) is READ FROM THE PAYLOAD FOR REJECTION ONLY:
    if present, it is recorded verbatim under
    `execution_result["rejected_override_readiness"]` for audit, and is
    NEVER merged into, defaulted from, or allowed to short-circuit the
    deterministic evaluation above (the model never sets a safe/
    readiness flag; no AUTO_SAFE self-mint). The persisted
    `execution_result["readiness"]` is always the real computed verdict.
  - Numeric operational claims: this executor only ever states a numeric
    operational claim (`justified_edits`, `changed_count`,
    `affected_case_ids`) when the consumed simulation report's `status ==
    "EXECUTED"`. A `STATIC_ONLY`/`NOT_EXECUTABLE` simulation report yields
    a preview with `numeric_claims_present == False` and only a
    qualitative/coverage statement in `qualitative_summary`. Every
    numeric field this executor DOES emit carries a paired
    `source_report_id`/`computation_ref` anchor pointing at the real
    simulation job id it was read from
    -- never bare prose with no evidentiary anchor.
  - No-op change: when the simulation report is EXECUTED with
    `changed_count == 0` (or no `outcomes` at all) and the impact report
    carries no blocking findings, the preview reports `justified_edits ==
    []` and `blockers == []` explicitly -- not an omitted/default field
    that merely looks empty (every response always includes these keys).
  - Staleness: reuses the SAME predicate every other job
    kind already uses, verbatim, not a second mechanism.
    `payload.expected_decision_epoch` is checked at the ROUTE layer
    (`infra/v2_policy_routes.py::create_job`, the SAME code path already
    used for `kind == "impact"`, now also covering `kind == "whatif"`)
    BEFORE this executor ever runs -- a stale epoch never reaches a
    worker attempt at all. Like every job of every kind, the row this
    executor writes is pinned to `pinned_source_revision_id`/
    `pinned_knowledge_revision_id`/`pinned_decision_epoch` at creation
    time by `PostgresV2Store._create_job_core` (unchanged here);
    this executor denormalizes those three pins into
    `execution_result["pinned_input"]`, mirroring
    `ImpactAnalysisExecutor`/`SimulationExecutor` exactly, so a reader
    comparing that pin against `store.head()` at read time can always
    tell a persisted preview is stale, without a second staleness
    mechanism existing anywhere in this module.
  - Cross-tenant drill-down: this executor never resolves a job id
    across tenants -- `ctx.store.job_record`/`find_candidates_by_id` are
    themselves tenant/workspace scoped (same primitive every sibling
    executor already relies on), and the whatif preview's OWN read is
    served through the unmodified generic `GET /v2/jobs/{id}` route,
    which already denies a caller with no membership in the target
    workspace before any row is read. This executor adds no new
    cross-tenant surface.
"""

from __future__ import annotations

from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest,
    NonRetryableExecutionError, ProviderCallBudget,
)
from infra.worker.whatif_config import load_whatif_risk_config


def _resolve_report(ctx: ExecutionContext, job_id, *, expected_kind: str):
    """Resolve an already-persisted job's `execution_result`, tenant/
    workspace-scoped, via the SAME `ctx.store.job_record` primitive
    `ImpactAnalysisExecutor` already uses for `relation_job_ids` (no
    second read primitive). Returns `(execution_result_or_None, reason)`;
    `reason` is None only when a real, COMPLETED, right-kind report was
    found."""
    tenant_id, workspace_id = ctx.job["tenant_id"], ctx.job["workspace_id"]
    if not isinstance(job_id, str) or not job_id:
        return None, f"{expected_kind}_job_id_missing"
    try:
        job = ctx.store.job_record(tenant_id, workspace_id, job_id)
    except Exception:
        return None, f"{expected_kind}_job_not_found"
    if job.get("kind") != expected_kind:
        return None, f"{expected_kind}_job_wrong_kind"
    if job.get("execution_status") != "COMPLETED":
        return None, f"{expected_kind}_job_not_completed"
    return job.get("execution_result") or {}, None


class WhatifPreviewExecutor:
    """See module docstring. Zero provider calls: every input is an
    already-persisted "impact"/"simulation" job id, same posture as
    `ImpactAnalysisExecutor`/`SimulationExecutor`."""

    external = False
    executor_id = "whatif.preview.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        payload = ctx.job["payload"]
        for key in ("impact_job_id", "simulation_job_id"):
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise NonRetryableExecutionError(
                    f"{key}_required", f"payload.{key} must be a non-empty string")
        # Load-at-prepare-time so a bad risk/blocker config (operator error,
        # not a caller-controllable input) fails the job loudly rather than
        # silently falling back to an unvalidated default -- same posture as
        # SimulationExecutor.prepare() validating `policy_expression` early.
        load_whatif_risk_config()
        return ExecutionRequest(prompt=None,
                                schema={"type": "object", "executor": self.executor_id},
                                provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
               calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        payload = job["payload"]
        cfg = load_whatif_risk_config()

        impact_job_id = payload.get("impact_job_id")
        simulation_job_id = payload.get("simulation_job_id")
        impact_report, impact_reason = _resolve_report(
            ctx, impact_job_id, expected_kind="impact")
        simulation_report, simulation_reason = _resolve_report(
            ctx, simulation_job_id, expected_kind="simulation")

        blockers: list[dict] = []
        if impact_reason is not None:
            blockers.append({
                "reason": f"report_unavailable:{impact_reason}",
                "source_report_id": impact_job_id, "severity": "HIGH"})
        if simulation_reason is not None:
            blockers.append({
                "reason": f"report_unavailable:{simulation_reason}",
                "source_report_id": simulation_job_id, "severity": "HIGH"})

        # --- open findings on the impact report.
        existing_conflicts = (impact_report or {}).get("existing_conflicts") or []
        for finding in existing_conflicts:
            if finding.get("lifecycle_state") != "OPEN":
                continue
            severity = finding.get("severity")
            if severity in cfg.blocking_severities:
                blockers.append({
                    "reason": f"open_{finding.get('kind', 'finding').lower()}:"
                              f"{finding.get('finding_id')}",
                    "finding_id": finding.get("finding_id"),
                    "severity": severity,
                    "source_report_id": impact_job_id})
            elif cfg.block_on_pending_manual_action and finding.get("authority_required"):
                blockers.append({
                    "reason": f"pending_manual_action:{finding.get('finding_id')}",
                    "finding_id": finding.get("finding_id"),
                    "authority_required": finding.get("authority_required"),
                    "source_report_id": impact_job_id})

        # --- partial coverage on either report.
        impact_coverage = (impact_report or {}).get("coverage") or {}
        if cfg.block_on_partial_coverage and impact_reason is None \
                and impact_coverage.get("complete") is False:
            blockers.append({
                "reason": "impact_report_coverage_incomplete",
                "source_report_id": impact_job_id})
        simulation_coverage = (simulation_report or {}).get("coverage") or {}
        partial_counts = {
            key: simulation_coverage.get(key, 0)
            for key in ("excluded", "unknown", "malformed", "duplicate")}
        if cfg.block_on_partial_coverage and simulation_reason is None \
                and any(count for count in partial_counts.values()):
            blockers.append({
                "reason": "simulation_report_partial_coverage",
                "detail": partial_counts,
                "source_report_id": simulation_job_id})

        # --- numeric claims only for a real EXECUTED simulation.
        simulation_status = (simulation_report or {}).get("status")
        numeric_claims_present = simulation_status == "EXECUTED"
        justified_edits: list[dict] = []
        changed_count_claim = None
        if numeric_claims_present:
            outcomes = simulation_report.get("outcomes") or []
            for outcome in outcomes:
                if not outcome.get("changed"):
                    continue
                justified_edits.append({
                    "historical_case_id": outcome.get("historical_case_id"),
                    "current_outcome": outcome.get("current_outcome"),
                    "shadow_outcome": outcome.get("shadow_outcome"),
                    # Every numeric/operational statement carries a
                    # traceable anchor back to the real report it was read
                    # from -- never bare model prose.
                    "source_report_id": simulation_job_id,
                    "computation_ref": f"job:{simulation_job_id}",
                })
            changed_count_claim = {
                "value": simulation_report.get("changed_count", len(justified_edits)),
                "source_report_id": simulation_job_id,
                "computation_ref": f"job:{simulation_job_id}",
            }
            qualitative_summary = None
        elif simulation_status == "STATIC_ONLY":
            qualitative_summary = (
                "simulation report is STATIC_ONLY -- no operational execution "
                "occurred; no numeric operational claim can be made")
        elif simulation_status == "NOT_EXECUTABLE":
            qualitative_summary = (
                "simulation report is NOT_EXECUTABLE"
                + (f" ({simulation_report.get('reason')})" if simulation_report else "")
                + " -- no numeric operational claim can be made")
        else:
            qualitative_summary = (
                "simulation report unavailable"
                if simulation_reason is not None else
                "simulation report status unrecognized -- no numeric operational claim made")

        readiness = "BLOCKED" if blockers else "READY"

        # An attempted client-supplied override is recorded for audit
        # ONLY -- never merged into the computed verdict above.
        rejected_override = payload.get("override_readiness")

        result = {
            "executor_id": self.executor_id,
            "readiness": readiness,
            "blockers": blockers,
            "numeric_claims_present": numeric_claims_present,
            "justified_edits": justified_edits,
            "changed_count": changed_count_claim,
            "qualitative_summary": qualitative_summary,
            "rejected_override_readiness": rejected_override,
            "impact_job_id": impact_job_id,
            "simulation_job_id": simulation_job_id,
            "impact_report_status": None if impact_reason else "COMPLETED",
            "simulation_report_status": simulation_status,
            "pinned_input": {
                "pinned_source_revision_id": job.get("pinned_source_revision_id"),
                "pinned_knowledge_revision_id": job.get("pinned_knowledge_revision_id"),
                "pinned_decision_epoch": job.get("pinned_decision_epoch"),
            },
        }
        return ExecutionOutcome(result=result, model_id=None)


__all__ = ["WhatifPreviewExecutor"]
