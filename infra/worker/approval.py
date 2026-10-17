"""Readiness and approval digest, job kind "approval",
executor_id "approval.digest.v1".

Contract:

  - Reuses the generic `POST /v2/jobs` / `GET /v2/jobs/{id}` route pair, the
    SAME dispatch mechanism `impact`/`whatif`/`proposal`/`review` already use
    (no second dispatch mechanism). `"approval"` is added to the
    SAME `expected_decision_epoch` kind tuple
    (`infra/v2_policy_routes.py::create_job`) those siblings already share.

  - Consumes an already-persisted, COMPLETED `kind="proposal"` job and an
    already-persisted, COMPLETED `kind="review"` job that reviews that EXACT
    proposal (`review_result["proposal_job_id"] == proposal_job_id`, checked
    here -- a mismatched bundle is a hard, non-retryable failure, never a
    silently-fabricated pairing). A missing/wrong-kind/not-completed
    `proposal_job_id`/`review_job_id` is a `NonRetryableExecutionError`
    (mirrors `infra/worker/review.py::_resolve_proposal`'s own pattern) --
    the job goes FAILED, never COMPLETED with a fabricated bundle.

  - **Snapshot, not approval/publication:** this executor calls
    `ctx.store.stage_blob`/`publish_source`/`approve_job` NOWHERE, and never
    advances `store.head(...)`. It only READS `ctx.store.job_record` and
    `ctx.store.head` -- the workspace's real version/source_revision_id/
    decision_epoch are structurally untouched by any approval computation,
    real or adversarial. Downstream consumers (decision recording, apply)
    must independently recheck freshness/current permission at their own
    mutation time; a once-green `readiness` here is never treated as a
    durable grant.

  - **Reproducible digest, no self-reference:** `approval_digest` is a
    `canonical_hash` (`core/change_intelligence/v2/serialize.py`, the same
    key-order-independent SHA-256 primitive already reviewed elsewhere) over
    a plain dict of: the proposal's own `execution_result` content hash, the
    review's own `execution_result` content hash, the CURRENT
    `source_revision_id`/`decision_epoch` read fresh from `ctx.store.head`
    at execution time, a fixed policy-ceiling snapshot
    (`JOB_CREATE_PERMISSION`/`JOB_APPROVE_PERMISSION["approval"]`), and the
    computed `required_action`. Nothing from THIS job's own row identity
    (its own `job_id`, worker, completion timestamp, or future attestation)
    ever enters that dict -- two independent approval jobs over an unchanged
    dependency set are therefore byte-identical in digest, and no approval
    job can self-reference its own not-yet-written row.

  - **Per-dependency staleness, not a generic flag:** `freshness["source_
    stale"]`/`freshness["epoch_stale"]` each compare the proposal's OWN
    `pinned_input.pinned_source_revision_id`/`pinned_decision_epoch` (the
    values `ProposalExecutor` pinned when it ran, itself pinned from
    `ReviewExecutor`'s identical `pinned_input`, reused verbatim -- never
    re-derived) against the REAL current head read fresh from this
    executor's own `ctx.store.head` call. Each stale condition appends one
    `{"dependency": "source"|"decision_epoch", ...}` entry to
    `stale_reasons`, naming the SPECIFIC dependency, not merely "something
    changed". A proposal revision ("revise") always creates a brand NEW
    `job_id` (`infra/worker/proposal.py`'s own module docstring) so an old
    approval keyed to v1's ids can never be silently reattached to v2's
    bytes; reviewing/approving v2 requires a genuinely new `review_job_id`/
    `approval` job naming v2's own `proposal_job_id`.

  - **Blocked with a specific reason, never a false-positive readiness:**
    `readiness` is `"BLOCKED"` whenever ANY of: the resolved review's own
    `readiness` is not `"READY_FOR_HUMAN_REVIEW"`, the resolved proposal's
    own `blockers` list is non-empty, the proposal's `classification` is
    `"manual"`, or any staleness condition holds. `blocked_reasons` is
    populated from the REAL upstream `checks`/`roles`/`blockers` already
    computed by `ReviewExecutor`/`ProposalExecutor` (never re-derived:
    existing fields are pulled through verbatim) plus this executor's own
    review-readiness/staleness/classification entries -- so a real upstream
    blocker (e.g. `acceptance_tests` UNAVAILABLE, a named
    `report_unavailable:*` proposal blocker) is always visible on the
    approval bundle too, never silently dropped once embedded in a larger
    computation. There is exactly one rule computing `readiness`; no role's
    or check's own status can be overridden or downgraded, and there is no
    code path that reaches `readiness = "READY"` while the underlying
    review reports `"BLOCKED"` -- bypassing that single check is therefore
    the load-bearing gate a mutation/removal test must catch.
"""

from __future__ import annotations

from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters.v2_policy import JOB_APPROVE_PERMISSION, JOB_CREATE_PERMISSION
from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest,
    NonRetryableExecutionError, ProviderCallBudget,
)

_REQUIRED_PAYLOAD_FIELDS = ("proposal_job_id", "review_job_id")


def _resolve_job(ctx: ExecutionContext, job_id, *, expected_kind: str, label: str):
    """Real, tenant/workspace-scoped resolution of an upstream job this
    approval evaluates -- mirrors `infra/worker/review.py::_resolve_proposal`
    (a reused pattern, not a second one). A missing/wrong-kind/
    not-completed reference is a hard, non-retryable failure: there is
    nothing real to approve."""
    tenant_id, workspace_id = ctx.job["tenant_id"], ctx.job["workspace_id"]
    try:
        job = ctx.store.job_record(tenant_id, workspace_id, job_id)
    except Exception as exc:
        raise NonRetryableExecutionError(
            f"{label}_job_not_found", str(job_id)) from exc
    if job.get("kind") != expected_kind:
        raise NonRetryableExecutionError(
            f"{label}_job_wrong_kind", f"kind={job.get('kind')!r}")
    if job.get("execution_status") != "COMPLETED":
        raise NonRetryableExecutionError(
            f"{label}_job_not_completed", f"status={job.get('execution_status')!r}")
    return job


def approval_digest_inputs(*, proposal_job_id, proposal_result, review_job_id,
                           review_result, source_revision_id, decision_epoch,
                           policy_snapshot, required_action) -> dict:
    """The single definition of what `approval_digest` binds. Shared with
    `infra/worker/apply.py`, which recomputes it at apply time from the
    CURRENT proposal/review job rows so a tampered stored
    row cannot be replayed under an approval computed over the original."""
    return {
        "proposal_job_id": proposal_job_id,
        "proposal_result_hash": canonical_hash(proposal_result),
        "review_job_id": review_job_id,
        "review_result_hash": canonical_hash(review_result),
        "source_revision_id": source_revision_id,
        "decision_epoch": decision_epoch,
        "policy_snapshot": policy_snapshot,
        "required_action": required_action,
    }


def _assert_draft_current(ctx, proposal_job_id) -> None:
    """A superseded draft's proposal is history: never reviewed, bundled or applied from now on (checked again here because the job may have been queued or running when the draft was revised)."""
    from infra import v2_lineage
    newer = v2_lineage.newer_draft_for_proposal(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], proposal_job_id)
    if newer:
        raise NonRetryableExecutionError("draft_superseded", f"{v2_lineage.SUPERSEDED_MSG} (newer draft {newer})")


class ApprovalExecutor:
    """See module docstring. Zero provider calls (`external = False`) -- a
    pure snapshot/digest computation over already-persisted state."""

    external = False
    executor_id = "approval.digest.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        payload = ctx.job["payload"]
        for key in _REQUIRED_PAYLOAD_FIELDS:
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise NonRetryableExecutionError(
                    f"{key}_required", f"payload.{key} must be a non-empty string")
        return ExecutionRequest(prompt=None,
                                schema={"type": "object", "executor": self.executor_id},
                                provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
               calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        tenant_id, workspace_id = job["tenant_id"], job["workspace_id"]
        payload = job["payload"]
        proposal_job_id = payload.get("proposal_job_id")
        review_job_id = payload.get("review_job_id")

        proposal_job = _resolve_job(
            ctx, proposal_job_id, expected_kind="proposal", label="proposal")
        _assert_draft_current(ctx, proposal_job_id)
        review_job = _resolve_job(
            ctx, review_job_id, expected_kind="review", label="review")

        proposal_result = proposal_job.get("execution_result") or {}
        review_result = review_job.get("execution_result") or {}

        if review_result.get("proposal_job_id") != proposal_job_id:
            # The named review does not actually evaluate the named
            # proposal -- a mismatched bundle. Never fabricate a pairing;
            # this is the "unsupported executor/exception referenced" A3
            # shape for a self-consistent bundle.
            raise NonRetryableExecutionError(
                "review_proposal_mismatch",
                f"review_job_id={review_job_id!r} evaluates "
                f"proposal_job_id={review_result.get('proposal_job_id')!r}, "
                f"expected {proposal_job_id!r}")

        # Real current state, read fresh at THIS execution -- never a value
        # cached from job submission time (see module docstring).
        head = ctx.store.head(tenant_id, workspace_id)

        pinned_input = proposal_result.get("pinned_input") or {}
        pinned_source_revision_id = pinned_input.get("pinned_source_revision_id")
        pinned_decision_epoch = pinned_input.get("pinned_decision_epoch")

        source_stale = (pinned_source_revision_id is not None
                        and pinned_source_revision_id != head.source_revision_id)
        epoch_stale = (pinned_decision_epoch is not None
                      and pinned_decision_epoch != head.decision_epoch)

        stale_reasons: list = []
        if source_stale:
            stale_reasons.append({
                "dependency": "source",
                "detail": f"pinned_source_revision_id={pinned_source_revision_id!r} "
                         f"current_source_revision_id={head.source_revision_id!r}"})
        if epoch_stale:
            stale_reasons.append({
                "dependency": "decision_epoch",
                "detail": f"pinned_decision_epoch={pinned_decision_epoch!r} "
                         f"current_decision_epoch={head.decision_epoch!r}"})

        # Pull real upstream blockers/readiness through verbatim:
        # never re-derive or reformat what proposal/review already computed.
        blocked_reasons: list = []
        for blocker in proposal_result.get("blockers") or []:
            entry = {"kind": "upstream_proposal_blocker"}
            if isinstance(blocker, dict):
                entry.update(blocker)
            else:
                entry["detail"] = blocker
            blocked_reasons.append(entry)
        for blocker in review_result.get("blockers") or []:
            entry = {"kind": "upstream_review_blocker"}
            if isinstance(blocker, dict):
                entry.update(blocker)
            else:
                entry["detail"] = blocker
            blocked_reasons.append(entry)

        review_readiness = review_result.get("readiness")
        if review_readiness != "READY_FOR_HUMAN_REVIEW":
            blocked_reasons.append({
                "kind": "review", "name": "review_readiness",
                "reason": review_readiness})

        classification = proposal_result.get("classification")
        if classification == "manual":
            blocked_reasons.append({
                "kind": "proposal", "name": "classification", "reason": "manual"})

        # Single, deterministic rule -- never AI-overridden, never a
        # majority vote (same posture as ReviewExecutor's own `readiness`):
        # ANY real blocker or staleness condition forces BLOCKED.
        readiness = "BLOCKED" if (blocked_reasons or source_stale or epoch_stale) else "READY"

        if classification == "manual":
            required_action = "manual_work_required"
        elif readiness == "BLOCKED":
            required_action = "resolve_blockers"
        else:
            required_action = "human_approval_required"

        # Fixed policy-ceiling identity -- not a live per-caller membership
        # check (that already happened at the route layer); this is the
        # "consumes membership/permission policy" input into the digest, a
        # stable identity of WHICH ceiling this bundle was evaluated under.
        policy_snapshot = {
            "job_create_permission": JOB_CREATE_PERMISSION.get("approval"),
            "job_approve_permission": JOB_APPROVE_PERMISSION.get("approval"),
        }

        # Excludes this job's OWN row identity entirely (no job_id, no
        # worker_id, no completion timestamp, no attestation of this job
        # itself) -- see module docstring's no-self-reference guarantee.
        digest_inputs = approval_digest_inputs(
            proposal_job_id=proposal_job_id, proposal_result=proposal_result,
            review_job_id=review_job_id, review_result=review_result,
            source_revision_id=head.source_revision_id,
            decision_epoch=head.decision_epoch,
            policy_snapshot=policy_snapshot, required_action=required_action)
        approval_digest = canonical_hash(digest_inputs)

        result = {
            "executor_id": self.executor_id,
            "proposal_job_id": proposal_job_id,
            "review_job_id": review_job_id,
            "proposal_version": proposal_result.get("proposal_version"),
            "readiness": readiness,
            "required_action": required_action,
            "blocked_reasons": blocked_reasons,
            "freshness": {"source_stale": source_stale, "epoch_stale": epoch_stale},
            "stale_reasons": stale_reasons,
            "approval_digest": approval_digest,
            "policy_snapshot": policy_snapshot,
            "snapshot": {"source_revision_id": head.source_revision_id,
                        "decision_epoch": head.decision_epoch},
        }
        _assert_draft_current(ctx, proposal_job_id)
        return ExecutionOutcome(result=result, model_id=None)


__all__ = ["ApprovalExecutor", "approval_digest_inputs"]
